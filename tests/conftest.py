"""Outils communs aux tests."""

import io
import re

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app.config import Parametres
from app.main import creer_app

CODE = "codeamis2026"
ADMIN = "AdminSecret!2026x"


def creer_parametres(dossier, **autres) -> Parametres:
    valeurs = dict(
        code_acces=CODE,
        admin_mot_de_passe=ADMIN,
        environnement="developpement",
        cookies_securises=False,
        dossier_donnees=dossier,
    )
    valeurs.update(autres)
    return Parametres(_env_file=None, **valeurs)


@pytest.fixture
def parametres(tmp_path):
    return creer_parametres(tmp_path / "data")


@pytest.fixture
def client(parametres):
    with TestClient(creer_app(parametres)) as c:
        yield c


def jeton_csrf(client: TestClient, url: str) -> str:
    page = client.get(url)
    trouve = re.search(r'name="csrf" value="([^"]+)"', page.text)
    assert trouve, f"Aucun jeton CSRF sur {url}"
    return trouve.group(1)


def connecter_ami(client: TestClient, prenom: str = "Léa") -> None:
    r = client.post("/acces", data={"csrf": jeton_csrf(client, "/acces"), "code": CODE}, follow_redirects=False)
    assert r.status_code == 303
    r = client.post("/prenom", data={"csrf": jeton_csrf(client, "/prenom"), "prenom": prenom}, follow_redirects=False)
    assert r.status_code == 303


def connecter_admin(client: TestClient) -> None:
    r = client.post("/admin/connexion", data={"csrf": jeton_csrf(client, "/admin"), "mot_de_passe": ADMIN},
                    follow_redirects=False)
    assert r.status_code == 303


def image_octets(format_: str = "JPEG", taille=(800, 600), exif: Image.Exif | None = None, mode="RGB") -> bytes:
    img = Image.new(mode, taille, "red" if mode == "RGB" else None)
    tampon = io.BytesIO()
    options = {"exif": exif.tobytes()} if exif is not None else {}
    img.save(tampon, format_, **options)
    return tampon.getvalue()


def exif_avec_gps() -> Image.Exif:
    exif = Image.Exif()
    exif[0x010F] = "MarqueSecrete"   # Make
    exif[0x0110] = "ModeleSecret"    # Model
    exif[0x0112] = 6                 # Orientation : rotation de 90°
    gps = {1: "N", 2: (46.0, 48.0, 50.0), 3: "W", 4: (71.0, 12.0, 30.0)}
    exif[0x8825] = gps               # GPSInfo
    return exif


def envoyer_photo(client: TestClient, contenu: bytes, nom="photo.jpg", description="", type_mime="image/jpeg", js=True):
    entetes = {"X-Requested-With": "fetch"} if js else {}
    return client.post(
        "/photos",
        data={"csrf": jeton_csrf(client, "/photos"), "descriptions": description},
        files={"photos": (nom, contenu, type_mime)},
        headers=entetes,
        follow_redirects=False,
    )
