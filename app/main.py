"""Point d'entrée de l'application Kahoot des fêtes."""

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import db, routes_admin, routes_public, securite
from .config import Parametres, obtenir_parametres

DOSSIER_APP = Path(__file__).parent
journal = logging.getLogger("kahoot")

MESSAGES_ERREUR = {
    400: "Requête invalide.",
    401: "Accès refusé.",
    403: "Action refusée. Rechargez la page et réessayez.",
    404: "Page introuvable.",
    405: "Action non permise.",
    413: "Fichier trop volumineux.",
    429: "Trop de requêtes. Patientez un peu.",
}


def creer_app(parametres: Parametres | None = None) -> FastAPI:
    parametres = parametres or obtenir_parametres()

    @asynccontextmanager
    async def cycle_de_vie(app: FastAPI):
        parametres.dossier_photos.mkdir(parents=True, exist_ok=True)
        parametres.dossier_miniatures.mkdir(parents=True, exist_ok=True)
        db.initialiser(parametres.chemin_bd)
        securite.purger_sessions_si_secrets_changes(parametres)
        # Exports interrompus : on ne laisse pas traîner de copies des photos
        dossier_tmp = parametres.dossier_donnees / "tmp"
        if dossier_tmp.is_dir():
            for fichier in dossier_tmp.glob("*.zip"):
                fichier.unlink(missing_ok=True)
        yield

    # Pas de documentation automatique ni de mode debug : rien d'interne n'est exposé
    app = FastAPI(debug=False, docs_url=None, redoc_url=None, openapi_url=None, lifespan=cycle_de_vie)
    app.state.parametres = parametres
    app.state.gabarits = Jinja2Templates(directory=DOSSIER_APP / "templates")
    app.state.limiteur = securite.LimiteurDebit()
    app.state.anti_force_brute_code = securite.AntiForceBrute()
    app.state.anti_force_brute_admin = securite.AntiForceBrute(essais_globaux=20)

    # Ordre d'exécution : sécurité → limite de taille → session → routes
    app.add_middleware(securite.MiddlewareSession, parametres=parametres)
    app.add_middleware(securite.MiddlewareLimiteCorps, limite=parametres.taille_max_requete)
    app.add_middleware(securite.MiddlewareSecurite, parametres=parametres, limiteur=app.state.limiteur)

    app.mount("/static", StaticFiles(directory=DOSSIER_APP / "static"), name="static")
    app.include_router(routes_public.routeur)
    app.include_router(routes_admin.routeur)

    @app.get("/health", include_in_schema=False)
    def sante():
        with db.connexion() as cx:
            cx.execute("SELECT 1")
        return {"statut": "ok"}

    def page_erreur(request: Request, statut: int, message: str):
        return app.state.gabarits.TemplateResponse(
            request, "erreur.html", {"message": message, "statut": statut}, status_code=statut
        )

    @app.exception_handler(StarletteHTTPException)
    async def erreur_http(request: Request, exc: StarletteHTTPException):
        message = MESSAGES_ERREUR.get(exc.status_code, "Une erreur est survenue.")
        # Seuls nos propres messages en français sont affichés, jamais les détails techniques
        if exc.status_code == 403 and isinstance(exc.detail, str) and exc.detail.startswith("Jeton"):
            message = exc.detail
        if request.headers.get("x-requested-with") == "fetch":
            return JSONResponse({"ok": False, "erreurs": [message]}, status_code=exc.status_code)
        return page_erreur(request, exc.status_code, message)

    @app.exception_handler(RequestValidationError)
    async def erreur_validation(request: Request, exc: RequestValidationError):
        return page_erreur(request, 400, MESSAGES_ERREUR[400])

    @app.exception_handler(Exception)
    async def erreur_interne(request: Request, exc: Exception):
        journal.exception("Erreur non gérée sur %s", request.url.path)
        return page_erreur(request, 500, "Une erreur est survenue. Réessayez plus tard.")

    return app


def app_factory() -> FastAPI:
    """Utilisée par uvicorn : `uvicorn app.main:app_factory --factory`."""
    return creer_app()
