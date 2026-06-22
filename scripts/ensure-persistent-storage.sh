#!/bin/bash
# Ensure persistent volume mount survives Coolify redeployments.
# Run this after any Coolify deploy of the binance-trade-bot.
# Usage: ./ensure-persistent-storage.sh

COMPOSE="REDACTED/applications/REDACTED/docker-compose.yaml"
DATA_DIR="REDACTED"

# Create data directory if missing
mkdir -p "$DATA_DIR"

# Check if compose file exists
if [ ! -f "$COMPOSE" ]; then
    echo "ERROR: Compose file not found at $COMPOSE"
    exit 1
fi

# Check if volume mount is present
if grep -q "REDACTED:/app/data" "$COMPOSE"; then
    echo "✅ Volume mount already present in compose file"
else
    echo "⚠️  Volume mount missing! Patching compose file..."
    # Insert volumes before the first label
    sed -i '/labels:/i\        volumes:\n            - REDACTED:/app/data' "$COMPOSE"
    echo "✅ Volume mount added. Restarting container..."
    docker compose -f "$COMPOSE" up -d
fi

# Verify
echo "Database files:"
ls -la "$DATA_DIR/"*.db 2>/dev/null
