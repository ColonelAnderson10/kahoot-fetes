"""Accès à la base SQLite. Toutes les requêtes sont paramétrées."""

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    jeton_hash      TEXT PRIMARY KEY,
    csrf            TEXT NOT NULL,
    prenom          TEXT,
    acces_ami       INTEGER NOT NULL DEFAULT 0,
    admin           INTEGER NOT NULL DEFAULT 0,
    expire_le       REAL NOT NULL,
    admin_expire_le REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS soumissions (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    type           TEXT NOT NULL CHECK (type IN ('photo', 'citation')),
    auteur         TEXT NOT NULL,
    fichier        TEXT UNIQUE,
    description    TEXT NOT NULL DEFAULT '',
    citation       TEXT NOT NULL DEFAULT '',
    personne_citee TEXT NOT NULL DEFAULT '',
    contexte       TEXT NOT NULL DEFAULT '',
    retenu         INTEGER NOT NULL DEFAULT 0,
    cree_le        TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_soumissions_auteur ON soumissions (auteur);

CREATE TABLE IF NOT EXISTS meta (
    cle    TEXT PRIMARY KEY,
    valeur TEXT NOT NULL
);
"""

_chemin_bd: Path | None = None


def initialiser(chemin: Path) -> None:
    global _chemin_bd
    _chemin_bd = chemin
    chemin.parent.mkdir(parents=True, exist_ok=True)
    with connexion() as cx:
        cx.executescript(SCHEMA)


@contextmanager
def connexion() -> Iterator[sqlite3.Connection]:
    if _chemin_bd is None:
        raise RuntimeError("Base de données non initialisée.")
    cx = sqlite3.connect(_chemin_bd, timeout=10)
    cx.row_factory = sqlite3.Row
    cx.execute("PRAGMA foreign_keys = ON")
    cx.execute("PRAGMA journal_mode = WAL")
    try:
        yield cx
        cx.commit()
    finally:
        cx.close()


def maintenant_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --- Soumissions ---

def ajouter_photo(auteur: str, fichier: str, description: str) -> int:
    with connexion() as cx:
        cur = cx.execute(
            "INSERT INTO soumissions (type, auteur, fichier, description, cree_le) VALUES ('photo', ?, ?, ?, ?)",
            (auteur, fichier, description, maintenant_iso()),
        )
        return cur.lastrowid


def ajouter_citation(auteur: str, citation: str, personne: str, contexte: str) -> int:
    with connexion() as cx:
        cur = cx.execute(
            "INSERT INTO soumissions (type, auteur, citation, personne_citee, contexte, cree_le) "
            "VALUES ('citation', ?, ?, ?, ?, ?)",
            (auteur, citation, personne, contexte, maintenant_iso()),
        )
        return cur.lastrowid


def lister_soumissions(auteur: str | None = None, type_: str | None = None) -> list[sqlite3.Row]:
    requete = "SELECT * FROM soumissions WHERE 1=1"
    params: list[str] = []
    if auteur:
        requete += " AND auteur = ?"
        params.append(auteur)
    if type_ in ("photo", "citation"):
        requete += " AND type = ?"
        params.append(type_)
    requete += " ORDER BY id DESC"
    with connexion() as cx:
        return cx.execute(requete, params).fetchall()


def lister_auteurs() -> list[str]:
    with connexion() as cx:
        return [r["auteur"] for r in cx.execute("SELECT DISTINCT auteur FROM soumissions ORDER BY auteur")]


def lister_retenus() -> list[sqlite3.Row]:
    with connexion() as cx:
        return cx.execute("SELECT * FROM soumissions WHERE retenu = 1 ORDER BY id").fetchall()


def obtenir_soumission(id_: int) -> sqlite3.Row | None:
    with connexion() as cx:
        return cx.execute("SELECT * FROM soumissions WHERE id = ?", (id_,)).fetchone()


def fichier_existe(fichier: str) -> bool:
    with connexion() as cx:
        return cx.execute("SELECT 1 FROM soumissions WHERE fichier = ?", (fichier,)).fetchone() is not None


def modifier_soumission(id_: int, description: str, citation: str, personne: str, contexte: str) -> None:
    with connexion() as cx:
        cx.execute(
            "UPDATE soumissions SET description = ?, citation = ?, personne_citee = ?, contexte = ? WHERE id = ?",
            (description, citation, personne, contexte, id_),
        )


def basculer_retenu(id_: int) -> None:
    with connexion() as cx:
        cx.execute("UPDATE soumissions SET retenu = 1 - retenu WHERE id = ?", (id_,))


def supprimer_soumission(id_: int) -> str | None:
    """Supprime la soumission et renvoie le nom du fichier photo à effacer, s'il y en a un."""
    with connexion() as cx:
        ligne = cx.execute("SELECT fichier FROM soumissions WHERE id = ?", (id_,)).fetchone()
        if ligne is None:
            return None
        cx.execute("DELETE FROM soumissions WHERE id = ?", (id_,))
        return ligne["fichier"]
