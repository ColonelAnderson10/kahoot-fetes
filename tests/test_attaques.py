"""Tests d'attaque : chaque test simule une attaque et vérifie qu'elle échoue."""

import csv
import io
import zipfile

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app import db
from app.export import neutraliser_cellule
from app.main import creer_app

from .conftest import (
    ADMIN, CODE, connecter_admin, connecter_ami, creer_parametres, envoyer_photo, image_octets, jeton_csrf,
)

XSS = '<script>alert("xss")</script><img src=x onerror=alert(1)>"\'>'


# --- Téléversements ---

@pytest.mark.parametrize("contenu, nom", [
    (b"#!/bin/sh\nrm -rf /\n", "script.jpg"),
    (b"<?php system($_GET['c']); ?>", "shell.php.jpg"),
    (b"<html><script>alert(1)</script></html>", "page.png"),
    (b"MZ\x90\x00" + b"\x00" * 200, "virus.heic"),
    (b"\xff\xd8\xff\xe0" + b"pas vraiment un jpeg" * 10, "tronque.jpg"),  # bonne signature, contenu invalide
    (b"", "vide.jpg"),
])
def test_faux_fichier_image_refuse(client, parametres, contenu, nom):
    connecter_ami(client)
    r = envoyer_photo(client, contenu, nom=nom)
    assert r.status_code == 400 and r.json()["ok"] is False
    assert list(parametres.dossier_photos.glob("*.jpg")) == []
    assert db.lister_soumissions() == []


def test_fichier_polyglotte_nettoye(client, parametres):
    """Un vrai JPEG suivi d'un script : la ré-encodage élimine tout ce qui n'est pas l'image."""
    connecter_ami(client)
    charge = b"<script>alert('polyglotte')</script><?php echo 1; ?>"
    r = envoyer_photo(client, image_octets() + charge)
    assert r.status_code == 200
    [fichier] = parametres.dossier_photos.glob("*.jpg")
    assert b"<script>" not in fichier.read_bytes() and b"<?php" not in fichier.read_bytes()


def test_bombe_de_decompression_refusee(client, parametres):
    """PNG de 12 000 x 12 000 (144 Mpx) qui pèse quelques dizaines de Ko compressé."""
    tampon = io.BytesIO()
    Image.new("1", (12_000, 12_000)).save(tampon, "PNG", optimize=True)
    assert len(tampon.getvalue()) < 1024 * 1024
    connecter_ami(client)
    r = envoyer_photo(client, tampon.getvalue(), nom="bombe.png", type_mime="image/png")
    assert r.status_code == 400 and "trop grande" in " ".join(r.json()["erreurs"]).lower()
    assert list(parametres.dossier_photos.glob("*.jpg")) == []


def test_traversee_de_repertoire_par_nom_de_fichier(client, parametres, tmp_path):
    connecter_ami(client)
    r = envoyer_photo(client, image_octets(), nom="../../../../evil.jpg")
    assert r.status_code == 200
    [fichier] = parametres.dossier_photos.glob("*.jpg")
    assert fichier.parent == parametres.dossier_photos and "evil" not in fichier.name
    assert not (tmp_path / "evil.jpg").exists()


def test_fichier_trop_gros_refuse(tmp_path):
    parametres = creer_parametres(tmp_path / "data", taille_max_photo_mo=1)
    with TestClient(creer_app(parametres)) as c:
        connecter_ami(c)
        r = envoyer_photo(c, b"\xff\xd8\xff" + b"0" * (3 * 1024 * 1024))
        assert r.status_code == 413
        assert db.lister_soumissions() == []


def test_trop_de_fichiers_dans_une_requete(client):
    connecter_ami(client)
    fichiers = [("photos", (f"{i}.jpg", image_octets(taille=(10, 10)), "image/jpeg")) for i in range(25)]
    r = client.post("/photos", data={"csrf": jeton_csrf(client, "/photos")}, files=fichiers,
                    headers={"X-Requested-With": "fetch"})
    assert r.status_code == 400
    assert db.lister_soumissions() == []


# --- XSS ---

def test_xss_echappe_partout(client):
    connecter_ami(client, XSS[:40])
    envoyer_photo(client, image_octets(), description=XSS)
    client.post("/citation", data={"csrf": jeton_csrf(client, "/citation"), "citation": XSS, "personne": XSS[:60],
                                   "contexte": XSS})
    pages = [client.get("/").text, client.get("/photos").text]
    connecter_admin(client)
    pages.append(client.get("/admin").text)
    pages.append(client.get("/admin", params={"auteur": XSS[:40]}).text)
    for page in pages:
        assert "<script>alert" not in page
        assert "onerror=alert(1)>" not in page
    assert "&lt;script&gt;" in pages[2]


def test_xss_page_erreur(client):
    r = client.get("/<script>alert(1)</script>")
    assert r.status_code == 404 and "<script>alert" not in r.text


# --- Injection SQL ---

def test_injection_sql(client):
    malveillant = "x'); DROP TABLE soumissions; --"
    connecter_ami(client, malveillant)
    client.post("/citation", data={"csrf": jeton_csrf(client, "/citation"), "citation": "' OR 1=1 --",
                                   "personne": malveillant})
    connecter_admin(client)
    r = client.get("/admin", params={"auteur": "' OR '1'='1", "type": "photo' OR '1'='1"})
    assert r.status_code == 200 and "Aucune soumission" in r.text
    [s] = db.lister_soumissions()
    assert s["auteur"] == malveillant and s["citation"] == "' OR 1=1 --"


# --- Injection CSV ---

@pytest.mark.parametrize("valeur", ["=HYPERLINK(\"http://evil\",\"x\")", "+1+1", "-2+3", "@SUM(A1)", "=cmd|' /C calc'!A0"])
def test_neutralisation_cellule(valeur):
    assert neutraliser_cellule(valeur).startswith("'")


def test_injection_csv_dans_export(client):
    connecter_ami(client, "=1+1")
    client.post("/citation", data={"csrf": jeton_csrf(client, "/citation"),
                                   "citation": "=HYPERLINK(\"http://evil.example\",\"clic\")",
                                   "personne": "@SUM(A1:A9)", "contexte": ""})
    connecter_admin(client)
    csrf = jeton_csrf(client, "/admin")
    [s] = db.lister_soumissions()
    client.post(f"/admin/soumission/{s['id']}/retenir", data={"csrf": csrf})
    r = client.post("/admin/export", data={"csrf": csrf})
    with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
        lignes = list(csv.reader(io.StringIO(zf.read("kahoot.csv").decode("utf-8-sig")), delimiter=";"))
    ligne = lignes[1]
    assert ligne[2] == "'=1+1" and ligne[3] == "'@SUM(A1:A9)" and ligne[4].startswith("'=HYPERLINK")


# --- Authentification ---

def test_force_brute_code_bloquee(client):
    for _ in range(5):
        r = client.post("/acces", data={"csrf": jeton_csrf(client, "/acces"), "code": "devinette123"})
        assert r.status_code == 401
    # Même le bon code est refusé pendant le blocage
    r = client.post("/acces", data={"csrf": jeton_csrf(client, "/acces"), "code": CODE}, follow_redirects=False)
    assert r.status_code == 429


def test_force_brute_admin_bloquee(client):
    for _ in range(5):
        r = client.post("/admin/connexion", data={"csrf": jeton_csrf(client, "/admin"), "mot_de_passe": "Devinette!123"})
        assert r.status_code == 401
    r = client.post("/admin/connexion", data={"csrf": jeton_csrf(client, "/admin"), "mot_de_passe": ADMIN},
                    follow_redirects=False)
    assert r.status_code == 429
    assert "Soumissions" not in client.get("/admin").text


def test_force_brute_distribuee_bloquee(client):
    """Au-delà de 20 échecs admin au total (plusieurs IP), la connexion admin est suspendue pour tous."""
    garde = client.app.state.anti_force_brute_admin
    for i in range(20):
        garde.echec(f"10.0.0.{i}")
    r = client.post("/admin/connexion", data={"csrf": jeton_csrf(client, "/admin"), "mot_de_passe": ADMIN})
    assert r.status_code == 429


def test_session_regeneree_a_la_connexion(client, parametres):
    """Fixation de session : le jeton reçu avant la connexion ne doit plus être valide après."""
    client.get("/acces")
    jeton_avant = client.cookies.get("kf_session")
    connecter_ami(client)
    jeton_apres = client.cookies.get("kf_session")
    assert jeton_avant and jeton_apres and jeton_avant != jeton_apres

    with TestClient(creer_app(parametres)) as attaquant:
        attaquant.cookies.set("kf_session", jeton_avant)
        r = attaquant.get("/photos", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/acces"


def test_session_expiree(client):
    connecter_ami(client)
    with db.connexion() as cx:
        cx.execute("UPDATE sessions SET expire_le = 0")
    r = client.get("/photos", follow_redirects=False)
    assert r.headers["location"] == "/acces"


def test_session_admin_expiree(client):
    connecter_admin(client)
    with db.connexion() as cx:
        cx.execute("UPDATE sessions SET admin_expire_le = 1")
    assert "Mot de passe admin" in client.get("/admin").text


def test_jeton_de_session_forge(client):
    client.cookies.set("kf_session", "A" * 43)
    assert client.get("/photos", follow_redirects=False).headers["location"] == "/acces"
    client.cookies.set("kf_session", "x" * 5000)
    assert client.get("/photos", follow_redirects=False).status_code == 303


def test_changement_de_code_invalide_les_sessions(tmp_path):
    dossier = tmp_path / "data"
    with TestClient(creer_app(creer_parametres(dossier))) as c:
        connecter_ami(c)
        jeton = c.cookies.get("kf_session")
    with TestClient(creer_app(creer_parametres(dossier, code_acces="nouveaucode2026"))) as c:
        c.cookies.set("kf_session", jeton)
        assert c.get("/photos", follow_redirects=False).headers["location"] == "/acces"


# --- Contrôle d'accès ---

def test_photos_inaccessibles_sans_admin(client):
    connecter_ami(client)
    envoyer_photo(client, image_octets())
    [s] = db.lister_soumissions()
    # Ni un ami, ni un inconnu ne peuvent récupérer une photo, même en connaissant son nom exact
    assert client.get(f"/media/{s['fichier']}").status_code == 404
    with TestClient(client.app) as inconnu:
        assert inconnu.get(f"/media/{s['fichier']}").status_code == 404


@pytest.mark.parametrize("chemin", [
    "/media/..%2Fkahoot.db", "/media/../kahoot.db", "/media/%2e%2e%2fkahoot.db",
    "/media/kahoot.db", "/static/../data/kahoot.db", "/data/kahoot.db", "/data/uploads/",
])
def test_traversee_url(client, chemin):
    connecter_admin(client)
    r = client.get(chemin)
    assert r.status_code == 404 and b"SQLite" not in r.content


def test_actions_admin_refusees_sans_mot_de_passe(client):
    connecter_ami(client)
    client.post("/citation", data={"csrf": jeton_csrf(client, "/citation"), "citation": "c", "personne": "p"})
    [s] = db.lister_soumissions()
    csrf = jeton_csrf(client, "/citation")  # jeton valide de l'ami, mais il n'est pas admin
    for action in ("modifier", "retenir", "supprimer"):
        client.post(f"/admin/soumission/{s['id']}/{action}", data={"csrf": csrf, "citation": "piraté"})
    s2 = db.obtenir_soumission(s["id"])
    assert s2 is not None and s2["citation"] == "c" and s2["retenu"] == 0
    r = client.post("/admin/export", data={"csrf": csrf}, follow_redirects=False)
    assert r.status_code == 303 and r.headers.get("content-type") != "application/zip"


def test_pas_de_documentation_api(client):
    for chemin in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(chemin).status_code == 404


# --- CSRF ---

def test_csrf_obligatoire(client):
    r = client.post("/acces", data={"code": CODE})
    assert r.status_code == 403
    r = client.post("/acces", data={"code": CODE, "csrf": "jeton-invente"})
    assert r.status_code == 403
    r = client.post("/acces", data={"code": CODE, "csrf": "jéton-àccentué"})
    assert r.status_code == 403  # pas d'erreur 500


def test_csrf_actions_admin(client):
    connecter_ami(client)
    client.post("/citation", data={"csrf": jeton_csrf(client, "/citation"), "citation": "c", "personne": "p"})
    connecter_admin(client)
    [s] = db.lister_soumissions()
    for action in ("supprimer", "retenir", "modifier"):
        r = client.post(f"/admin/soumission/{s['id']}/{action}", data={"citation": "x"})
        assert r.status_code == 403
    assert client.post("/admin/export", data={}).status_code == 403
    assert db.obtenir_soumission(s["id"])["citation"] == "c"


def test_csrf_jeton_dune_autre_session(client, parametres):
    """Le jeton CSRF d'une autre session (celle de l'attaquant) est refusé."""
    with TestClient(creer_app(parametres)) as attaquant:
        jeton_attaquant = jeton_csrf(attaquant, "/acces")
    connecter_ami(client)
    r = client.post("/citation", data={"csrf": jeton_attaquant, "citation": "c", "personne": "p"})
    assert r.status_code == 403


# --- En-têtes, erreurs, limitation de débit ---

def test_entetes_de_securite(client):
    r = client.get("/acces")
    h = r.headers
    assert "default-src 'self'" in h["content-security-policy"] and "frame-ancestors 'none'" in h["content-security-policy"]
    assert h["x-content-type-options"] == "nosniff"
    assert h["x-frame-options"] == "DENY"
    assert h["referrer-policy"] == "no-referrer"
    assert h["cache-control"] == "no-store"
    assert "server" not in h


def test_hsts_et_cookie_securise_en_production(tmp_path):
    parametres = creer_parametres(tmp_path / "data", environnement="production", cookies_securises=False)
    assert parametres.cookies_securises is True  # impossible de désactiver Secure en production
    with TestClient(creer_app(parametres), base_url="https://testserver") as c:
        r = c.get("/acces")
        assert "max-age=31536000" in r.headers["strict-transport-security"]
        cookie = r.headers["set-cookie"]
        assert cookie.startswith("__Host-kf_session=")
        for attribut in ("Secure", "HttpOnly", "SameSite=Lax", "Path=/"):
            assert attribut in cookie


def test_aucune_trace_derreur(client, monkeypatch):
    def plante(*_a, **_k):
        raise RuntimeError("secret interne /app/data/kahoot.db")

    monkeypatch.setattr(db, "lister_auteurs", plante)
    connecter_admin(client)
    with TestClient(client.app, raise_server_exceptions=False) as c:
        c.cookies.update(client.cookies)
        r = c.get("/admin")
    assert r.status_code == 500
    assert "Traceback" not in r.text and "secret interne" not in r.text and "kahoot.db" not in r.text


def test_erreur_validation_sans_details(client):
    connecter_admin(client)
    r = client.post("/admin/soumission/pas-un-nombre/modifier", data={"csrf": jeton_csrf(client, "/admin")})
    assert r.status_code == 400 and "pydantic" not in r.text.lower() and "int_parsing" not in r.text


def test_limitation_de_debit(client):
    client.app.state.limiteur.reinitialiser()
    statuts = [client.post("/acces", data={}).status_code for _ in range(125)]
    assert statuts[0] == 403 and statuts[-1] == 429
