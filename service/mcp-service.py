#!/usr/bin/env python3
"""
mcp-service.py — c-build

Runs inside a Docker container. Exposes C build, test, lint, and
analysis tools to Claude Code.

Register with Claude Code (run this inside the claude-sandbox-core container):
    claude mcp add c-build --transport http http://localhost:5192/mcp
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import sys
from pathlib import Path

from fastmcp import FastMCP
from mcp_knowledge_base import KnowledgeReporter

# ── Config ────────────────────────────────────────────────────────────────────

PROJECTS_DIR = Path(os.environ.get("PROJECTS_DIR", "/opt/projects"))

# Compiler defaults; overridable via env so a project can opt into clang.
CC      = os.environ.get("CC", "gcc")
CFLAGS  = os.environ.get("CFLAGS",  "-Wall -Wextra -O2 -g")
LDFLAGS = os.environ.get("LDFLAGS", "")

# Sanitizer flags used by analyze(tool="asan").
ASAN_CFLAGS = "-fsanitize=address,undefined -fno-omit-frame-pointer -g -O1"

# ── Knowledge reporter ────────────────────────────────────────────────────────

_reporter = KnowledgeReporter(service="mcp-build")
_report = _reporter.report


# ── Helpers ───────────────────────────────────────────────────────────────────

def _project_dir(project: str) -> Path:
    if not project or "/" in project or ".." in project:
        raise ValueError(f"Invalid project name: {project!r}")
    d = PROJECTS_DIR / project
    if not d.is_dir():
        raise FileNotFoundError(f"Project directory not found: {d}")
    return d


def _detect_backend(pd: Path) -> str:
    """Return one of: 'cmake', 'meson', 'make', 'direct'."""
    if (pd / "CMakeLists.txt").exists():
        return "cmake"
    if (pd / "meson.build").exists():
        return "meson"
    for name in ("Makefile", "makefile", "GNUmakefile"):
        if (pd / name).exists():
            return "make"
    return "direct"


def _run(
    cmd: list[str],
    cwd: str | None = None,
    env: dict | None = None,
) -> tuple[bool, str]:
    """Run synchronously, capture combined stdout+stderr."""
    full_env = os.environ.copy()
    if env:
        full_env.update(env)
    proc = subprocess.run(
        cmd,
        cwd=cwd,
        env=full_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    return proc.returncode == 0, proc.stdout


async def _run_async(
    cmd: list[str],
    cwd: str | None = None,
    env: dict | None = None,
) -> tuple[bool, str]:
    return await asyncio.to_thread(_run, cmd, cwd, env)


def _gather_c_sources(pd: Path) -> list[Path]:
    """Direct-compile fallback: prefer src/, else root *.c."""
    src_dir = pd / "src"
    if src_dir.is_dir():
        return sorted(src_dir.rglob("*.c"))
    return sorted(p for p in pd.glob("*.c"))


# ── MCP server ────────────────────────────────────────────────────────────────

mcp = FastMCP(
    name="c-build",
    instructions=(
        "Tools for building, testing, linting, and analysing C projects. "
        "build auto-detects CMakeLists.txt → cmake, meson.build → meson, "
        "Makefile → make, otherwise compiles *.c directly with gcc. "
        "Test, lint, and analyze failures are reported to c-knowledge."
    ),
)


# ── build ─────────────────────────────────────────────────────────────────────

@mcp.tool()
async def build(project: str) -> str:
    """
    Build a C project, auto-detecting the build system.

    Detection order: CMakeLists.txt → cmake (Ninja, build dir at build/);
    meson.build → meson (build dir at build/); Makefile → make; otherwise
    compile every *.c under src/ (or root) directly with gcc into
    build-make/<project>.

    Args:
        project: Folder name under ~/Projects (no path separators).

    Returns the full build log. Run before run_tests / lint / analyze.
    """
    try:
        pd = _project_dir(project)
    except Exception as e:
        result = f"BUILD FAILED\n\n{e}"
        _report("build", {"project": project}, result, False)
        return result

    backend = _detect_backend(pd)
    lines = [f"Backend: {backend}"]

    if backend == "cmake":
        build_dir = pd / "build"
        if not (build_dir / "CMakeCache.txt").exists():
            ok, out = await _run_async(
                ["cmake", "-S", ".", "-B", "build", "-G", "Ninja"],
                cwd=str(pd),
            )
            lines.append(f"-- cmake configure ({'ok' if ok else 'failed'}) --\n{out}")
            if not ok:
                result = "BUILD FAILED ✗\n\n" + "\n\n".join(lines)
                _report("build", {"project": project}, result, False)
                return result
        ok, out = await _run_async(["cmake", "--build", "build"], cwd=str(pd))
        lines.append(f"-- cmake build ({'ok' if ok else 'failed'}) --\n{out}")

    elif backend == "meson":
        build_dir = pd / "build"
        if not (build_dir / "build.ninja").exists():
            ok, out = await _run_async(["meson", "setup", "build"], cwd=str(pd))
            lines.append(f"-- meson setup ({'ok' if ok else 'failed'}) --\n{out}")
            if not ok:
                result = "BUILD FAILED ✗\n\n" + "\n\n".join(lines)
                _report("build", {"project": project}, result, False)
                return result
        ok, out = await _run_async(["meson", "compile", "-C", "build"], cwd=str(pd))
        lines.append(f"-- meson compile ({'ok' if ok else 'failed'}) --\n{out}")

    elif backend == "make":
        ok, out = await _run_async(["make"], cwd=str(pd))
        lines.append(f"-- make ({'ok' if ok else 'failed'}) --\n{out}")

    else:  # direct
        sources = _gather_c_sources(pd)
        if not sources:
            result = "BUILD FAILED ✗\n\nNo CMakeLists.txt, meson.build, Makefile, or *.c files found."
            _report("build", {"project": project}, result, False)
            return result
        out_dir = pd / "build-make"
        out_dir.mkdir(exist_ok=True)
        out_bin = out_dir / project
        cmd = [CC, *CFLAGS.split(), *[str(s) for s in sources], "-o", str(out_bin)]
        if LDFLAGS:
            cmd += LDFLAGS.split()
        ok, out = await _run_async(cmd, cwd=str(pd))
        lines.append(
            f"-- {CC} ({'ok' if ok else 'failed'}) --\n"
            f"$ {' '.join(cmd)}\n{out}"
        )

    header = "BUILD SUCCEEDED ✓" if ok else "BUILD FAILED ✗"
    result = f"{header}\n\n" + "\n\n".join(lines)
    _report("build", {"project": project, "backend": backend}, result, ok)
    return result


# ── run_tests ─────────────────────────────────────────────────────────────────

_CTEST_LINE = re.compile(
    r"^\s*\d+/\d+\s+Test\s+#\d+:\s+(?P<name>\S+)\s+\.+\s*(?P<status>Passed|Failed|.+?)\s",
    re.MULTILINE,
)


def _parse_ctest(log: str) -> list[dict]:
    """Extract per-test outcomes from a ctest log."""
    tests = []
    for m in _CTEST_LINE.finditer(log):
        name = m.group("name")
        status = m.group("status").strip()
        tests.append({"node_id": name, "passed": status.startswith("Passed")})
    return tests


def _parse_meson(pd: Path) -> list[dict]:
    """Read meson's machine-readable testlog.json if present."""
    log_path = pd / "build" / "meson-logs" / "testlog.json"
    if not log_path.exists():
        return []
    tests = []
    try:
        # testlog.json is line-delimited JSON, one record per test.
        for line in log_path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            r = json.loads(line)
            tests.append({
                "node_id": r.get("name", "<unknown>"),
                "passed": r.get("result") == "OK",
            })
    except Exception:
        return []
    return tests


def _gather_test_binaries(pd: Path) -> list[Path]:
    bins: list[Path] = []
    for build_dir in pd.glob("build*"):
        if not build_dir.is_dir():
            continue
        for p in build_dir.rglob("test_*"):
            if p.is_file() and os.access(p, os.X_OK):
                bins.append(p)
    return sorted(bins)


@mcp.tool()
async def run_tests(project: str, test_filter: str = "") -> str:
    """
    Run the project's tests using whichever runner matches the build system.

    cmake → ctest, meson → meson test, make → make test (then make check),
    direct → execute every build*/test_* binary.

    Args:
        project: Folder name under ~/Projects.
        test_filter: Optional name filter (passed as -R to ctest, as a
                     test name to meson, ignored for make/direct).
    """
    try:
        pd = _project_dir(project)
    except Exception as e:
        result = f"RUN_TESTS FAILED\n\n{e}"
        _report("run_tests", {"project": project, "test_filter": test_filter}, result, False)
        return result

    backend = _detect_backend(pd)
    tests: list[dict] = []
    log = ""
    ok = True

    if backend == "cmake":
        if not (pd / "build").is_dir():
            return _fail_no_build("run_tests", project, test_filter)
        cmd = ["ctest", "--test-dir", "build", "--output-on-failure"]
        if test_filter:
            cmd += ["-R", test_filter]
        ok, log = await _run_async(cmd, cwd=str(pd))
        tests = _parse_ctest(log)

    elif backend == "meson":
        if not (pd / "build").is_dir():
            return _fail_no_build("run_tests", project, test_filter)
        cmd = ["meson", "test", "-C", "build", "--print-errorlogs"]
        if test_filter:
            cmd.append(test_filter)
        ok, log = await _run_async(cmd, cwd=str(pd))
        tests = _parse_meson(pd)

    elif backend == "make":
        ok, log = await _run_async(["make", "test"], cwd=str(pd))
        # If `make test` doesn't exist, try `make check`.
        if not ok and "No rule to make target" in log:
            ok2, log2 = await _run_async(["make", "check"], cwd=str(pd))
            log = log + "\n\n--- retry: make check ---\n" + log2
            ok = ok2
        tests = [{"node_id": "make-test", "passed": ok}]

    else:  # direct
        bins = _gather_test_binaries(pd)
        if not bins:
            result = (
                "RUN_TESTS FAILED ✗\n\n"
                "No build system detected and no build*/test_* binaries found.\n"
                "Run build() first, or place test_*.c sources under src/."
            )
            _report("run_tests", {"project": project, "test_filter": test_filter}, result, False)
            return result
        chunks = []
        all_ok = True
        for b in bins:
            success, out = await _run_async([str(b)], cwd=str(pd))
            tests.append({"node_id": b.name, "passed": success})
            chunks.append(
                f"-- {b.relative_to(pd)} ({'ok' if success else 'failed'}) --\n{out}"
            )
            all_ok = all_ok and success
        ok = all_ok
        log = "\n\n".join(chunks)

    # Summary
    total = len(tests)
    passed = sum(1 for t in tests if t["passed"])
    failed = total - passed
    summary = (
        f"backend={backend} total={total} passed={passed} failed={failed}"
    )
    header = "TESTS PASSED ✓" if ok else "TESTS FAILED ✗"

    if failed:
        summary += "\n\nFailures:\n" + "\n".join(
            f"  {t['node_id']}" for t in tests if not t["passed"]
        )

    # Send full envelope to ingest; return summary + log tail to caller.
    ingest_payload = json.dumps({
        "summary": f"{header}\n{summary}",
        "tests": tests,
        "stdout": log[-8000:],
    })
    _report("run_tests", {"project": project, "test_filter": test_filter}, ingest_payload, ok)

    tail = log[-2000:] if log else ""
    return f"{header}\n\n{summary}\n\n--- runner stdout (tail) ---\n{tail}"


def _fail_no_build(tool: str, project: str, test_filter: str = "") -> str:
    result = (
        f"{tool.upper()} FAILED ✗\n\n"
        f"No build/ directory found. Run build('{project}') first."
    )
    args = {"project": project}
    if test_filter:
        args["test_filter"] = test_filter
    _report(tool, args, result, False)
    return result


# ── lint ──────────────────────────────────────────────────────────────────────

def _gather_lint_targets(pd: Path) -> list[Path]:
    """All .c and .h under the project, skipping build directories."""
    skip_parts = {"build", "build-make", ".git", "node_modules"}
    out = []
    for ext in ("*.c", "*.h"):
        for p in pd.rglob(ext):
            if any(part in skip_parts for part in p.relative_to(pd).parts):
                continue
            out.append(p)
    return sorted(out)


@mcp.tool()
async def lint(project: str) -> str:
    """
    Run clang-tidy and cppcheck across the project.

    clang-tidy uses the compile-commands DB if present (cmake / meson
    can emit one); otherwise it lints with default flags. cppcheck runs
    with --enable=warning,style.

    Args:
        project: Folder name under ~/Projects.
    """
    try:
        pd = _project_dir(project)
    except Exception as e:
        result = f"LINT FAILED\n\n{e}"
        _report("lint", {"project": project}, result, False)
        return result

    targets = _gather_lint_targets(pd)
    if not targets:
        result = "LINT FAILED ✗\n\nNo .c or .h files found."
        _report("lint", {"project": project}, result, False)
        return result

    lines: list[str] = []

    # clang-tidy — use compile_commands.json if cmake/meson generated one.
    tidy_cmd = ["clang-tidy"]
    cdb = pd / "build" / "compile_commands.json"
    if cdb.exists():
        tidy_cmd += ["-p", str(cdb.parent)]
    tidy_cmd += [str(t) for t in targets if t.suffix == ".c"]
    if len(tidy_cmd) > (3 if cdb.exists() else 1):
        tidy_ok, tidy_out = await _run_async(tidy_cmd, cwd=str(pd))
        lines.append(f"-- clang-tidy ({'ok' if tidy_ok else 'warnings'}) --\n{tidy_out}")
    else:
        tidy_ok = True
        lines.append("-- clang-tidy skipped (no .c files) --")

    # cppcheck — fast and zero-config.
    cppcheck_cmd = [
        "cppcheck", "--enable=warning,style",
        "--inline-suppr", "--quiet", "--error-exitcode=2",
        str(pd),
    ]
    cpp_ok, cpp_out = await _run_async(cppcheck_cmd, cwd=str(pd))
    lines.append(f"-- cppcheck ({'ok' if cpp_ok else 'issues'}) --\n{cpp_out}")

    ok = tidy_ok and cpp_ok
    header = "LINT CLEAN ✓" if ok else "LINT FAILED ✗"
    result = f"{header}\n\n" + "\n\n".join(lines)
    _report("lint", {"project": project}, result, ok)
    return result


# ── analyze ───────────────────────────────────────────────────────────────────

@mcp.tool()
async def analyze(project: str, tool: str = "valgrind") -> str:
    """
    Run dynamic analysis on the project's test binaries.

    tool="valgrind" (default): run every build*/test_* under
        valgrind --leak-check=full --error-exitcode=23. Use the existing
        build — no recompile.

    tool="asan": rebuild the project with -fsanitize=address,undefined
        (writes into build-asan/), then run the resulting test binaries.
        Only supported for the direct-compile path right now; for cmake
        / meson / make you would set CFLAGS in the project's build files.

    Args:
        project: Folder name under ~/Projects.
        tool:    'valgrind' or 'asan'.
    """
    try:
        pd = _project_dir(project)
    except Exception as e:
        result = f"ANALYZE FAILED\n\n{e}"
        _report("analyze", {"project": project, "tool": tool}, result, False)
        return result

    if tool == "valgrind":
        return await _analyze_valgrind(project, pd)
    if tool == "asan":
        return await _analyze_asan(project, pd)
    result = f"ANALYZE FAILED ✗\n\nUnknown tool {tool!r}. Use 'valgrind' or 'asan'."
    _report("analyze", {"project": project, "tool": tool}, result, False)
    return result


async def _analyze_valgrind(project: str, pd: Path) -> str:
    bins = _gather_test_binaries(pd)
    if not bins:
        result = (
            "ANALYZE FAILED ✗\n\n"
            "No build*/test_* binaries to analyse. Run build() and run_tests() first."
        )
        _report("analyze", {"project": project, "tool": "valgrind"}, result, False)
        return result

    sections: list[str] = []
    findings: list[dict] = []
    overall_ok = True

    for b in bins:
        cmd = [
            "valgrind",
            "--leak-check=full",
            "--show-leak-kinds=all",
            "--track-origins=yes",
            "--error-exitcode=23",
            str(b),
        ]
        ok, out = await _run_async(cmd, cwd=str(pd))
        sections.append(f"-- {b.relative_to(pd)} ({'clean' if ok else 'errors'}) --\n{out}")
        findings.append({"node_id": b.name, "passed": ok})
        overall_ok = overall_ok and ok

    log = "\n\n".join(sections)
    header = "ANALYZE CLEAN ✓" if overall_ok else "ANALYZE FOUND ISSUES ✗"
    summary = (
        f"tool=valgrind binaries={len(bins)} "
        f"clean={sum(1 for f in findings if f['passed'])} "
        f"with_errors={sum(1 for f in findings if not f['passed'])}"
    )

    ingest_payload = json.dumps({
        "summary": f"{header}\n{summary}",
        "findings": findings,
        "stdout": log[-12000:],
    })
    _report(
        "analyze",
        {"project": project, "tool": "valgrind"},
        ingest_payload,
        overall_ok,
    )

    tail = log[-2500:]
    return f"{header}\n\n{summary}\n\n--- valgrind output (tail) ---\n{tail}"


async def _analyze_asan(project: str, pd: Path) -> str:
    sources = _gather_c_sources(pd)
    backend = _detect_backend(pd)
    if backend != "direct" or not sources:
        result = (
            "ANALYZE FAILED ✗\n\n"
            "asan path currently only supports direct-compile projects "
            "(no CMakeLists.txt / meson.build / Makefile, plus *.c sources). "
            "For cmake/meson/make projects, set "
            "CFLAGS='-fsanitize=address,undefined -g -O1' in your build files "
            "and use analyze(tool='valgrind') instead."
        )
        _report("analyze", {"project": project, "tool": "asan"}, result, False)
        return result

    out_dir = pd / "build-asan"
    out_dir.mkdir(exist_ok=True)
    out_bin = out_dir / f"{project}-asan"

    cmd = [
        CC, *CFLAGS.split(), *ASAN_CFLAGS.split(),
        *[str(s) for s in sources], "-o", str(out_bin),
    ]
    ok, out = await _run_async(cmd, cwd=str(pd))
    if not ok:
        result = f"ANALYZE FAILED ✗\n\n-- asan rebuild --\n{out}"
        _report("analyze", {"project": project, "tool": "asan"}, result, False)
        return result

    # Run the rebuilt binary; ASan exits non-zero on detection.
    env = {
        "ASAN_OPTIONS": "abort_on_error=0:halt_on_error=0:detect_leaks=1",
        "UBSAN_OPTIONS": "print_stacktrace=1:halt_on_error=0",
    }
    run_ok, run_out = await _run_async([str(out_bin)], cwd=str(pd), env=env)
    log = f"-- asan rebuild --\n{out}\n\n-- asan run --\n{run_out}"

    header = "ANALYZE CLEAN ✓" if run_ok else "ANALYZE FOUND ISSUES ✗"
    findings = [{"node_id": out_bin.name, "passed": run_ok}]
    summary = f"tool=asan binary={out_bin.name} clean={run_ok}"

    ingest_payload = json.dumps({
        "summary": f"{header}\n{summary}",
        "findings": findings,
        "stdout": log[-12000:],
    })
    _report(
        "analyze",
        {"project": project, "tool": "asan"},
        ingest_payload,
        run_ok,
    )

    tail = log[-2500:]
    return f"{header}\n\n{summary}\n\n--- asan output (tail) ---\n{tail}"


# ── Entrypoint ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print(f"PROJECTS_DIR={PROJECTS_DIR}")
    print(f"CC={CC}  CFLAGS={CFLAGS!r}  LDFLAGS={LDFLAGS!r}")
    print(f"KNOWLEDGE_URL={_reporter.url}")
    print("Starting c-build MCP on http://0.0.0.0:5192")
    print()
    print("Register with Claude Code:")
    print("  claude mcp add c-build --transport http http://localhost:5192/mcp")
    print()
    mcp.run(transport="streamable-http", host="0.0.0.0", port=5192)
