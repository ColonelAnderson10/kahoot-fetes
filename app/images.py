"""Vérification et normalisation des photos téléversées."""

import uuid
from pathlib import Path
from typing import BinaryIO

import pillow_heif
from PIL import Image, ImageOps

pillow_heif.register_heif_opener()

# Protection contre les bombes de décompression : 50 mégapixels maximum
# (un téléphone récent produit au plus ~48 Mpx). Pillow lève une erreur au-delà du double.
PIXELS_MAX = 50_000_000
Image.MAX_IMAGE_PIXELS = PIXELS_MAX

COTE_MAX = 1920
COTE_MINIATURE = 480
QUALITE_JPEG = 85

# Marques ISO-BMFF acceptées pour HEIC/HEIF
MARQUES_HEIF = {b"heic", b"heix", b"hevc", b"hevx", b"heim", b"heis", b"mif1", b"msf1"}


class ImageInvalide(ValueError):
    """Le fichier n'est pas une image acceptée ou ne peut pas être traité."""


def detecter_format(entete: bytes) -> str | None:
    """Détermine le vrai format à partir des octets de début (signature), jamais de l'extension."""
    if entete.startswith(b"\xff\xd8\xff"):
        return "JPEG"
    if entete.startswith(b"\x89PNG\r\n\x1a\n"):
        return "PNG"
    if entete[:4] == b"RIFF" and entete[8:12] == b"WEBP":
        return "WEBP"
    if entete[4:8] == b"ftyp" and entete[8:12] in MARQUES_HEIF:
        return "HEIF"
    return None


def traiter_photo(source: BinaryIO, dossier_photos: Path, dossier_miniatures: Path) -> str:
    """Valide, normalise et enregistre une photo. Renvoie le nom du fichier créé (UUID.jpg).

    - vérifie la signature du fichier ;
    - refuse les images trop grandes avant de les décoder ;
    - corrige l'orientation EXIF ;
    - convertit en JPEG 1920 px max, qualité 85, sans aucune métadonnée.
    """
    source.seek(0)
    entete = source.read(32)
    source.seek(0)
    format_detecte = detecter_format(entete)
    if format_detecte is None:
        raise ImageInvalide("Format non accepté (JPEG, PNG, WEBP ou HEIC seulement).")

    try:
        with Image.open(source, formats=[format_detecte]) as img:
            largeur, hauteur = img.size
            if largeur * hauteur > PIXELS_MAX or largeur > 20_000 or hauteur > 20_000:
                raise ImageInvalide("Image trop grande.")
            img.load()
            img = ImageOps.exif_transpose(img)
            img = _vers_rgb(img)
            img.thumbnail((COTE_MAX, COTE_MAX), Image.Resampling.LANCZOS)

            # Nouvelle image vierge : aucune métadonnée (EXIF, GPS, XMP, ICC, commentaires) n'est recopiée
            propre = Image.new("RGB", img.size)
            propre.paste(img)
    except ImageInvalide:
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise ImageInvalide("Image trop grande.") from None
    except Exception:
        raise ImageInvalide("Fichier image illisible ou corrompu.") from None

    nom = f"{uuid.uuid4().hex}.jpg"
    dossier_photos.mkdir(parents=True, exist_ok=True)
    dossier_miniatures.mkdir(parents=True, exist_ok=True)
    propre.save(dossier_photos / nom, "JPEG", quality=QUALITE_JPEG, optimize=True)

    miniature = propre.copy()
    miniature.thumbnail((COTE_MINIATURE, COTE_MINIATURE), Image.Resampling.LANCZOS)
    miniature.save(dossier_miniatures / nom, "JPEG", quality=80)
    return nom


def _vers_rgb(img: Image.Image) -> Image.Image:
    """Convertit en RGB ; la transparence est remplacée par un fond blanc."""
    if img.mode in ("RGBA", "LA", "PA") or (img.mode == "P" and "transparency" in img.info):
        img = img.convert("RGBA")
        fond = Image.new("RGB", img.size, (255, 255, 255))
        fond.paste(img, mask=img.getchannel("A"))
        return fond
    return img.convert("RGB")
