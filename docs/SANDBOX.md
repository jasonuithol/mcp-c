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
- **c-build** (`build`, `run_tests`, `lint`, `analyze`, `install_dep`,
  `install_dep_source`, `remove_dep`, `list_deps`) — gcc 13 / clang
  toolchain with cmake, meson, make, valgrind, clang-tidy, cppcheck,
  AddressSanitizer, ThreadSanitizer. `build` auto-detects the build
  system. Test, lint, and analyze failures auto-ingest into c-knowledge.

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
   `tool="asan"`). Reports are indexed. The asan path runs tests with
   `ASAN_OPTIONS=halt_on_error=1:verify_asan_link_order=0:handle_segv=0`:
   - `verify_asan_link_order=0` so tests that dlopen non-instrumented
     provider/plugin `.so`s don't bail with "ASan runtime does not come
     first in initial library list" (tradeoff: ASan won't track allocs
     from libs loaded before libasan).
   - `handle_segv=0` so a genuine SIGSEGV in the test exits cleanly with
     `rc=139` instead of getting stuck in libasan's recursive
     DEADLYSIGNAL handler. Tradeoff: no ASan-printed stack on signal
     crashes — debug those under gdb. Heap-overflow / use-after-free /
     leak reports are unaffected (those don't go through the signal
     handler).
   See `/workspace/docs/C_MCP.md` for the full rationale.
6. **For data races / lock-discipline bugs, run `analyze(tool="tsan")`.**
   Rebuilds the project under `-fsanitize=thread,undefined` into
   `build-tsan/` and runs the resulting tests with `TSAN_OPTIONS` /
   `UBSAN_OPTIONS` halting on the first finding. Works for cmake, meson,
   and direct-compile (cmake also gets `POSITION_INDEPENDENT_CODE=OFF`
   and `-no-pie` on test executables; shared libs stay PIC). Reach for
   this whenever you're touching mutexes, condvars, atomics, lock-free
   queues, or anything else with concurrent shared state — TSan catches
   races that mock-based unit tests will miss every time. Reports are
   indexed.

   **Caveat:** tsan needs the c-build container started under a relaxed
   seccomp profile (TSan disables ASLR via the `personality()` syscall,
   which the runtime default seccomp filters). If you see
   `setarch: failed to set personality` in the test output, ask the host
   user to restart the container with
   `SECCOMP_PROFILE=$PWD/service/seccomp/tsan.json ./service/start-container.sh`
   — you can't do that yourself from inside the sandbox.

## Forensics from analyze()

Every `analyze()` invocation writes durable artifacts under
`<project>/.forensics/<tool>/`:

- `heartbeat.log` — fsync'd `START`/`END` line per test, with pid and
  elapsed time. **If the host wedges mid-run, this is the first file to
  read** — a `START` with no matching `END` tells you exactly which test
  was in flight.
- `output/<test>.log` — live, line-flushed, fsync'd capture of the test's
  combined stdout+stderr (so output past the last libc flush isn't lost).
- `snapshots/<test>.before.json` and `.after.json` — meminfo, loadavg,
  process count, cgroup `memory.{current,max}` / `pids.{current,max}`.

Under sanitizers, tests are run **serially with a per-test wall-clock
timeout** (60s default; overridable via `MCP_C_TEST_TIMEOUT_S` on the
container). On timeout the test's process group is sent `SIGABRT` (so
ASan/TSan can dump state into `output/<test>.log`), then `SIGKILL` after
a grace period (`MCP_C_TEST_ABORT_GRACE_S`, 5s default).

Pass `scope=...` to `analyze` to sanitize-run a single test without
inventing a workaround: regex for ctest, substring for meson / direct /
valgrind.

If the host crashes during an `analyze(tool=...)`, after reboot ask the
host user to grab the kernel log from the dead boot
(`journalctl -k -b -1`) — that lives in journald, not the container, so
the service can't capture it.

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

## Project dependencies

Project libraries live in `<project>/deps/{include,lib,lib/pkgconfig}`.
The build/test/lint/analyze tools auto-add those paths to `CPATH`,
`LIBRARY_PATH`, `LD_LIBRARY_PATH`, and `PKG_CONFIG_PATH` — your build
files don't need to know about `deps/` at all, just `-lyaml` /
`pkg-config --libs yaml-0.1` like normal.

Two ways to populate it:

- **`install_dep(project, packages)`** — for libraries that exist in
  Debian bookworm. Pass dev + runtime packages, e.g.
  `install_dep("ChessGen", ["libyaml-dev", "libyaml-0-2"])`. Extracts
  the .debs straight into `deps/` (no compilation, ABI is correct by
  construction). This is the fast path — prefer it.
- **`install_dep_source(project, name, url, sha256, configure_args, build_system, arch)`**
  — for libraries not in bookworm, or HEAD/custom versions. Builds
  inside the c-build container so binaries are ABI-compatible. Auto-
  detects autotools / cmake / meson / make from the tarball; override
  with `build_system=...` if it guesses wrong.

`list_deps(project)` shows the current manifest (version, arch, file
count per entry). `remove_dep(project, packages)` clears the manifest
entry and deletes its files, **except files still claimed by another
entry** — shared files survive until the last claimant is removed.
Each install records the complete file list it would contribute
(derived from staging / `DESTDIR`), so this works correctly for
`libyaml-dev`/`libyaml-0-2`-style overlapping packages. The `arch` parameter on
all four install/remove tools defaults to `"native"`; only `"native"`
and `"amd64"` are accepted right now since the c-build image ships
only the native amd64 toolchain. The c-build container itself only
carries the toolchain — don't ask for libraries to be added to its
Dockerfile.

## Per-project context

Look in `/workspace/docs/projects/<PROJECT>.md` for project-specific
notes before making architectural decisions. The project's own
`CLAUDE.md` (if present at `/workspace/<project>/CLAUDE.md`) takes
precedence for anything conflicting.
