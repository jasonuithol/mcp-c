#!/usr/bin/env bash
# build-container.sh — build the C MCP build container image
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "Building c-mcp-build image..."
docker build -f "$SCRIPT_DIR/Dockerfile" -t c-mcp-build "$SCRIPT_DIR"
echo "Done. Run with: $SCRIPT_DIR/start-container.sh"
