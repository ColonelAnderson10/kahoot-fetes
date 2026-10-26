#!/bin/bash
# Sauvegarde du dossier data/ (base SQLite + photos) dans une archive datée.
#   ./scripts/sauvegarde.sh [dossier_destination]
# Par défaut : /mnt/user/backups/kahoot-fetes. Les 10 archives les plus récentes sont conservées.
set -euo pipefail

DONNEES="/mnt/user/appdata/kahoot-fetes/data"
DESTINATION="${1:-/mnt/user/backups/kahoot-fetes}"
CONTENEUR="kahoot-fetes"
GARDER=10
HORODATAGE="$(date +%Y%m%d-%H%M%S)"

mkdir -p "$DESTINATION"
chmod 700 "$DESTINATION"

# Copie cohérente de la base, même si l'application est en train d'écrire
if docker ps --format '{{.Names}}' | grep -qx "$CONTENEUR"; then
  docker exec "$CONTENEUR" python -c \
    "import sqlite3; s=sqlite3.connect('/app/data/kahoot.db'); d=sqlite3.connect('/app/data/kahoot-sauvegarde.db'); s.backup(d); d.close(); s.close()"
else
  cp "$DONNEES/kahoot.db" "$DONNEES/kahoot-sauvegarde.db"
fi

ARCHIVE="$DESTINATION/kahoot-fetes-$HORODATAGE.tar.gz"
tar -czf "$ARCHIVE" -C "$DONNEES" kahoot-sauvegarde.db uploads
rm -f "$DONNEES/kahoot-sauvegarde.db"
chmod 600 "$ARCHIVE"
echo "Sauvegarde créée : $ARCHIVE"

# Rotation : on ne garde que les $GARDER plus récentes
ls -1t "$DESTINATION"/kahoot-fetes-*.tar.gz 2>/dev/null | tail -n +$((GARDER + 1)) | xargs -r rm -f
