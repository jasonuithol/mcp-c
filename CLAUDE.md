# mcp-c

Host-side notes for working on this repo. **Sandbox-side operating guide
is at [`docs/SANDBOX.md`](docs/SANDBOX.md)** — that file is mounted into
the sandbox and surfaces to in-sandbox Claude via auto-load.

## What this repo is

The C development half of the `claude-sandbox-core` `c` domain. Two
sibling containers:

| Subdir | Container | Port | Purpose |
|--------|-----------|------|---------|
| `service/` | `c-mcp-build` | 5192 | `build`, `run_tests`, `lint`, `analyze`, `install_dep`, `install_dep_source`, `remove_dep`, `list_deps` |
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
- **Per-project deps:** project-specific libraries live in
  `<project>/deps/{include,lib,lib/pkgconfig}` and are populated by
  `install_dep` (extracts a Debian bookworm package — no compilation,
  ABI matches by construction) or `install_dep_source` (curl + build
  from tarball). The build/test/lint/analyze tools auto-pick `deps/`
  up via `_deps_env()`, which sets `CPATH`, `LIBRARY_PATH`,
  `LD_LIBRARY_PATH`, `PKG_CONFIG_PATH`. Don't add project libs to
  `service/Dockerfile` — keep that image to the toolchain only.
  Manifest at `deps/.installed.json` records the **complete** file list
  each entry would contribute (derived from the apt staging dir, or from
  `DESTDIR=…` for source builds). `remove_dep` is refcount-aware: a file
  shared between two entries survives until the last claimant is removed.
  Standalone-Makefile source builds aren't supported — DESTDIR semantics
  vary too much to track files reliably; repackage with autotools/cmake/
  meson if you need a Makefile-only library.
- **Arch:** all install/remove tools take an `arch` parameter defaulting
  to `"native"`. Today only `"native"` / `"amd64"` are accepted — the
  c-build image only ships the native amd64 toolchain. To enable
  cross-compile arches, bake the `crossbuild-essential-<arch>` packages
  into `service/Dockerfile` and extend `_SUPPORTED_ARCHES` /
  `_DEB_TO_MULTIARCH` in `service/mcp-service.py`.
