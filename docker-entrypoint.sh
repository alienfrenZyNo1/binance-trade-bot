#!/bin/bash
# Entrypoint: ensures config files persist across container restarts/rebuilds.
#
# user.cfg and supported_coin_list live on the persistent volume (/app/data/config/)
# so they survive image rebuilds and Coolify redeploys.
#
# First boot: copies from image to volume.
# Subsequent boots: volume copy wins (persists any runtime changes).

PERSIST_DIR="/app/data/config"
mkdir -p "$PERSIST_DIR"

for cfg_file in user.cfg supported_coin_list; do
    IMG_PATH="/app/${cfg_file}"
    VOL_PATH="${PERSIST_DIR}/${cfg_file}"

    if [ -f "$VOL_PATH" ]; then
        # Volume has the file — use it (persistent copy wins)
        cp "$VOL_PATH" "$IMG_PATH"
        echo "[entrypoint] Loaded ${cfg_file} from persistent volume"
    elif [ -f "$IMG_PATH" ]; then
        # First boot — seed the volume from the image
        cp "$IMG_PATH" "$VOL_PATH"
        echo "[entrypoint] Seeded ${cfg_file} to persistent volume (first boot)"
    fi
done

# Strategy: let the config file's strategy setting take priority over env var.
# This allows switching strategies by editing user.cfg on the persistent volume,
# without needing to change Coolify env vars (which are encrypted/locked).
USER_STRATEGY=$(grep -E "^strategy=" /app/user.cfg 2>/dev/null | cut -d'=' -f2 | tr -d '[:space:]')
if [ -n "$USER_STRATEGY" ]; then
    export STRATEGY="$USER_STRATEGY"
    echo "[entrypoint] Strategy from user.cfg: $STRATEGY"
fi

# Hand off to the main process
exec "$@"
