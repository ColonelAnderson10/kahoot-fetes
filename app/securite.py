"""Sessions, CSRF, limitation de débit, anti-force brute et en-têtes de sécurité."""

import hashlib
import hmac
import secrets
import threading
import time
from collections import defaultdict, deque

from fastapi import HTTPException, Request
from starlette.datastructures import FormData
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from . import db
from .config import Parametres

DUREE_SESSION_ANONYME = 3600  # 1 h : sert seulement à porter le jeton CSRF des formulaires de connexion


# --- Outils ---

def _hash(valeur: str) -> str:
    return hashlib.sha256(valeur.encode("utf-8")).hexdigest()


def comparer_secret(fourni: str, attendu: str) -> bool:
    """Comparaison en temps constant (on compare des empreintes de même longueur)."""
    return hmac.compare_digest(_hash(fourni), _hash(attendu))


def nom_cookie(parametres: Parametres) -> str:
    # Le préfixe __Host- impose Secure, Path=/ et l'absence de Domain
    return "__Host-kf_session" if parametres.cookies_securises else "kf_session"


# --- Sessions côté serveur ---

def _creer_session(prenom: str | None, acces_ami: bool, admin: bool, expire_le: float, admin_expire_le: float) -> tuple[str, dict]:
    jeton = secrets.token_urlsafe(32)
    session = {
        "jeton_hash": _hash(jeton),
        "csrf": secrets.token_urlsafe(32),
        "prenom": prenom,
        "acces_ami": int(acces_ami),
        "admin": int(admin),
        "expire_le": expire_le,
        "admin_expire_le": admin_expire_le,
    }
    with db.connexion() as cx:
        cx.execute("DELETE FROM sessions WHERE expire_le < ?", (time.time(),))
        cx.execute(
            "INSERT INTO sessions (jeton_hash, csrf, prenom, acces_ami, admin, expire_le, admin_expire_le) "
            "VALUES (:jeton_hash, :csrf, :prenom, :acces_ami, :admin, :expire_le, :admin_expire_le)",
            session,
        )
    return jeton, session


def _charger_session(jeton: str) -> dict | None:
    with db.connexion() as cx:
        ligne = cx.execute("SELECT * FROM sessions WHERE jeton_hash = ?", (_hash(jeton),)).fetchone()
    if ligne is None:
        return None
    session = dict(ligne)
    maintenant = time.time()
    if session["expire_le"] < maintenant:
        _supprimer_session(session["jeton_hash"])
        return None
    if session["admin"] and session["admin_expire_le"] < maintenant:
        session["admin"] = 0
        with db.connexion() as cx:
            cx.execute("UPDATE sessions SET admin = 0 WHERE jeton_hash = ?", (session["jeton_hash"],))
    return session


def _supprimer_session(jeton_hash: str) -> None:
    with db.connexion() as cx:
        cx.execute("DELETE FROM sessions WHERE jeton_hash = ?", (jeton_hash,))


def purger_sessions_si_secrets_changes(parametres: Parametres) -> None:
    """Invalide toutes les sessions si CODE_ACCES ou ADMIN_MOT_DE_PASSE a changé depuis le dernier démarrage."""
    empreinte = _hash("kahoot-fetes|" + parametres.code_acces + "|" + parametres.admin_mot_de_passe)
    with db.connexion() as cx:
        ligne = cx.execute("SELECT valeur FROM meta WHERE cle = 'empreinte_secrets'").fetchone()
        if ligne is None or not hmac.compare_digest(ligne["valeur"], empreinte):
            cx.execute("DELETE FROM sessions")
            cx.execute("INSERT OR REPLACE INTO meta (cle, valeur) VALUES ('empreinte_secrets', ?)", (empreinte,))


def session_courante(request: Request) -> dict:
    """Renvoie la session de la requête, en créant une session anonyme au besoin."""
    session = request.state.session
    if session is None:
        jeton, session = _creer_session(None, False, False, time.time() + DUREE_SESSION_ANONYME, 0)
        request.state.session = session
        request.state.nouveau_jeton = jeton
    return session


def regenerer_session(request: Request, *, prenom: str | None = None, acces_ami: bool | None = None,
                      admin: bool | None = None) -> dict:
    """Crée une nouvelle session (nouveau jeton, nouveau CSRF) et détruit l'ancienne.

    Appelée à chaque connexion pour empêcher la fixation de session.
    """
    parametres: Parametres = request.app.state.parametres
    ancienne = request.state.session or {}
    maintenant = time.time()
    acces = bool(ancienne.get("acces_ami")) if acces_ami is None else acces_ami
    est_admin = bool(ancienne.get("admin")) if admin is None else admin
    prenom = ancienne.get("prenom") if prenom is None else prenom

    expire_le = maintenant + DUREE_SESSION_ANONYME
    if acces:
        expire_le = max(expire_le, maintenant + parametres.session_ami_jours * 86400)
    admin_expire_le = maintenant + parametres.session_admin_minutes * 60 if est_admin else 0
    if est_admin:
        expire_le = max(expire_le, admin_expire_le)

    if ancienne.get("jeton_hash"):
        _supprimer_session(ancienne["jeton_hash"])
    jeton, session = _creer_session(prenom, acces, est_admin, expire_le, admin_expire_le)
    request.state.session = session
    request.state.nouveau_jeton = jeton
    return session


def changer_prenom(request: Request, prenom: str) -> None:
    session = session_courante(request)
    session["prenom"] = prenom
    with db.connexion() as cx:
        cx.execute("UPDATE sessions SET prenom = ? WHERE jeton_hash = ?", (prenom, session["jeton_hash"]))


def prolonger_admin(request: Request) -> None:
    """Expiration glissante de la session admin."""
    parametres: Parametres = request.app.state.parametres
    session = request.state.session
    nouvelle = time.time() + parametres.session_admin_minutes * 60
    with db.connexion() as cx:
        cx.execute(
            "UPDATE sessions SET admin_expire_le = ?, expire_le = MAX(expire_le, ?) WHERE jeton_hash = ?",
            (nouvelle, nouvelle, session["jeton_hash"]),
        )


def est_ami(request: Request) -> bool:
    session = request.state.session
    return bool(session and session["acces_ami"])


def est_admin(request: Request) -> bool:
    session = request.state.session
    return bool(session and session["admin"])


class MiddlewareSession:
    """Charge la session depuis le cookie et pose le cookie quand un nouveau jeton est émis."""

    def __init__(self, app: ASGIApp, parametres: Parametres) -> None:
        self.app = app
        self.parametres = parametres

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        from starlette.requests import HTTPConnection

        connexion = HTTPConnection(scope)
        nom = nom_cookie(self.parametres)
        jeton = connexion.cookies.get(nom)
        etat = scope.setdefault("state", {})
        etat["session"] = _charger_session(jeton) if jeton and len(jeton) < 100 else None
        etat["nouveau_jeton"] = None

        async def envoyer(message: Message) -> None:
            if message["type"] == "http.response.start" and etat.get("nouveau_jeton"):
                attributs = [
                    f"{nom}={etat['nouveau_jeton']}",
                    "Path=/",
                    "HttpOnly",
                    "SameSite=Lax",
                    f"Max-Age={self.parametres.session_ami_jours * 86400}",
                ]
                if self.parametres.cookies_securises:
                    attributs.append("Secure")
                entetes = list(message.get("headers", []))
                entetes.append((b"set-cookie", "; ".join(attributs).encode("latin-1")))
                message["headers"] = entetes
            await send(message)

        await self.app(scope, receive, envoyer)


# --- CSRF ---

async def lire_formulaire(request: Request, max_fichiers: int = 1) -> FormData:
    """Lit le formulaire avec des limites strictes et vérifie le jeton CSRF."""
    formulaire = await request.form(max_files=max_fichiers, max_fields=20, max_part_size=64 * 1024)
    session = request.state.session
    fourni = formulaire.get("csrf")
    if not session or not isinstance(fourni, str) or not hmac.compare_digest(
        fourni.encode("utf-8"), session["csrf"].encode("utf-8")
    ):
        await formulaire.close()
        raise HTTPException(status_code=403, detail="Jeton de sécurité invalide. Rechargez la page et réessayez.")
    return formulaire


# --- Limitation de débit et anti-force brute (en mémoire, un seul processus) ---

class LimiteurDebit:
    def __init__(self) -> None:
        self._verrou = threading.Lock()
        self._requetes: dict[str, deque] = defaultdict(deque)

    def autoriser(self, cle: str, limite: int, fenetre: float) -> bool:
        maintenant = time.monotonic()
        with self._verrou:
            file = self._requetes[cle]
            while file and file[0] <= maintenant - fenetre:
                file.popleft()
            if len(file) >= limite:
                return False
            file.append(maintenant)
            if len(self._requetes) > 50_000:  # garde-fou mémoire : on retire seulement les fenêtres expirées
                for c in [c for c, f in self._requetes.items() if not f or f[-1] <= maintenant - fenetre]:
                    del self._requetes[c]
            return True

    def reinitialiser(self) -> None:
        with self._verrou:
            self._requetes.clear()


class AntiForceBrute:
    """Bloque temporairement une IP (et, au-delà d'un seuil global, tout le monde) après des échecs répétés."""

    def __init__(self, essais_par_ip: int = 5, essais_globaux: int = 30, duree_blocage: float = 900) -> None:
        self.essais_par_ip = essais_par_ip
        self.essais_globaux = essais_globaux
        self.duree = duree_blocage
        self._verrou = threading.Lock()
        self._echecs: dict[str, deque] = defaultdict(deque)
        self._echecs_globaux: deque = deque()

    def _purger(self, file: deque, maintenant: float) -> None:
        while file and file[0] <= maintenant - self.duree:
            file.popleft()

    def bloque(self, ip: str) -> bool:
        maintenant = time.monotonic()
        with self._verrou:
            file = self._echecs[ip]
            self._purger(file, maintenant)
            self._purger(self._echecs_globaux, maintenant)
            return len(file) >= self.essais_par_ip or len(self._echecs_globaux) >= self.essais_globaux

    def echec(self, ip: str) -> None:
        maintenant = time.monotonic()
        with self._verrou:
            if len(self._echecs) > 50_000:  # garde-fou mémoire : on retire les IP sans échec récent
                for cle in [c for c, f in self._echecs.items() if not f or f[-1] <= maintenant - self.duree]:
                    del self._echecs[cle]
            self._echecs[ip].append(maintenant)
            self._echecs_globaux.append(maintenant)

    def succes(self, ip: str) -> None:
        with self._verrou:
            self._echecs.pop(ip, None)

    def reinitialiser(self) -> None:
        with self._verrou:
            self._echecs.clear()
            self._echecs_globaux.clear()


def ip_client(request: Request) -> str:
    # uvicorn remplace request.client par l'IP réelle quand le proxy est de confiance (--forwarded-allow-ips)
    return request.client.host if request.client else "inconnue"


# --- Middlewares ASGI bruts ---

def _reponse_simple(statut: int, texte: str) -> tuple[Message, Message]:
    corps = (
        "<!doctype html><html lang=\"fr\"><meta charset=\"utf-8\"><title>Erreur</title>"
        f"<p>{texte}</p></html>"
    ).encode("utf-8")
    debut = {
        "type": "http.response.start",
        "status": statut,
        "headers": [(b"content-type", b"text/html; charset=utf-8"), (b"content-length", str(len(corps)).encode())],
    }
    return debut, {"type": "http.response.body", "body": corps}


class _CorpsTropGros(Exception):
    pass


class MiddlewareLimiteCorps:
    """Refuse les requêtes dont le corps dépasse la limite (Content-Length annoncé ou flux réel)."""

    def __init__(self, app: ASGIApp, limite: int) -> None:
        self.app = app
        self.limite = limite

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        for nom, valeur in scope.get("headers", []):
            if nom == b"content-length":
                try:
                    trop_gros = int(valeur) > self.limite
                except ValueError:
                    trop_gros = True
                if trop_gros:
                    for m in _reponse_simple(413, "Fichier trop volumineux."):
                        await send(m)
                    return

        recu = 0
        reponse_commencee = False

        async def recevoir() -> Message:
            nonlocal recu
            message = await receive()
            if message["type"] == "http.request":
                recu += len(message.get("body", b""))
                if recu > self.limite:
                    raise _CorpsTropGros()
            return message

        async def envoyer(message: Message) -> None:
            nonlocal reponse_commencee
            if message["type"] == "http.response.start":
                reponse_commencee = True
            await send(message)

        try:
            await self.app(scope, recevoir, envoyer)
        except _CorpsTropGros:
            if not reponse_commencee:
                for m in _reponse_simple(413, "Fichier trop volumineux."):
                    await send(m)


class MiddlewareSecurite:
    """Limitation de débit par IP et en-têtes de sécurité sur toutes les réponses."""

    def __init__(self, app: ASGIApp, parametres: Parametres, limiteur: LimiteurDebit) -> None:
        self.app = app
        self.parametres = parametres
        self.limiteur = limiteur

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        client = scope.get("client")
        ip = client[0] if client else "inconnue"
        methode = scope["method"]
        chemin: str = scope["path"]
        if chemin != "/health":
            if methode == "POST" and chemin == "/photos":
                # Envois en parallèle depuis le téléphone : limite propre, plus haute
                autorise = self.limiteur.autoriser(f"photos:{ip}", 300, 60)
            elif methode == "POST":
                autorise = self.limiteur.autoriser(f"post:{ip}", 120, 60)
            else:
                autorise = self.limiteur.autoriser(f"get:{ip}", 600, 60)
            if not autorise:
                for m in _reponse_simple(429, "Trop de requêtes. Patientez une minute et réessayez."):
                    await self._envoyer_avec_entetes(send, m, chemin)
                return

        async def envoyer(message: Message) -> None:
            await self._envoyer_avec_entetes(send, message, chemin)

        await self.app(scope, receive, envoyer)

    async def _envoyer_avec_entetes(self, send: Send, message: Message, chemin: str) -> None:
        if message["type"] == "http.response.start":
            entetes = [(k, v) for k, v in message.get("headers", []) if k.lower() != b"server"]
            entetes += [
                (b"content-security-policy",
                 b"default-src 'self'; img-src 'self' blob: data:; script-src 'self'; style-src 'self'; "
                 b"object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"),
                (b"x-content-type-options", b"nosniff"),
                (b"x-frame-options", b"DENY"),
                (b"referrer-policy", b"no-referrer"),
                (b"permissions-policy", b"camera=(), microphone=(), geolocation=()"),
                (b"cross-origin-opener-policy", b"same-origin"),
                (b"cross-origin-resource-policy", b"same-origin"),
            ]
            if not chemin.startswith("/static/"):
                entetes.append((b"cache-control", b"no-store"))
            if self.parametres.production:
                entetes.append((b"strict-transport-security", b"max-age=31536000; includeSubDomains"))
            message["headers"] = entetes
        await send(message)
