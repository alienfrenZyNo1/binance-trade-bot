#!/bin/bash
# Ensure persistent volume mount survives Coolify redeployments.
COMPOSE_FILE="${COOLIFY_COMPOSE_FILE:-docker-compose.yaml}"
DATA_DIR="${DATA_DIR:-./data}"
mkdir -p "$DATA_DIR"
if [ ! -f "$COMPOSE_FILE" ]; then
    echo "ERROR: Compose file not found at $COMPOSE_FILE"
    exit 1
fi
if grep -q "data:/app/data" "$COMPOSE_FILE"; then
    echo "✅ Volume mount already present"
else
    echo "⚠️  Volume mount missing! Patching..."
    sed -i '/labels:/i\\        volumes:\\n            - '"$DATA_DIR"':/app/data' "$COMPOSE_FILE"
    docker compose -f "$COMPOSE_FILE" up -d
fi
