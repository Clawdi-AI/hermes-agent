#!/bin/bash
# Docker entrypoint: bootstrap config files into the mounted volume, then run hermes.
#
# Supported first-argument modes:
#   webapi       Run ONLY the FastAPI webapi module (no messenger
#                platforms). Useful for headless API-only deployments.
#   dashboard    Run BOTH the webapi AND the gateway. Used by the Clawdi
#                k8s / Phala CVM deployments where the frontend needs
#                the webapi routes AND the agent needs to service
#                configured messenger platforms. See
#                hermes/k8s/pod-template.yaml.j2 in the Clawdi repo.
#   gateway      (or any other hermes subcommand) Passed through to
#                `hermes` — preserves upstream behavior.
set -e

HERMES_HOME="/opt/data"
INSTALL_DIR="/opt/hermes"

# Create essential directory structure.  Cache and platform directories
# (cache/images, cache/audio, platforms/whatsapp, etc.) are created on
# demand by the application — don't pre-create them here so new installs
# get the consolidated layout from get_hermes_dir().
mkdir -p "$HERMES_HOME"/{cron,sessions,logs,hooks,memories,skills}

# .env
if [ ! -f "$HERMES_HOME/.env" ]; then
    cp "$INSTALL_DIR/.env.example" "$HERMES_HOME/.env"
fi

# config.yaml
if [ ! -f "$HERMES_HOME/config.yaml" ]; then
    cp "$INSTALL_DIR/cli-config.yaml.example" "$HERMES_HOME/config.yaml"
fi

# SOUL.md
if [ ! -f "$HERMES_HOME/SOUL.md" ]; then
    cp "$INSTALL_DIR/docker/SOUL.md" "$HERMES_HOME/SOUL.md"
fi

# Sync bundled skills (manifest-based so user edits are preserved)
if [ -d "$INSTALL_DIR/skills" ]; then
    python3 "$INSTALL_DIR/tools/skills_sync.py"
fi

mode="${1:-}"

case "$mode" in
    webapi)
        # Headless API-only. No messenger platforms.
        shift
        exec python3 -m webapi "$@"
        ;;
    dashboard)
        # Both webapi (foreground) and gateway (background). Used by
        # Clawdi-managed deployments. The gateway runs as a background
        # child and is killed by trap when the foreground exits so the
        # container exits cleanly when uvicorn dies.
        shift
        echo "[entrypoint] starting gateway in background..."
        hermes gateway run "$@" &
        gateway_pid=$!
        trap 'kill -TERM "$gateway_pid" 2>/dev/null || true' EXIT TERM INT
        echo "[entrypoint] starting webapi in foreground (gateway pid=$gateway_pid)..."
        exec python3 -m webapi
        ;;
    *)
        # Pass-through: treat any other first arg as a `hermes` subcommand.
        # This preserves upstream behavior for `gateway`, `chat`, `cron`,
        # `setup`, etc. and keeps existing docker-compose files working.
        exec hermes "$@"
        ;;
esac
