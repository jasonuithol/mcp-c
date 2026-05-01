#!/usr/bin/env bash
# clean.sh — undo setup.sh. Removes both containers AND their images so a
# fresh clean → setup → start cycle returns the repo to a verifiable bare
# state.
#
# Does NOT touch host-mounted state (knowledge/knowledge/ ChromaDB index)
# — that's data, not setup. Delete it manually for a totally fresh KB.
set -euo pipefail

# Containers first (must be removed before their image can be deleted).
for name in c-mcp-build c-mcp-knowledge; do
    if docker container inspect "$name" >/dev/null 2>&1; then
        echo "Removing container $name..."
        docker rm -f "$name" >/dev/null
    fi
done

for image in c-mcp-build c-mcp-knowledge; do
    if docker image inspect "$image" >/dev/null 2>&1; then
        echo "Removing image $image..."
        docker rmi -f "$image" >/dev/null
    fi
done

echo "Done."
