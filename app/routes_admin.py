"""Page d'administration : galerie, modification, suppression, sélection et export."""

import os
import tempfile
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode

from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

from . import db, securite
from .export import creer_export
from .routes_public import (
    LONGUEUR_CITATION, LONGUEUR_CONTEXTE, LONGUEUR_DESCRIPTION, LONGUEUR_PERSONNE,
    nettoyer, rediriger, rendre,
)

routeur = APIRouter(prefix="/admin")


def _filtres_url(formulaire) -> str:
    """Revient à la galerie avec les mêmes filtres (valeurs ré-encodées, jamais recopiées telles quelles)."""
    params = {}
    auteur = formulaire.get("filtre_auteur")
    type_ = formulaire.get("filtre_type")
    if isinstance(auteur, str) and auteur:
        params["auteur"] = auteur[:40]
    if type_ in ("photo", "citation"):
        params["type"] = type_
    return "/admin" + (f"?{urlencode(params)}" if params else "")


@routeur.get("")
async def galerie(request: Request, auteur: str = "", type: str = ""):
    if not securite.est_admin(request):
        return rendre(request, "admin_connexion.html")
    securite.prolonger_admin(request)
    type_ = type if type in ("photo", "citation") else ""
    auteur = auteur[:40]
    soumissions = db.lister_soumissions(auteur or None, type_ or None)
    return rendre(
        request, "admin.html",
        soumissions=soumissions, auteurs=db.lister_auteurs(),
        filtre_auteur=auteur, filtre_type=type_,
        nb_retenus=len(db.lister_retenus()),
    )


@routeur.post("/connexion")
async def connexion(request: Request):
    formulaire = await securite.lire_formulaire(request, max_fichiers=0)
    ip = securite.ip_client(request)
    garde = request.app.state.anti_force_brute_admin
    if garde.bloque(ip):
        return rendre(request, "admin_connexion.html", 429, erreur="Trop d'essais. Réessayez dans 15 minutes.")
    mot_de_passe = formulaire.get("mot_de_passe")
    attendu = request.app.state.parametres.admin_mot_de_passe
    if not isinstance(mot_de_passe, str) or not securite.comparer_secret(mot_de_passe, attendu):
        garde.echec(ip)
        return rendre(request, "admin_connexion.html", 401, erreur="Mot de passe incorrect.")
    garde.succes(ip)
    securite.regenerer_session(request, admin=True)
    return rediriger("/admin")


@routeur.post("/deconnexion")
async def deconnexion(request: Request):
    await securite.lire_formulaire(request, max_fichiers=0)
    securite.regenerer_session(request, admin=False)
    return rediriger("/admin")


@routeur.post("/soumission/{id_}/modifier")
async def modifier(request: Request, id_: int):
    if not securite.est_admin(request):
        return rediriger("/admin")
    formulaire = await securite.lire_formulaire(request, max_fichiers=0)
    s = db.obtenir_soumission(id_)
    if s is not None:
        db.modifier_soumission(
            id_,
            nettoyer(formulaire.get("description", s["description"]), LONGUEUR_DESCRIPTION, True),
            nettoyer(formulaire.get("citation", s["citation"]), LONGUEUR_CITATION, True),
            nettoyer(formulaire.get("personne", s["personne_citee"]), LONGUEUR_PERSONNE),
            nettoyer(formulaire.get("contexte", s["contexte"]), LONGUEUR_CONTEXTE, True),
        )
    return rediriger(_filtres_url(formulaire) + f"#s{id_}")


@routeur.post("/soumission/{id_}/retenir")
async def retenir(request: Request, id_: int):
    if not securite.est_admin(request):
        return rediriger("/admin")
    formulaire = await securite.lire_formulaire(request, max_fichiers=0)
    db.basculer_retenu(id_)
    return rediriger(_filtres_url(formulaire) + f"#s{id_}")


@routeur.post("/soumission/{id_}/supprimer")
async def supprimer(request: Request, id_: int):
    if not securite.est_admin(request):
        return rediriger("/admin")
    formulaire = await securite.lire_formulaire(request, max_fichiers=0)
    fichier = db.supprimer_soumission(id_)
    if fichier:
        parametres = request.app.state.parametres
        for dossier in (parametres.dossier_photos, parametres.dossier_miniatures):
            (dossier / fichier).unlink(missing_ok=True)
    return rediriger(_filtres_url(formulaire))


@routeur.post("/export")
async def exporter(request: Request):
    if not securite.est_admin(request):
        return rediriger("/admin")
    await securite.lire_formulaire(request, max_fichiers=0)
    parametres = request.app.state.parametres
    dossier_tmp = parametres.dossier_donnees / "tmp"
    dossier_tmp.mkdir(parents=True, exist_ok=True)
    descripteur, chemin = tempfile.mkstemp(suffix=".zip", dir=dossier_tmp)
    os.close(descripteur)
    await run_in_threadpool(creer_export, parametres.dossier_photos, Path(chemin))
    nom = f"kahoot-fetes-{datetime.now():%Y%m%d-%H%M}.zip"
    return FileResponse(chemin, media_type="application/zip", filename=nom,
                        background=BackgroundTask(os.remove, chemin))
