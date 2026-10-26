#!/bin/bash
# Active ou désactive l'accès public (Tailscale Funnel) pour ce conteneur seulement.
#   ./scripts/funnel.sh on      → accessible publiquement sur Internet
#   ./scripts/funnel.sh off     → accessible seulement depuis votre tailnet
#   ./scripts/funnel.sh status  → état actuel
# Le conteneur tailscale relit automatiquement serve.json : aucun redémarrage nécessaire.
set -euo pipefail

RACINE="$(cd "$(dirname "$0")/.." && pwd)"
CONFIG="$RACINE/tailscale/config"
CONTENEUR="kahoot-fetes-tailscale"
mkdir -p "$CONFIG"

case "${1:-}" in
  on)
    cp "$RACINE/tailscale/serve-funnel.json" "$CONFIG/serve.json"
    echo "Funnel ACTIVÉ : l'application est publique."
    ;;
  off)
    cp "$RACINE/tailscale/serve-prive.json" "$CONFIG/serve.json"
    echo "Funnel DÉSACTIVÉ : l'application n'est plus publique."
    ;;
  status)
    ;;
  *)
    echo "Usage : $0 on|off|status" >&2
    exit 1
    ;;
esac

sleep 3
docker exec "$CONTENEUR" tailscale funnel status || true
