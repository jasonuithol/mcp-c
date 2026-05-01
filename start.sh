#!/usr/bin/env bash
# start.sh — bring up both mcp-c containers.
# Idempotent: each inner script revives an existing container or creates a new one.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "Starting c-mcp-build..."
"$SCRIPT_DIR/service/start-container.sh"

echo "Starting c-mcp-knowledge..."
"$SCRIPT_DIR/knowledge/start-container.sh"

echo "Done. Services on :5192 (build) and :5194 (knowledge)."
