# C basics inside the c-build container

## Toolchain

- gcc 13 (`gcc`, `g++`) — default
- clang + clang-tidy + clang-format
- cmake, meson, ninja, make
- valgrind, cppcheck
- AddressSanitizer / UBSan (via `-fsanitize=address,undefined`)
- gdb (interactive, not exposed as MCP tool)

## Project layout the build tool understands

Auto-detection (first match wins):

| File at project root | Backend |
|----------------------|---------|
| `CMakeLists.txt`     | cmake → ninja, build dir at `build/` |
| `meson.build`        | meson → ninja, build dir at `build/` |
| `Makefile` / `makefile` / `GNUmakefile` | make, build dir as configured by the Makefile |
| (none)               | gcc compiles every `*.c` under `src/` (or root if no `src/`) into `build-make/<project>` |

## Test conventions

`run_tests` dispatches based on the build system:

- cmake: `ctest --test-dir build --output-on-failure`
- meson: `meson test -C build --print-errorlogs`
- make:  `make test` (falls back to `make check`)
- direct: every `build*/test_*` binary is executed in turn

For the direct path, write tests as standalone `test_*.c` programs that
exit non-zero on failure. Easy to integrate later with any framework
(Unity, Check, Criterion) without the MCP needing to know about it.

## Headless / sandbox notes

- No display, no GPU access in the build container.
- `/opt/projects/<project>` is your project; writes there hit the host.
- `~/.cache` etc. inside the container are ephemeral.
