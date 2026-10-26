"""Tests fonctionnels : envois, accès, administration, export."""

import csv
import io
import zipfile

import pillow_heif
import pytest
from fastapi.testclient import TestClient
from PIL import Image
from pydantic import ValidationError

from app import db
from app.main import creer_app

from .conftest import (
    ADMIN, CODE, connecter_admin, connecter_ami, creer_parametres, envoyer_photo, exif_avec_gps,
    image_octets, jeton_csrf,
)


def _photos_enregistrees(parametres):
    return sorted(p for p in parametres.dossier_photos.glob("*.jpg"))


# --- Envoi de photos ---

def test_envoi_jpeg_converti_redimensionne_sans_exif(client, parametres):
    connecter_ami(client)
    contenu = image_octets("JPEG", (4000, 3000), exif_avec_gps())
    r = envoyer_photo(client, contenu, description="Au chalet")
    assert r.status_code == 200 and r.json()["enregistrees"] == 1

    [fichier] = _photos_enregistrees(parametres)
    assert len(fichier.stem) == 32  # UUID, jamais le nom d'origine
    with Image.open(fichier) as img:
        assert img.format == "JPEG"
        # Orientation 6 corrigée : l'image 4000x3000 devient verticale, réduite à 1920 px
        assert img.size == (1440, 1920)
        assert not img.getexif()
        assert "exif" not in img.info

    [s] = db.lister_soumissions()
    assert s["auteur"] == "Léa" and s["description"] == "Au chalet" and s["type"] == "photo"


def test_envoi_heic_converti_en_jpeg_sans_gps(client, parametres):
    pillow_heif.register_heif_opener()
    tampon = io.BytesIO()
    Image.new("RGB", (640, 480), "green").save(tampon, "HEIF", exif=exif_avec_gps().tobytes())
    connecter_ami(client)
    r = envoyer_photo(client, tampon.getvalue(), nom="IMG_0001.HEIC", type_mime="image/heic")
    assert r.status_code == 200, r.text

    [fichier] = _photos_enregistrees(parametres)
    contenu = fichier.read_bytes()
    assert contenu.startswith(b"\xff\xd8\xff")
    assert b"Exif" not in contenu and b"MarqueSecrete" not in contenu
    with Image.open(fichier) as img:
        assert img.format == "JPEG" and not img.getexif()


@pytest.mark.parametrize("format_", ["PNG", "WEBP"])
def test_envoi_png_webp(client, parametres, format_):
    connecter_ami(client)
    contenu = image_octets(format_, (300, 200), mode="RGBA")
    r = envoyer_photo(client, contenu, nom=f"x.{format_.lower()}", type_mime=f"image/{format_.lower()}")
    assert r.status_code == 200
    [fichier] = _photos_enregistrees(parametres)
    with Image.open(fichier) as img:
        assert img.format == "JPEG" and img.mode == "RGB"


def test_envoi_sans_js_redirige_vers_merci(client):
    connecter_ami(client)
    r = envoyer_photo(client, image_octets(), js=False)
    assert r.status_code == 303 and r.headers["location"] == "/merci?photos=1"
    assert "1 photo bien reçue" in client.get("/merci?photos=1").text


def test_miniature_creee(client, parametres):
    connecter_ami(client)
    envoyer_photo(client, image_octets("JPEG", (2000, 1000)))
    [fichier] = _photos_enregistrees(parametres)
    with Image.open(parametres.dossier_miniatures / fichier.name) as mini:
        assert max(mini.size) == 480


def test_envois_simultanes(client, parametres):
    """Le navigateur envoie jusqu'à 3 photos en parallèle : toutes doivent être enregistrées."""
    from concurrent.futures import ThreadPoolExecutor

    connecter_ami(client)
    csrf = jeton_csrf(client, "/photos")

    def envoyer(i):
        return client.post(
            "/photos",
            data={"csrf": csrf, "descriptions": f"photo {i}"},
            files={"photos": (f"{i}.jpg", image_octets(taille=(1200, 900)), "image/jpeg")},
            headers={"X-Requested-With": "fetch"},
        ).status_code

    with ThreadPoolExecutor(max_workers=3) as executeur:
        statuts = list(executeur.map(envoyer, range(9)))
    assert statuts == [200] * 9
    assert len(_photos_enregistrees(parametres)) == 9
    assert sorted(s["description"] for s in db.lister_soumissions()) == sorted(f"photo {i}" for i in range(9))


def test_limite_photos_plus_haute_que_les_autres_formulaires(client):
    """Les envois de photos ont leur propre limite (300/min) : 150 envois rapides ne sont pas bloqués."""
    client.app.state.limiteur.reinitialiser()
    # Sans jeton CSRF : réponses 403 rapides, mais chaque requête compte pour la limite
    statuts = {client.post("/photos", data={}, follow_redirects=False).status_code for _ in range(150)}
    assert statuts == {303}  # pas encore connecté → redirection, jamais 429
    statuts = [client.post("/acces", data={}).status_code for _ in range(125)]
    assert statuts[-1] == 429  # les autres formulaires restent limités à 120/min


# --- Citations ---

def test_envoi_citation(client):
    connecter_ami(client, "Marc")
    r = client.post("/citation", data={
        "csrf": jeton_csrf(client, "/citation"),
        "citation": "Je conduis mieux après deux bières.",
        "personne": "Oncle Gilles",
        "contexte": "Réveillon 2025",
    }, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/merci?citation=1"
    [s] = db.lister_soumissions()
    assert (s["type"], s["auteur"], s["personne_citee"], s["contexte"]) == ("citation", "Marc", "Oncle Gilles", "Réveillon 2025")


def test_citation_incomplete_refusee(client):
    connecter_ami(client)
    r = client.post("/citation", data={"csrf": jeton_csrf(client, "/citation"), "citation": "", "personne": "X"})
    assert r.status_code == 400
    assert db.lister_soumissions() == []


# --- Accès ---

def test_acces_refuse_sans_code(client):
    for url in ("/", "/photos", "/citation", "/prenom", "/merci"):
        r = client.get(url, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/acces", url


def test_envoi_refuse_sans_code(client):
    r = client.post("/citation", data={"csrf": jeton_csrf(client, "/acces"), "citation": "x", "personne": "y"},
                    follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/acces"
    r = envoyer_photo(client, image_octets())
    assert r.status_code == 401
    assert db.lister_soumissions() == []


def test_mauvais_code(client):
    r = client.post("/acces", data={"csrf": jeton_csrf(client, "/acces"), "code": "mauvais-code"})
    assert r.status_code == 401 and "Code incorrect" in r.text


def test_prenom_memorise(client):
    connecter_ami(client, "Sophie")
    assert "Salut Sophie" in client.get("/").text
    assert "Salut Sophie" in client.get("/photos").text


def test_admin_refuse_sans_mot_de_passe(client):
    connecter_ami(client)  # même un ami connecté n'a pas accès à l'admin
    r = client.get("/admin")
    assert "Mot de passe admin" in r.text and "Soumissions" not in r.text
    r = client.post("/admin/connexion", data={"csrf": jeton_csrf(client, "/admin"), "mot_de_passe": CODE})
    assert r.status_code == 401


# --- Administration ---

def _preparer_contenu(client):
    connecter_ami(client, "Léa")
    envoyer_photo(client, image_octets(), description="Photo 1")
    envoyer_photo(client, image_octets(), description="Photo 2")
    client.post("/citation", data={"csrf": jeton_csrf(client, "/citation"), "citation": "Oups", "personne": "Paul"})
    connecter_admin(client)


def test_admin_galerie_filtres(client):
    _preparer_contenu(client)
    r = client.get("/admin")
    assert r.status_code == 200 and "Photo 1" in r.text and "Oups" in r.text
    r = client.get("/admin?type=citation")
    assert "Oups" in r.text and "Photo 1" not in r.text
    r = client.get("/admin?auteur=Personne")
    assert "Aucune soumission" in r.text


def test_admin_modifier_retenir_supprimer(client, parametres):
    _preparer_contenu(client)
    photo = next(s for s in db.lister_soumissions() if s["type"] == "photo")
    csrf = jeton_csrf(client, "/admin")

    client.post(f"/admin/soumission/{photo['id']}/modifier", data={"csrf": csrf, "description": "Nouvelle"})
    client.post(f"/admin/soumission/{photo['id']}/retenir", data={"csrf": csrf})
    s = db.obtenir_soumission(photo["id"])
    assert s["description"] == "Nouvelle" and s["retenu"] == 1

    assert (parametres.dossier_photos / photo["fichier"]).exists()
    client.post(f"/admin/soumission/{photo['id']}/supprimer", data={"csrf": csrf})
    assert db.obtenir_soumission(photo["id"]) is None
    assert not (parametres.dossier_photos / photo["fichier"]).exists()
    assert not (parametres.dossier_miniatures / photo["fichier"]).exists()


def test_admin_voit_les_photos(client):
    _preparer_contenu(client)
    photo = next(s for s in db.lister_soumissions() if s["type"] == "photo")
    r = client.get(f"/media/{photo['fichier']}")
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
    assert client.get(f"/media/{photo['fichier']}?mini=true").status_code == 200


def test_export_zip_csv(client):
    _preparer_contenu(client)
    csrf = jeton_csrf(client, "/admin")
    for s in db.lister_soumissions():
        client.post(f"/admin/soumission/{s['id']}/retenir", data={"csrf": csrf})

    r = client.post("/admin/export", data={"csrf": csrf})
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
        noms = sorted(zf.namelist())
        assert noms == ["kahoot.csv", "photos/001_lea.jpg", "photos/002_lea.jpg"]
        lignes = list(csv.reader(io.StringIO(zf.read("kahoot.csv").decode("utf-8-sig")), delimiter=";"))
    assert lignes[0] == ["numero", "type", "auteur_envoi", "personne_citee", "texte_ou_description", "fichier"]
    assert lignes[1] == ["1", "photo", "Léa", "", "Photo 1", "001_lea.jpg"]
    assert lignes[3] == ["3", "citation", "Léa", "Paul", "Oups", ""]


# --- Configuration et persistance ---

@pytest.mark.parametrize("code, admin", [
    ("court", ADMIN),                                   # code trop court
    (CODE, "court"),                                    # mot de passe admin trop court
    ("a-remplacer-code-amis", ADMIN),                   # valeur de .env.example
    (CODE, "a-remplacer-mot-de-passe-admin"),           # valeur de .env.example
    ("12345678", ADMIN),                                # valeur triviale
    ("aaaaaaaaaaaa", ADMIN),                            # trop répétitif
    ("", ADMIN),                                        # absent
])
def test_secrets_faibles_refuses(tmp_path, code, admin):
    with pytest.raises(ValidationError):
        creer_parametres(tmp_path, code_acces=code, admin_mot_de_passe=admin)


def test_secret_refuse_absent_du_message_derreur(tmp_path):
    with pytest.raises(ValidationError) as erreur:
        creer_parametres(tmp_path, admin_mot_de_passe="Secret-42")  # trop court : moins de 12 caractères
    assert "Secret-42" not in str(erreur.value)


def test_secrets_valides_acceptes(tmp_path):
    assert creer_parametres(tmp_path).code_acces == CODE


def test_admin_identique_au_code_refuse(tmp_path):
    with pytest.raises(ValidationError):
        creer_parametres(tmp_path, code_acces="memesecret123", admin_mot_de_passe="memesecret123")


def test_donnees_conservees_apres_redemarrage(parametres):
    """Simule un redémarrage : une nouvelle instance relit le même dossier data/."""
    with TestClient(creer_app(parametres)) as c:
        connecter_ami(c)
        envoyer_photo(c, image_octets())
    with TestClient(creer_app(parametres)) as c:
        connecter_admin(c)
        [s] = db.lister_soumissions()
        assert c.get(f"/media/{s['fichier']}").status_code == 200


def test_health(client):
    assert client.get("/health").json() == {"statut": "ok"}
