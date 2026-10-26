"""Export des éléments retenus : un ZIP avec les photos renommées et un CSV."""

import csv
import io
import re
import unicodedata
import zipfile
from pathlib import Path

from . import db

CARACTERES_DANGEREUX = ("=", "+", "-", "@", "\t", "\r", "\n")


def neutraliser_cellule(valeur: str) -> str:
    """Empêche l'injection de formules à l'ouverture du CSV dans Excel ou LibreOffice."""
    valeur = valeur or ""
    if valeur.startswith(CARACTERES_DANGEREUX):
        return "'" + valeur
    return valeur


def nom_fichier_sur(prenom: str) -> str:
    """Prénom réduit à des caractères ASCII sûrs pour un nom de fichier (pas de / ni de ..)."""
    ascii_ = unicodedata.normalize("NFKD", prenom).encode("ascii", "ignore").decode("ascii").lower()
    ascii_ = re.sub(r"[^a-z0-9]+", "-", ascii_).strip("-")
    return ascii_[:30] or "anonyme"


def creer_export(dossier_photos: Path, destination: Path) -> None:
    """Écrit le ZIP d'export dans `destination`."""
    lignes = []
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for numero, s in enumerate(db.lister_retenus(), start=1):
            nom_zip = ""
            if s["type"] == "photo" and s["fichier"]:
                source = dossier_photos / s["fichier"]
                if source.is_file():
                    nom_zip = f"{numero:03d}_{nom_fichier_sur(s['auteur'])}.jpg"
                    zf.write(source, arcname=f"photos/{nom_zip}")
            texte = s["description"] if s["type"] == "photo" else s["citation"]
            if s["type"] == "citation" and s["contexte"]:
                texte = f"{texte} (contexte : {s['contexte']})"
            lignes.append([
                str(numero),
                s["type"],
                s["auteur"],
                s["personne_citee"],
                texte,
                nom_zip,
            ])

        tampon = io.StringIO()
        # Point-virgule : séparateur attendu par Excel en français
        ecrivain = csv.writer(tampon, delimiter=";", quoting=csv.QUOTE_ALL)
        ecrivain.writerow(["numero", "type", "auteur_envoi", "personne_citee", "texte_ou_description", "fichier"])
        for ligne in lignes:
            ecrivain.writerow([neutraliser_cellule(c) for c in ligne])
        # BOM UTF-8 pour qu'Excel affiche correctement les accents
        zf.writestr("kahoot.csv", "﻿" + tampon.getvalue())
