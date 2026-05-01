# claude-sandbox-core (c domain)

You are running inside a Podman container for C development. Your cwd is
`/workspace/<project>/` — a bind-mount of `~/Projects/<project>` on the
host. Changes you make are real.

This file is mounted at `/workspace/docs/SANDBOX.md` and symlinked to
`/workspace/CLAUDE.md` by the sandbox entrypoint, so it auto-loads at the
start of the session.

## MCP services

Two services are registered. Use them — don't reinvent.

- **c-knowledge** (`ask`, `ask_tagged`, `ask_module`, `ask_project`,
  `stats`, `list_sources`) — RAG over curated docs, indexed project
  source, and accumulated test-failure / fix / sanitizer history.
  **Query this before writing non-trivial code.** Cross-project retrieval
  is the point.
- **c-build** (`build`, `run_tests`, `lint`, `analyze`) — gcc 13 / clang
  toolchain with cmake, meson, make, valgrind, clang-tidy, cppcheck,
  AddressSanitizer. `build` auto-detects the build system. Test,
  lint, and analyze failures auto-ingest into c-knowledge.

Detail: `/workspace/docs/C_MCP.md`, `/workspace/docs/INGEST_MCP.md`.

## Working loop

1. **Ask the knowledge base first.** e.g. `ask_tagged("how to free a
   linked list", ["memory"])` or `ask_tagged("...", ["successful-example",
   "ringbuffer"])` for known-good patterns from existing projects.
2. **Read before you write.** Use Read/Glob/Grep on the project rather
   than relying solely on knowledge retrieval — the code is the source of
   truth; the knowledge base is a lossy index of it.
3. **Run tests via `run_tests`**, not a raw shell. Failures feed back
   into the knowledge base and become retrievable next session.
4. **Lint via `lint`.** Cheap, and failures get indexed.
5. **For memory bugs, run `analyze`.** It runs the test binary under
   valgrind by default (or rebuilds with `-fsanitize=address` when
   `tool="asan"`). Reports are indexed.

## Project conventions

- The build tool picks the right backend automatically. Don't shell out
  to `cmake`/`make` manually — go through `build` so failures get
  ingested.
- Out-of-tree build dirs land at `<project>/build/` (cmake, meson) or
  `<project>/build-make/` (make), so you can `rm -rf build*` to do a
  clean rebuild.
- Tests are run via the project's native test runner (`ctest`,
  `meson test`, `make test`/`make check`). If none of those exist,
  `run_tests` falls back to executing every `build*/test_*` binary it
  finds.

Detail: `/workspace/docs/C_BASICS.md`, `/workspace/docs/C_GOTCHAS.md`.

## What's not here

- No interactive debugger. gdb runs fine in the container but isn't
  exposed as an MCP tool — drop into a host shell if you need a
  TUI session.
- No process-control service. Long-running daemons aren't a normal C
  build artefact in this sandbox.
- No package/install tooling. Distribution model (deb, tarball, etc.) is
  per-project — no MCP surface yet.

## Per-project context

Look in `/workspace/docs/projects/<PROJECT>.md` for project-specific
notes before making architectural decisions. The project's own
`CLAUDE.md` (if present at `/workspace/<project>/CLAUDE.md`) takes
precedence for anything conflicting.
