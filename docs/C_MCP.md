# c MCP services

Two MCP services are registered in this sandbox:

## c-build (port 5192)

Runs inside a gcc-13 + clang container. Tools:

| Tool | Args | Purpose |
|------|------|---------|
| `build` | `project: str` | Auto-detect build system. `CMakeLists.txt` → cmake, `meson.build` → meson, `Makefile` → make, else compile `*.c` directly. Out-of-tree under `<project>/build/` (cmake/meson) or `<project>/build-make/`. |
| `run_tests` | `project: str, test_filter: str = ""` | Run `ctest`, `meson test`, or `make test` matching the build system. Falls back to executing every `build*/test_*` binary it finds. Failures fire to c-knowledge. |
| `lint` | `project: str` | `clang-tidy` over `*.c`/`*.h` plus `cppcheck --enable=warning,style`. Failures fire to c-knowledge. |
| `analyze` | `project: str, tool: str = "valgrind"` | `tool="valgrind"` runs every test binary under valgrind with `--leak-check=full`. `tool="asan"` rebuilds with `-fsanitize=address,undefined -g -O1` and runs the test binaries — sanitizer reports go straight to c-knowledge. |

The container picks up these from the project layout:
`requirements`-equivalent for C is the toolchain itself (already inside
the image), so there is no `install_deps`. Header search paths and
libraries come from the project's own build files.

## c-knowledge (port 5194)

See `mcp-knowledge/CLAUDE.md` for the full design.

Query tools: `ask`, `ask_tagged`, `ask_module`, `ask_project`,
`list_sources`, `stats`.

Maintenance tools: `forget`, `seed_docs`, `seed_c_source`, `retag_all`.

The knowledge base is **domain-scoped, not project-scoped** — all C
projects share one `c_knowledge` collection so cross-project patterns
surface in retrieval.

## What's not here (yet)

- **No debugger surface.** gdb is in the image but not exposed as a tool.
- **No package/distribution tools.** Per-project packaging (deb, tarball,
  static binary) hasn't been generalised yet.
- **No fuzzing.** libFuzzer / AFL aren't installed; if a project needs
  them, add them to the service Dockerfile and wire a tool.
