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

# Optional seccomp profile. If $SECCOMP_PROFILE is set and points at a
# readable file, pass --security-opt seccomp=… to docker run. Otherwise
# the container runtime's default profile applies (which is what most
# work wants — only specific scenarios like analyze(tool='tsan') need a
# relaxed profile because TSan needs personality(ADDR_NO_RANDOMIZE) to
# disable ASLR for its shadow memory). One profile per container; to
# swap, do: docker rm -f $CONTAINER_NAME && SECCOMP_PROFILE=… ./start-container.sh
# A bundled profile lives at service/seccomp/tsan.json (allow-all). Tighten
# it freely — start from your runtime's default and add an allow rule for
# the personality syscall.
SECCOMP_ARG=()
if [ -n "${SECCOMP_PROFILE:-}" ]; then
    if [ -r "$SECCOMP_PROFILE" ]; then
        SECCOMP_ARG=(--security-opt "seccomp=$SECCOMP_PROFILE")
        echo "Using seccomp profile: $SECCOMP_PROFILE"
    else
        echo "WARNING: SECCOMP_PROFILE=$SECCOMP_PROFILE not readable; using runtime default" >&2
    fi
fi

# Revive a leftover container from a prior run if one exists; otherwise
# create a fresh one. Runs as container-root by default — under rootless
# podman, container uid 0 maps to the host's invoking user, so files
# created in /opt/projects land owned by that host user.
#
# Note: seccomp is bound at container creation, so reviving an existing
# container ignores any new $SECCOMP_PROFILE — you must `docker rm -f` first.
if docker container inspect "$CONTAINER_NAME" >/dev/null 2>&1; then
    docker start "$CONTAINER_NAME" >/dev/null
else
    docker run -d \
        --name "$CONTAINER_NAME" \
        --network host \
        "${SECCOMP_ARG[@]}" \
        -v "$HOME/Projects:/opt/projects" \
        -e PROJECTS_DIR=/opt/projects \
        -e KNOWLEDGE_URL=http://localhost:5194/ingest \
        c-mcp-build
fi
