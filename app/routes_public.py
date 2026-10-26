"""Pages publiques : code d'accès, prénom, envoi de photos et de citations."""

import asyncio
import re
import shutil
import unicodedata

from fastapi import APIRouter, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from starlette.datastructures import UploadFile

from . import db, securite
from .images import ImageInvalide, traiter_photo

routeur = APIRouter()

LONGUEUR_PRENOM = 40
LONGUEUR_DESCRIPTION = 500
LONGUEUR_CITATION = 1000
LONGUEUR_PERSONNE = 60
LONGUEUR_CONTEXTE = 500
MAX_PHOTOS_PAR_REQUETE = 20
MOTIF_FICHIER = re.compile(r"^[0-9a-f]{32}\.jpg$")
ESPACE_LIBRE_MIN = 1024 * 1024 * 1024  # 1 Go

# Au plus 2 photos décodées en même temps : une photo de 50 Mpx occupe ~150 Mo en mémoire
_traitement_images = asyncio.Semaphore(2)


def nettoyer(texte: object, longueur_max: int, multiligne: bool = False) -> str:
    """Retire les caractères de contrôle et tronque. L'échappement HTML est fait par Jinja2 à l'affichage."""
    if not isinstance(texte, str):
        return ""
    texte = unicodedata.normalize("NFC", texte)
    autorises = {"\n"} if multiligne else set()
    texte = "".join(c for c in texte if c in autorises or unicodedata.category(c)[0] != "C")
    if multiligne:
        texte = re.sub(r"\n{3,}", "\n\n", texte)
    return texte.strip()[:longueur_max]


def rendre(request: Request, gabarit: str, statut: int = 200, **contexte) -> Response:
    session = securite.session_courante(request)
    contexte.update(csrf=session["csrf"], prenom=session.get("prenom"), est_admin=bool(session["admin"]))
    return request.app.state.gabarits.TemplateResponse(request, gabarit, contexte, status_code=statut)


def rediriger(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


def exiger_ami(request: Request) -> str | None:
    """Renvoie l'URL de redirection si l'ami n'est pas connecté ou n'a pas de prénom."""
    if not securite.est_ami(request):
        return "/acces"
    if not request.state.session.get("prenom"):
        return "/prenom"
    return None


# --- Code d'accès ---

@routeur.get("/acces")
async def page_acces(request: Request):
    if securite.est_ami(request):
        return rediriger("/")
    return rendre(request, "acces.html")


@routeur.post("/acces")
async def valider_acces(request: Request):
    formulaire = await securite.lire_formulaire(request, max_fichiers=0)
    ip = securite.ip_client(request)
    garde = request.app.state.anti_force_brute_code
    if garde.bloque(ip):
        return rendre(request, "acces.html", 429, erreur="Trop d'essais. Réessayez dans 15 minutes.")
    code = formulaire.get("code")
    if not isinstance(code, str) or not securite.comparer_secret(code.strip(), request.app.state.parametres.code_acces):
        garde.echec(ip)
        return rendre(request, "acces.html", 401, erreur="Code incorrect.")
    garde.succes(ip)
    securite.regenerer_session(request, acces_ami=True)
    return rediriger("/" if request.state.session.get("prenom") else "/prenom")


# --- Prénom ---

@routeur.get("/prenom")
async def page_prenom(request: Request):
    if not securite.est_ami(request):
        return rediriger("/acces")
    return rendre(request, "prenom.html")


@routeur.post("/prenom")
async def enregistrer_prenom(request: Request):
    if not securite.est_ami(request):
        return rediriger("/acces")
    formulaire = await securite.lire_formulaire(request, max_fichiers=0)
    prenom = nettoyer(formulaire.get("prenom"), LONGUEUR_PRENOM)
    if not prenom:
        return rendre(request, "prenom.html", 400, erreur="Entrez votre prénom.")
    securite.changer_prenom(request, prenom)
    return rediriger("/")


# --- Accueil ---

@routeur.get("/")
async def accueil(request: Request):
    if url := exiger_ami(request):
        return rediriger(url)
    return rendre(request, "accueil.html")


# --- Photos ---

@routeur.get("/photos")
async def page_photos(request: Request):
    if url := exiger_ami(request):
        return rediriger(url)
    return rendre(request, "photos.html", taille_max_mo=request.app.state.parametres.taille_max_photo_mo)


@routeur.post("/photos")
async def envoyer_photos(request: Request):
    via_js = request.headers.get("x-requested-with") == "fetch"
    if url := exiger_ami(request):
        if via_js:
            return JSONResponse({"ok": False, "message": "Session expirée. Rechargez la page."}, status_code=401)
        return rediriger(url)

    parametres = request.app.state.parametres
    formulaire = await securite.lire_formulaire(request, max_fichiers=MAX_PHOTOS_PAR_REQUETE)
    try:
        fichiers = [f for f in formulaire.getlist("photos") if isinstance(f, UploadFile) and f.filename is not None]
        descriptions = formulaire.getlist("descriptions")
        if not fichiers:
            raise ImageInvalide("Aucune photo reçue.")

        auteur = request.state.session["prenom"]
        enregistrees = 0
        erreurs = []
        for i, fichier in enumerate(fichiers):
            description = nettoyer(descriptions[i] if i < len(descriptions) else "", LONGUEUR_DESCRIPTION, True)
            if fichier.size is not None and fichier.size > parametres.taille_max_photo:
                erreurs.append(f"Photo {i + 1} : trop volumineuse (max {parametres.taille_max_photo_mo} Mo).")
                continue
            if shutil.disk_usage(parametres.dossier_photos).free < ESPACE_LIBRE_MIN:
                erreurs.append("Le serveur est plein. Préviens l'organisateur.")
                break
            try:
                async with _traitement_images:
                    nom = await run_in_threadpool(
                        traiter_photo, fichier.file, parametres.dossier_photos, parametres.dossier_miniatures
                    )
            except ImageInvalide as e:
                erreurs.append(f"Photo {i + 1} : {e}")
                continue
            db.ajouter_photo(auteur, nom, description)
            enregistrees += 1
    except ImageInvalide as e:
        enregistrees, erreurs = 0, [str(e)]
    finally:
        await formulaire.close()

    if via_js:
        statut = 200 if enregistrees else 400
        return JSONResponse({"ok": enregistrees > 0, "enregistrees": enregistrees, "erreurs": erreurs}, status_code=statut)
    if not enregistrees:
        return rendre(request, "photos.html", 400, erreur=" ".join(erreurs),
                      taille_max_mo=parametres.taille_max_photo_mo)
    return rediriger(f"/merci?photos={enregistrees}")


# --- Citations ---

@routeur.get("/citation")
async def page_citation(request: Request):
    if url := exiger_ami(request):
        return rediriger(url)
    return rendre(request, "citation.html")


@routeur.post("/citation")
async def envoyer_citation(request: Request):
    if url := exiger_ami(request):
        return rediriger(url)
    formulaire = await securite.lire_formulaire(request, max_fichiers=0)
    citation = nettoyer(formulaire.get("citation"), LONGUEUR_CITATION, True)
    personne = nettoyer(formulaire.get("personne"), LONGUEUR_PERSONNE)
    contexte = nettoyer(formulaire.get("contexte"), LONGUEUR_CONTEXTE, True)
    if not citation or not personne:
        return rendre(request, "citation.html", 400, erreur="La citation et la personne sont obligatoires.",
                      valeurs={"citation": citation, "personne": personne, "contexte": contexte})
    db.ajouter_citation(request.state.session["prenom"], citation, personne, contexte)
    return rediriger("/merci?citation=1")


@routeur.get("/merci")
async def merci(request: Request):
    if url := exiger_ami(request):
        return rediriger(url)
    try:
        photos = max(0, min(int(request.query_params.get("photos", "0")), 10_000))
    except ValueError:
        photos = 0
    return rendre(request, "merci.html", photos=photos, citation=request.query_params.get("citation") == "1")


# --- Photos servies (admin seulement) ---

@routeur.get("/media/{nom}")
async def media(request: Request, nom: str, mini: bool = False):
    if not securite.est_admin(request):
        raise HTTPException(status_code=404)
    if not MOTIF_FICHIER.fullmatch(nom):
        raise HTTPException(status_code=404)
    parametres = request.app.state.parametres
    dossier = parametres.dossier_miniatures if mini else parametres.dossier_photos
    chemin = dossier / nom
    if not chemin.is_file():
        raise HTTPException(status_code=404)
    return FileResponse(chemin, media_type="image/jpeg")
