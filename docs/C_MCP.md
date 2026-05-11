# c MCP services

Two MCP services are registered in this sandbox:

## c-build (port 5192)

Runs inside a gcc-13 + clang container. Tools:

| Tool | Args | Purpose |
|------|------|---------|
| `build` | `project: str` | Auto-detect build system. `CMakeLists.txt` → cmake, `meson.build` → meson, `Makefile` → make, else compile `*.c` directly. Out-of-tree under `<project>/build/` (cmake/meson) or `<project>/build-make/`. |
| `run_tests` | `project: str, test_filter: str = ""` | Run `ctest`, `meson test`, or `make test` matching the build system. Falls back to executing every `build*/test_*` binary it finds. Failures fire to c-knowledge. |
| `lint` | `project: str` | `clang-tidy` over `*.c`/`*.h` plus `cppcheck --enable=warning,style`. Failures fire to c-knowledge. |
| `analyze` | `project: str, tool: str = "valgrind", scope: str = ""` | `tool="valgrind"` runs every test binary under valgrind with `--leak-check=full`. `tool="asan"` rebuilds with `-fsanitize=address,undefined -g -O1 -Wl,-z,now` and runs the test binaries with `ASAN_OPTIONS=halt_on_error=1:verify_asan_link_order=0:handle_segv=0` plus `LD_BIND_NOW=1` (see "ASan option choices" below). `tool="tsan"` is the thread-sanitizer flavour. Sanitizer reports go to c-knowledge. Tests run serially with a per-test wall-clock timeout (SIGABRT → SIGKILL). Every run writes forensic artifacts to `<project>/.forensics/<tool>/` — `heartbeat.log` (fsync'd START/END per test), `output/<test>.log` (live, fsync'd capture), `snapshots/<test>.{before,after}.json` (meminfo/loadavg/cgroup). After a host wedge, check `heartbeat.log` to see which test was in flight. `scope` filters which tests run (ctest regex / meson substring / direct-binary substring). Honors ctest `WILL_FAIL` and `SKIP_RETURN_CODE` properties — summary line splits results into `clean / skipped / with_errors`. |
| `debug` | `project: str, test: str, build_dir: str = "build"` | Run a single test (ctest test name or path under `<project>/<build_dir>/`) under `gdb --batch` and capture a backtrace. Use when a test exits with a fatal signal (`rc=139` SIGSEGV / `rc=134` SIGABRT / etc.) and you want to see *where* it crashed. The complement to `analyze(asan)` — with `handle_segv=0`, ASan can't print stacks on crash; gdb can. Auto-sets `ASAN_OPTIONS` / `TSAN_OPTIONS` when `build_dir` includes `asan`/`tsan` so sanitizer-instrumented binaries can start (gdb still intercepts the fatal signal first, so the trace is gdb's). Forensics under `<project>/.forensics/debug/`. |

### ASan option choices (`tool="asan"`)

These are baked into `service/mcp-service.py::ASAN_RUN_ENV`. Each one fixes a real failure mode hit on this codebase — don't strip without reading the comment in the source first.

- **`halt_on_error=1` + `abort_on_error=1`** — abort immediately on the first non-signal ASan finding (heap overflow, use-after-free, leak), one clean report per test.
- **`verify_asan_link_order=0`** — skip the startup check that aborts with *"ASan runtime does not come first in initial library list"*. Required for projects that dlopen non-instrumented provider/plugin `.so`s (e.g. betl's pg/mssql/csv providers). Tradeoff: ASan can't track allocations made by libs loaded before libasan (libpq, libfreetds, libyaml). **Do not try to fix this with `LD_PRELOAD=libasan.so` — that double-initialises libasan (because the instrumented binary also has libasan in DT_NEEDED) and produces an unbreakable DEADLYSIGNAL loop.**
- **`handle_segv=0`** — let the kernel handle fatal signals (SIGSEGV/SIGBUS/SIGFPE) instead of libasan. When a test genuinely crashes, libasan's handler tries to symbolize the crash, the unwind itself faults, the handler re-enters (libasan sets `SA_NODEFER`), and you get an infinite `AddressSanitizer:DEADLYSIGNAL` loop until the per-test 60s timeout fires. With `handle_segv=0`, the test exits with `rc=139` (SIGSEGV) / `rc=134` (SIGABRT), no log-spam, no useless libasan-frame coredumps. Tradeoff: no ASan-printed stack on signal crashes — debug those directly under gdb. Non-signal ASan reports (heap-buffer-overflow, use-after-free, leaks, UBSan) are unaffected.
- **`LD_BIND_NOW=1`** (env) + **`-Wl,-z,now`** (link flag) — force eager PLT resolution in sanitizer-instrumented binaries. Without it, libasan's init constructor touches a lazy PLT slot (observed: `pthread_getspecific`) and the resolver (`_dl_fixup` → `do_lookup_x`) faults with a pre-`main()` SIGSEGV that has no useful application stack. We apply both: the env var covers any binary we run, the link flag bakes `DT_BIND_NOW` into the build-asan/build-tsan binaries themselves so the protection survives env-stripping. Same fix is used for `tool="tsan"`.

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
