#!/usr/bin/env bash
# start-container.sh — run the c-mcp-build container
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Load .env if present (reserved for future use)
if [ -f "$SCRIPT_DIR/.env" ]; then
    set -a
    source "$SCRIPT_DIR/.env"
    set +a
fi

CONTAINER_NAME="c-mcp-build"

# Revive a leftover container from a prior run if one exists; otherwise
# create a fresh one. Runs as container-root by default — under rootless
# podman, container uid 0 maps to the host's invoking user, so files
# created in /opt/projects land owned by that host user.
if docker container inspect "$CONTAINER_NAME" >/dev/null 2>&1; then
    docker start "$CONTAINER_NAME" >/dev/null
else
    docker run -d \
        --name "$CONTAINER_NAME" \
        --network host \
        -v "$HOME/Projects:/opt/projects" \
        -e PROJECTS_DIR=/opt/projects \
        -e KNOWLEDGE_URL=http://localhost:5194/ingest \
        c-mcp-build
fi
