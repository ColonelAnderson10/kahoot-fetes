"""Configuration de l'application, lue depuis les variables d'environnement ou le fichier .env."""

from functools import lru_cache
from pathlib import Path

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Valeurs triviales refusées pour les secrets
SECRETS_INTERDITS = {
    "password", "motdepasse", "mot-de-passe", "12345678", "123456789", "1234567890",
    "azertyuiop", "qwertyuiop", "changeme", "admin1234", "kahoot123", "noel2026",
}


def _verifier_secret(valeur: str, nom: str, longueur_min: int) -> str:
    valeur = valeur.strip()
    if len(valeur) < longueur_min:
        raise ValueError(f"{nom} doit contenir au moins {longueur_min} caractères.")
    if "remplacer" in valeur.lower() or valeur.lower() in SECRETS_INTERDITS:
        raise ValueError(f"{nom} est une valeur par défaut ou trop faible.")
    if len(set(valeur)) < 5:
        raise ValueError(f"{nom} est trop répétitif.")
    return valeur


class Parametres(BaseSettings):
    # hide_input_in_errors : un secret refusé n'apparaît jamais dans les journaux
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", hide_input_in_errors=True
    )

    code_acces: str
    admin_mot_de_passe: str
    environnement: str = "production"
    dossier_donnees: Path = Path("data")
    taille_max_photo_mo: int = 25
    session_ami_jours: int = 30
    session_admin_minutes: int = 120
    # Les cookies Secure ne sont désactivables qu'en développement (HTTP local)
    cookies_securises: bool = True

    @field_validator("code_acces")
    @classmethod
    def _code(cls, v: str) -> str:
        return _verifier_secret(v, "CODE_ACCES", 8)

    @field_validator("admin_mot_de_passe")
    @classmethod
    def _admin(cls, v: str) -> str:
        return _verifier_secret(v, "ADMIN_MOT_DE_PASSE", 12)

    @field_validator("environnement")
    @classmethod
    def _env(cls, v: str) -> str:
        v = v.strip().lower()
        if v not in {"production", "developpement"}:
            raise ValueError("ENVIRONNEMENT doit valoir « production » ou « developpement ».")
        return v

    @field_validator("taille_max_photo_mo")
    @classmethod
    def _taille(cls, v: int) -> int:
        if not 1 <= v <= 100:
            raise ValueError("TAILLE_MAX_PHOTO_MO doit être entre 1 et 100.")
        return v

    @model_validator(mode="after")
    def _coherence(self) -> "Parametres":
        if self.code_acces == self.admin_mot_de_passe:
            raise ValueError("ADMIN_MOT_DE_PASSE doit être différent de CODE_ACCES.")
        if self.environnement == "production":
            self.cookies_securises = True
        return self

    @property
    def production(self) -> bool:
        return self.environnement == "production"

    @property
    def taille_max_photo(self) -> int:
        return self.taille_max_photo_mo * 1024 * 1024

    @property
    def taille_max_requete(self) -> int:
        # Une photo par requête + une marge pour les champs texte
        return self.taille_max_photo + 1024 * 1024

    @property
    def dossier_photos(self) -> Path:
        return self.dossier_donnees / "uploads"

    @property
    def dossier_miniatures(self) -> Path:
        return self.dossier_donnees / "uploads" / "miniatures"

    @property
    def chemin_bd(self) -> Path:
        return self.dossier_donnees / "kahoot.db"


@lru_cache
def obtenir_parametres() -> Parametres:
    return Parametres()
