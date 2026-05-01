# mcp-c

Host-side notes for working on this repo. **Sandbox-side operating guide
is at [`docs/SANDBOX.md`](docs/SANDBOX.md)** — that file is mounted into
the sandbox and surfaces to in-sandbox Claude via auto-load.

## What this repo is

The C development half of the `claude-sandbox-core` `c` domain. Two
sibling containers:

| Subdir | Container | Port | Purpose |
|--------|-----------|------|---------|
| `service/` | `c-mcp-build` | 5192 | `build`, `run_tests`, `lint`, `analyze` |
| `knowledge/` | `c-mcp-knowledge` | 5194 | RAG over curated docs + indexed source + accumulated failure/fix/sanitizer history |

The two halves are paired: `service/` fires fire-and-forget POSTs at
`knowledge/`'s `/ingest` endpoint, so signals from build/test/lint/analyze
runs accumulate as retrievable context.

## Where to look

- **`README.md`** — user-facing setup / start / stop instructions.
- **`docs/SANDBOX.md`** — sandbox-side operating guide (auto-loaded inside
  the sandbox).
- **`docs/`** — curated reference docs mounted read-only into the sandbox
  at `/workspace/docs/` (`C_BASICS.md`, `C_GOTCHAS.md`, `C_MCP.md`,
  `INGEST_MCP.md`, `projects/<PROJECT>.md`).
- **`knowledge/CLAUDE.md`** — design doc for the knowledge service
  (chunking strategy, ingest routing, metadata schema, known concerns).
- **`service/mcp-service.py`** — the build/test/lint/analyze tools.
- **`knowledge/mcp-service.py`** — the FastMCP query server + `/ingest`
  HTTP endpoint.

## Conventions worth preserving

- Domain-scoped collection: every C project shares one ChromaDB
  collection (`c_knowledge`) with `project` metadata. Cross-project
  retrieval is the point — don't refactor toward per-project collections.
- The build tool auto-detects backend (`CMakeLists.txt` → cmake,
  `meson.build` → meson, `Makefile` → make, else direct gcc). Backend
  detection lives in `service/mcp-service.py::_detect_backend()`.
- Test failures, lint errors, build errors, and sanitizer reports are
  all indexed via the same `/ingest` flow described in
  `docs/INGEST_MCP.md`. Any new tool that emits actionable failure
  output should hook into that.
