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
import datetime
import hashlib
import json
import os
import platform
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
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
# ASAN_OPTIONS rationale:
#   abort_on_error=1, halt_on_error=1: when ASan detects a non-signal
#     error (heap overflow, use-after-free, etc.), abort immediately
#     instead of trying to continue.
#   handle_segv=0: do NOT install ASan's SIGSEGV handler. When a test
#     genuinely crashes with a fatal signal, libasan's handler tries to
#     print a backtrace, the backtrace walk itself faults inside libasan,
#     the handler re-fires (libasan uses SA_NODEFER), and we get an
#     infinite "ASan:DEADLYSIGNAL" loop that produces multi-million-line
#     log files until our 60s SIGABRT timeout fires (whose coredump frame
#     is libasan's re-entered handler, not the original fault). With
#     handle_segv=0 the kernel handles the signal: test exits cleanly
#     with rc=139 (SIGSEGV) or rc=134 (SIGABRT), no log-spam, no useless
#     libasan-frame coredumps. Tradeoff: no ASan-printed stack on signal
#     crashes — debug those under gdb. Non-signal ASan reports (heap
#     overflows, leaks, UB) are unaffected.
#   detect_leaks=1: enable LSan at process exit.
#   verify_asan_link_order=0: skip the runtime check that aborts with
#     "ASan runtime does not come first in initial library list" when
#     the instrumented binary dlopens (or transitively links) provider/
#     plugin .so files that aren't asan-instrumented. ASan still works
#     for the project's own code; it just can't track allocations made
#     by libs loaded before libasan (libpq, libfreetds, libyaml, ...).
#
# An earlier attempt to fix the ordering check via LD_PRELOAD=libasan.so
# caused libasan to be double-initialised (since the binary's DT_NEEDED
# also references libasan), which corrupted its state and produced the
# same DEADLYSIGNAL loop. verify_asan_link_order=0 is the supported
# workaround; LD_PRELOAD is not.
ASAN_RUN_ENV = {
    "ASAN_OPTIONS": "abort_on_error=1:halt_on_error=1:detect_leaks=1:verify_asan_link_order=0:handle_segv=0",
    "UBSAN_OPTIONS": "print_stacktrace=1:halt_on_error=1",
}

# Sanitizer flags used by analyze(tool="tsan"). Thread + UB sanitizer, no -O level
# pinned (TSan instrumentation works at any opt level; leave it to CFLAGS).
TSAN_CFLAGS = "-fsanitize=thread,undefined -fno-omit-frame-pointer -g"
TSAN_RUN_ENV = {
    "TSAN_OPTIONS": "halt_on_error=1:second_deadlock_stack=1",
    "UBSAN_OPTIONS": "halt_on_error=1:print_stacktrace=1",
}
# Wrapping the test runner with `setarch <arch> -R` sets ADDR_NO_RANDOMIZE on
# the runner's process personality; that bit is inherited by every child the
# runner spawns (fork preserves it, execve doesn't clear it), so each test
# binary runs without ASLR. Required because TSan's shadow memory mapping is
# incompatible with high-entropy ASLR — without this, even non-PIE binaries
# fail flakily with "FATAL: ThreadSanitizer: unexpected memory mapping" as the
# loader's randomised library placement collides with TSan's reserved range.
# Requires the personality() syscall, which the container runtime's default
# seccomp profile filters with ENOSYS — start-container.sh accepts an optional
# $SECCOMP_PROFILE pointing at service/seccomp/tsan.json (or a project-specific
# profile) to relax that.
TSAN_RUN_WRAPPER = ["setarch", platform.machine(), "-R"]

# ── Forensics config ─────────────────────────────────────────────────────────
# Per-test wall-clock timeout used by analyze() runs. On timeout, the test's
# process group is sent SIGABRT (so ASan/TSan can dump state to the captured
# output log) and SIGKILL after a short grace period.
TEST_TIMEOUT_S = float(os.environ.get("MCP_C_TEST_TIMEOUT_S", "60"))
TEST_ABORT_GRACE_S = float(os.environ.get("MCP_C_TEST_ABORT_GRACE_S", "5"))

# Forensic artifacts (heartbeat, fsync'd live output, pre/post system
# snapshots) land under <project>/<FORENSICS_DIRNAME>/<tool>/ so they are:
#   - host-visible (the project dir is bind-mounted in from the host),
#   - per-project (no cross-talk between betl/foo/bar runs),
#   - resilient to a kernel-level wedge (each write is fsync'd),
#   - resilient to container teardown (artifacts persist on the host FS).
#
# Items NOT recorded here because they require host-side tooling unavailable
# inside the container:
#   - post-reboot kernel ring (journalctl -k -b -1) — requires host journald
#   - container event tap (podman events) — requires the docker socket
# Run those manually on the host after a wedge; they survive the reboot.
FORENSICS_DIRNAME = ".forensics"

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


# ── Per-project deps/ ─────────────────────────────────────────────────────────
#
# A project may carry a deps/ tree populated by install_dep / install_dep_source:
#
#     <project>/deps/
#       include/        — headers (added to CPATH)
#       lib/            — shared libs, static libs (LIBRARY_PATH, LD_LIBRARY_PATH)
#       lib/pkgconfig/  — .pc files (PKG_CONFIG_PATH)
#
# build/run_tests/lint/analyze auto-pick it up via _deps_env. No flags needed
# in the project's own build files for stock pkg-config / -l<name> usage.

DEPS_MANIFEST_NAME = ".installed.json"


def _deps_root(pd: Path) -> Path:
    return pd / "deps"


def _deps_env(pd: Path) -> dict[str, str]:
    """Env-var additions exposing <project>/deps/ to compilers and linkers."""
    deps = _deps_root(pd)
    if not deps.is_dir():
        return {}
    env: dict[str, str] = {}
    inc = deps / "include"
    lib = deps / "lib"
    pkg = lib / "pkgconfig"
    if inc.is_dir():
        env["CPATH"] = str(inc)
    if lib.is_dir():
        env["LIBRARY_PATH"] = str(lib)
        env["LD_LIBRARY_PATH"] = str(lib)
    if pkg.is_dir():
        env["PKG_CONFIG_PATH"] = str(pkg)
    return env


def _read_manifest(pd: Path) -> dict:
    f = _deps_root(pd) / DEPS_MANIFEST_NAME
    if not f.is_file():
        return {}
    try:
        return json.loads(f.read_text())
    except Exception:
        return {}


def _write_manifest(pd: Path, manifest: dict) -> None:
    deps = _deps_root(pd)
    deps.mkdir(parents=True, exist_ok=True)
    (deps / DEPS_MANIFEST_NAME).write_text(json.dumps(manifest, indent=2, sort_keys=True))


# ── Forensics: heartbeat, fsync'd tee, snapshots, timeouts ───────────────────


def _iso_now() -> str:
    """UTC ISO-8601 with microseconds, for heartbeat lines."""
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _safe_name(s: str) -> str:
    """Make a test name safe for a path component."""
    return re.sub(r"[^A-Za-z0-9._-]+", "_", s) or "unnamed"


def _capture_snapshot() -> dict:
    """One-shot view of process/memory pressure. Cheap; called pre + post test."""
    snap: dict = {"timestamp": _iso_now()}

    meminfo: dict[str, str] = {}
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                k, _, v = line.partition(":")
                k = k.strip()
                if k in ("MemTotal", "MemFree", "MemAvailable", "SwapTotal", "SwapFree"):
                    meminfo[k] = v.strip()
    except Exception as e:
        meminfo["error"] = str(e)
    snap["meminfo"] = meminfo

    try:
        snap["loadavg"] = Path("/proc/loadavg").read_text().strip()
    except Exception as e:
        snap["loadavg"] = f"error: {e}"

    try:
        snap["proc_count"] = sum(1 for p in Path("/proc").iterdir() if p.name.isdigit())
    except Exception as e:
        snap["proc_count"] = f"error: {e}"

    # cgroup v2: bookworm + rootless podman → unified hierarchy at /sys/fs/cgroup.
    cgroup: dict[str, str] = {}
    for key in ("memory.current", "memory.max", "memory.swap.current",
                "pids.current", "pids.max"):
        try:
            cgroup[key] = Path(f"/sys/fs/cgroup/{key}").read_text().strip()
        except Exception:
            pass
    snap["cgroup"] = cgroup

    return snap


class _ForensicsRecorder:
    """
    Per-analyze artifact recorder. Writes to <pd>/<FORENSICS_DIRNAME>/<tool>/:
      heartbeat.log   — START/END line per test, fsync'd before the next step
      output/<n>.log  — live, unbuffered, fsync'd stdout+stderr capture
      snapshots/<n>.{before,after}.json — meminfo/loadavg/cgroup snapshots

    Each invocation wipes the prior run's directory so the artifacts always
    describe the most recent analyze() call.
    """

    def __init__(self, pd: Path, tool: str):
        self.root = pd / FORENSICS_DIRNAME / tool
        if self.root.exists():
            shutil.rmtree(self.root, ignore_errors=True)
        self.output_dir = self.root / "output"
        self.snapshots_dir = self.root / "snapshots"
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.snapshots_dir.mkdir(parents=True, exist_ok=True)
        self.heartbeat = self.root / "heartbeat.log"
        # Create + fsync an empty heartbeat so its directory entry is on disk
        # before the first test starts.
        with open(self.heartbeat, "wb") as f:
            os.fsync(f.fileno())

    def _fsync_append(self, path: Path, text: str) -> None:
        with open(path, "ab", buffering=0) as f:
            f.write(text.encode("utf-8"))
            os.fsync(f.fileno())

    def heartbeat_start(self, test_name: str, pid: int) -> None:
        self._fsync_append(
            self.heartbeat,
            f"START {_iso_now()} {test_name} pid={pid}\n",
        )

    def heartbeat_end(
        self,
        test_name: str,
        rc: int | None,
        elapsed_ms: int,
        killed: str | None,
    ) -> None:
        rc_s = "-" if rc is None else str(rc)
        killed_s = killed or "-"
        self._fsync_append(
            self.heartbeat,
            f"END {_iso_now()} {test_name} rc={rc_s} elapsed_ms={elapsed_ms} killed={killed_s}\n",
        )

    def snapshot(self, test_name: str, when: str) -> None:
        snap = _capture_snapshot()
        path = self.snapshots_dir / f"{_safe_name(test_name)}.{when}.json"
        path.write_text(json.dumps(snap, indent=2))

    def output_path(self, test_name: str) -> Path:
        return self.output_dir / f"{_safe_name(test_name)}.log"

    def artifacts(self) -> dict[str, str]:
        return {
            "heartbeat": str(self.heartbeat),
            "output_dir": str(self.output_dir),
            "snapshots_dir": str(self.snapshots_dir),
        }


def _artifacts_section(recorder: _ForensicsRecorder) -> str:
    a = recorder.artifacts()
    return (
        "Forensic artifacts (if the host wedges mid-run, look here first):\n"
        f"  heartbeat:      {a['heartbeat']}\n"
        f"  output dir:     {a['output_dir']}\n"
        f"  snapshots dir:  {a['snapshots_dir']}\n"
    )


def _run_with_forensics(
    cmd: list[str],
    cwd: str,
    env: dict | None,
    output_log: Path,
    timeout_s: float = TEST_TIMEOUT_S,
    abort_grace_s: float = TEST_ABORT_GRACE_S,
    on_start=None,
) -> dict:
    """
    Run cmd in its own session, tee combined stdout+stderr to output_log with
    fsync after every chunk, and enforce a wall-clock timeout that escalates
    SIGABRT → wait → SIGKILL on the process group.

    The new session means killpg() reaches the whole subtree (e.g. setarch
    → stdbuf → ctest → test_binary all die together).

    on_start, if given, is called with the spawned pid right after Popen —
    used by the caller to write the heartbeat START line with the real pid.

    Returns dict: {rc, captured, pid, elapsed_ms, kill_reason, success}.
    """
    full_env = os.environ.copy()
    if env:
        full_env.update(env)

    proc = subprocess.Popen(
        cmd,
        cwd=cwd,
        env=full_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    if on_start is not None:
        try:
            on_start(proc.pid)
        except Exception:
            pass

    captured = bytearray()
    out_f = open(output_log, "wb", buffering=0)

    def _reader():
        # read1 returns whatever is immediately available; loop until EOF.
        while True:
            try:
                chunk = proc.stdout.read1(4096)
            except (ValueError, OSError):
                return
            if not chunk:
                return
            captured.extend(chunk)
            try:
                out_f.write(chunk)
                os.fsync(out_f.fileno())
            except Exception:
                # Filesystem hiccup — keep capturing in memory so we don't
                # lose what's in the pipe.
                pass

    t = threading.Thread(target=_reader, daemon=True)
    t.start()

    start = time.monotonic()
    kill_reason: str | None = None
    abort_at: float | None = None

    while True:
        rc = proc.poll()
        if rc is not None:
            break
        now = time.monotonic()
        if kill_reason is None and (now - start) >= timeout_s:
            kill_reason = "SIGABRT"
            abort_at = now
            try:
                os.killpg(proc.pid, signal.SIGABRT)
            except ProcessLookupError:
                pass
        elif (kill_reason == "SIGABRT"
              and abort_at is not None
              and (now - abort_at) >= abort_grace_s):
            kill_reason = "SIGKILL"
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        time.sleep(0.1)

    # Drain the pipe — give the reader a moment to flush the last chunks.
    t.join(timeout=5.0)
    try:
        out_f.close()
    except Exception:
        pass

    elapsed_ms = int((time.monotonic() - start) * 1000)
    rc = proc.returncode
    success = rc == 0 and kill_reason is None
    return {
        "rc": rc,
        "captured": captured.decode("utf-8", errors="replace"),
        "pid": proc.pid,
        "elapsed_ms": elapsed_ms,
        "kill_reason": kill_reason,
        "success": success,
    }


async def _run_one_test_with_forensics(
    cmd: list[str],
    cwd: str,
    env: dict | None,
    test_name: str,
    recorder: _ForensicsRecorder,
) -> dict:
    """Wrap _run_with_forensics with the per-test forensics protocol:
    pre-snapshot → heartbeat START (with real pid) → run → heartbeat END →
    post-snapshot.
    """
    recorder.snapshot(test_name, "before")

    def _on_start(pid: int) -> None:
        recorder.heartbeat_start(test_name, pid)

    result = await asyncio.to_thread(
        _run_with_forensics,
        cmd,
        cwd,
        env,
        recorder.output_path(test_name),
        TEST_TIMEOUT_S,
        TEST_ABORT_GRACE_S,
        _on_start,
    )
    recorder.heartbeat_end(
        test_name,
        rc=result["rc"],
        elapsed_ms=result["elapsed_ms"],
        killed=result["kill_reason"],
    )
    recorder.snapshot(test_name, "after")
    return result


async def _enumerate_ctest_tests(pd: Path, build_dir: str, env: dict | None) -> list[dict]:
    """
    Return per-test metadata via ctest's --show-only=json-v1, as a list of
    dicts: {"name": str, "command": list[str], "cwd": str, "env": dict[str,str]}.

    We use the json-v1 surface (not `ctest -N`) so we get the actual test
    binary path and can execute it directly, bypassing ctest at run time.
    That isolation matters because ctest is not sanitizer-instrumented;
    pass deps_env only here (no sanitizer env) so an accidental future
    LD_PRELOAD or aggressive ASAN_OPTIONS setting can't crash ctest itself.
    """
    ok, out = await _run_async(
        ["ctest", "--test-dir", build_dir, "--show-only=json-v1"],
        cwd=str(pd),
        env=env,
    )
    if not ok:
        return []
    try:
        data = json.loads(out)
    except Exception:
        return []
    tests: list[dict] = []
    for t in data.get("tests", []):
        name = t.get("name")
        command = t.get("command") or []
        if not name or not command:
            continue
        cwd = str(pd)
        env_kv: dict[str, str] = {}
        for p in t.get("properties", []):
            pn = p.get("name")
            pv = p.get("value")
            if pn == "WORKING_DIRECTORY" and isinstance(pv, str):
                cwd = pv
            elif pn == "ENVIRONMENT" and pv:
                items = pv if isinstance(pv, list) else [pv]
                for item in items:
                    if isinstance(item, str) and "=" in item:
                        k, _, v = item.partition("=")
                        env_kv[k] = v
        tests.append({
            "name": name,
            "command": list(command),
            "cwd": cwd,
            "env": env_kv,
        })
    return tests


async def _enumerate_meson_tests(pd: Path, build_dir: str, env: dict | None) -> list[str]:
    """Return meson's known test names."""
    _ok, out = await _run_async(
        ["meson", "test", "-C", build_dir, "--list"],
        cwd=str(pd),
        env=env,
    )
    names: list[str] = []
    for line in out.splitlines():
        s = line.strip()
        if not s:
            continue
        # meson can prefix lines with "suite / name"; take the last segment.
        names.append(s.split("/")[-1].strip())
    return names


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
    deps_env = _deps_env(pd) or None
    lines = [f"Backend: {backend}"]

    if backend == "cmake":
        build_dir = pd / "build"
        if not (build_dir / "CMakeCache.txt").exists():
            ok, out = await _run_async(
                ["cmake", "-S", ".", "-B", "build", "-G", "Ninja"],
                cwd=str(pd),
                env=deps_env,
            )
            lines.append(f"-- cmake configure ({'ok' if ok else 'failed'}) --\n{out}")
            if not ok:
                result = "BUILD FAILED ✗\n\n" + "\n\n".join(lines)
                _report("build", {"project": project}, result, False)
                return result
        ok, out = await _run_async(["cmake", "--build", "build"], cwd=str(pd), env=deps_env)
        lines.append(f"-- cmake build ({'ok' if ok else 'failed'}) --\n{out}")

    elif backend == "meson":
        build_dir = pd / "build"
        if not (build_dir / "build.ninja").exists():
            ok, out = await _run_async(["meson", "setup", "build"], cwd=str(pd), env=deps_env)
            lines.append(f"-- meson setup ({'ok' if ok else 'failed'}) --\n{out}")
            if not ok:
                result = "BUILD FAILED ✗\n\n" + "\n\n".join(lines)
                _report("build", {"project": project}, result, False)
                return result
        ok, out = await _run_async(["meson", "compile", "-C", "build"], cwd=str(pd), env=deps_env)
        lines.append(f"-- meson compile ({'ok' if ok else 'failed'}) --\n{out}")

    elif backend == "make":
        ok, out = await _run_async(["make"], cwd=str(pd), env=deps_env)
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
        ok, out = await _run_async(cmd, cwd=str(pd), env=deps_env)
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


def _parse_meson(pd: Path, build_dir: str = "build") -> list[dict]:
    """Read meson's machine-readable testlog.json if present."""
    log_path = pd / build_dir / "meson-logs" / "testlog.json"
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
    deps_env = _deps_env(pd) or None
    tests: list[dict] = []
    log = ""
    ok = True

    if backend == "cmake":
        if not (pd / "build").is_dir():
            return _fail_no_build("run_tests", project, test_filter)
        cmd = ["ctest", "--test-dir", "build", "--output-on-failure"]
        if test_filter:
            cmd += ["-R", test_filter]
        ok, log = await _run_async(cmd, cwd=str(pd), env=deps_env)
        tests = _parse_ctest(log)

    elif backend == "meson":
        if not (pd / "build").is_dir():
            return _fail_no_build("run_tests", project, test_filter)
        cmd = ["meson", "test", "-C", "build", "--print-errorlogs"]
        if test_filter:
            cmd.append(test_filter)
        ok, log = await _run_async(cmd, cwd=str(pd), env=deps_env)
        tests = _parse_meson(pd)

    elif backend == "make":
        ok, log = await _run_async(["make", "test"], cwd=str(pd), env=deps_env)
        # If `make test` doesn't exist, try `make check`.
        if not ok and "No rule to make target" in log:
            ok2, log2 = await _run_async(["make", "check"], cwd=str(pd), env=deps_env)
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
            success, out = await _run_async([str(b)], cwd=str(pd), env=deps_env)
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
    skip_parts = {"build", "build-make", ".git", "node_modules", FORENSICS_DIRNAME}
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

    deps_env = _deps_env(pd) or None
    lines: list[str] = []

    # clang-tidy — use compile_commands.json if cmake/meson generated one.
    tidy_cmd = ["clang-tidy"]
    cdb = pd / "build" / "compile_commands.json"
    if cdb.exists():
        tidy_cmd += ["-p", str(cdb.parent)]
    tidy_cmd += [str(t) for t in targets if t.suffix == ".c"]
    if len(tidy_cmd) > (3 if cdb.exists() else 1):
        tidy_ok, tidy_out = await _run_async(tidy_cmd, cwd=str(pd), env=deps_env)
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
    cpp_ok, cpp_out = await _run_async(cppcheck_cmd, cwd=str(pd), env=deps_env)
    lines.append(f"-- cppcheck ({'ok' if cpp_ok else 'issues'}) --\n{cpp_out}")

    ok = tidy_ok and cpp_ok
    header = "LINT CLEAN ✓" if ok else "LINT FAILED ✗"
    result = f"{header}\n\n" + "\n\n".join(lines)
    _report("lint", {"project": project}, result, ok)
    return result


# ── analyze ───────────────────────────────────────────────────────────────────

@mcp.tool()
async def analyze(project: str, tool: str = "valgrind", scope: str = "") -> str:
    """
    Run dynamic analysis on the project's test binaries.

    tool="valgrind" (default): run every build*/test_* under
        valgrind --leak-check=full --error-exitcode=23. Use the existing
        build — no recompile.

    tool="asan": rebuild with -fsanitize=address,undefined into build-asan/
        and run the resulting test binaries.
          - direct: compiles all sources into one build-asan/<project>-asan.
          - cmake:  configures build-asan/ with -DCMAKE_C_FLAGS injecting
                    the sanitizer, builds, then runs ctest.
          - meson:  sets up build-asan/ with c_args/c_link_args injecting
                    the sanitizer, compiles, then runs meson test.
          - make:   not supported (Makefiles vary too much to inject
                    sanitizer flags reliably; set CFLAGS in your Makefile).

        Test runs set ASAN_OPTIONS=verify_asan_link_order=0 so tests that
        dlopen non-instrumented provider/plugin .so files don't fail with
        "ASan runtime does not come first in initial library list".

    tool="tsan": same backend matrix as asan, but with
        -fsanitize=thread,undefined and TSAN_OPTIONS/UBSAN_OPTIONS set to
        halt on the first finding. Direct mode builds one binary per
        test_*.c into build-tsan/test_* and runs each.

        Test runners are wrapped with `setarch -R` to disable ASLR for the
        test process tree (TSan's shadow memory mapping can't coexist with
        high-entropy ASLR). That requires the personality() syscall, which
        the container runtime's default seccomp profile filters with ENOSYS.
        Restart the c-mcp-build container under a relaxed profile before
        running tsan analyses:
            docker rm -f c-mcp-build
            SECCOMP_PROFILE=$PWD/service/seccomp/tsan.json \\
                ./service/start-container.sh
        Symptom of forgetting this step: every test fails with
        "setarch: failed to set personality" in the test output.

    Forensics. Every analyze() run records, under
    <project>/.forensics/<tool>/:
        heartbeat.log           — fsync'd START/END line per test, so if
                                  the host wedges mid-run the file shows
                                  exactly which test was in flight.
        output/<test>.log       — live, line-flushed, fsync'd capture of
                                  combined stdout+stderr (so output past
                                  the last flush isn't lost).
        snapshots/<test>.before.json, <test>.after.json
                                — meminfo / loadavg / proc count / cgroup
                                  memory.{current,max} and pids.{current,max}
                                  before and after each test.
    Tests are run one at a time under sanitizer modes (serial = the brief's
    "ctest --jobs 1 under sanitizers" item). Each test has a wall-clock
    timeout of MCP_C_TEST_TIMEOUT_S seconds (default 60); on timeout the
    test's process group is sent SIGABRT (letting ASan/TSan dump state to
    output/<test>.log) and SIGKILL after a grace period.

    Items the brief asks for that need host-side tooling and so are NOT
    captured by this service (do them by hand after a wedge):
      - post-reboot kernel ring (journalctl -k -b -1)
      - container event tap     (podman events --format json)

    Args:
        project: Folder name under ~/Projects.
        tool:    'valgrind', 'asan', or 'tsan'.
        scope:   Optional test name filter. For ctest it's matched as a
                 regex against test names (same as `ctest -R`). For meson,
                 matched as a substring of the test name. For direct-compile
                 and valgrind, matched as a substring of the binary basename.
                 Use this to sanitize-run a single test without rebuilding.
    """
    try:
        pd = _project_dir(project)
    except Exception as e:
        result = f"ANALYZE FAILED\n\n{e}"
        _report("analyze", {"project": project, "tool": tool, "scope": scope}, result, False)
        return result

    if tool == "valgrind":
        return await _analyze_valgrind(project, pd, scope)
    if tool == "asan":
        return await _analyze_asan(project, pd, scope)
    if tool == "tsan":
        return await _analyze_tsan(project, pd, scope)
    result = f"ANALYZE FAILED ✗\n\nUnknown tool {tool!r}. Use 'valgrind', 'asan', or 'tsan'."
    _report("analyze", {"project": project, "tool": tool, "scope": scope}, result, False)
    return result


async def _analyze_valgrind(project: str, pd: Path, scope: str = "") -> str:
    bins = _gather_test_binaries(pd)
    if scope:
        bins = [b for b in bins if scope in b.name]
    if not bins:
        result = (
            "ANALYZE FAILED ✗\n\n"
            "No build*/test_* binaries to analyse. Run build() and run_tests() first."
            + (f"\nNote: scope={scope!r} filtered all binaries out." if scope else "")
        )
        _report("analyze", {"project": project, "tool": "valgrind", "scope": scope}, result, False)
        return result

    deps_env = _deps_env(pd) or None
    recorder = _ForensicsRecorder(pd, "valgrind")
    sections: list[str] = []
    findings: list[dict] = []
    overall_ok = True

    for b in bins:
        name = b.name
        cmd = [
            "stdbuf", "-o0", "-e0",
            "valgrind",
            "--leak-check=full",
            "--show-leak-kinds=all",
            "--track-origins=yes",
            "--error-exitcode=23",
            str(b),
        ]
        result = await _run_one_test_with_forensics(
            cmd, str(pd), deps_env, name, recorder,
        )
        ok = result["success"]
        out = result["captured"]
        timeout_note = ""
        if result["kill_reason"]:
            timeout_note = (
                f" [{result['kill_reason']} after {result['elapsed_ms']}ms]"
            )
            ok = False
        sections.append(
            f"-- {b.relative_to(pd)} ({'clean' if ok else 'errors'}){timeout_note} --\n{out}"
        )
        findings.append({"node_id": name, "passed": ok})
        overall_ok = overall_ok and ok

    log = "\n\n".join(sections)
    header = "ANALYZE CLEAN ✓" if overall_ok else "ANALYZE FOUND ISSUES ✗"
    summary = (
        f"tool=valgrind binaries={len(bins)} "
        f"clean={sum(1 for f in findings if f['passed'])} "
        f"with_errors={sum(1 for f in findings if not f['passed'])}"
    )
    if scope:
        summary += f" scope={scope!r}"

    artifacts = _artifacts_section(recorder)

    ingest_payload = json.dumps({
        "summary": f"{header}\n{summary}",
        "findings": findings,
        "stdout": log[-12000:],
        "artifacts": recorder.artifacts(),
    })
    _report(
        "analyze",
        {"project": project, "tool": "valgrind", "scope": scope},
        ingest_payload,
        overall_ok,
    )

    tail = log[-2500:]
    return f"{header}\n\n{summary}\n\n{artifacts}\n--- valgrind output (tail) ---\n{tail}"


async def _analyze_sanitizer_cmake(
    project: str,
    pd: Path,
    tool: str,
    san_flags: str,
    san_env: dict[str, str],
    exe_link_extra: str = "",
    extra_defines: list[str] | None = None,
    test_wrapper: list[str] | None = None,
    scope: str = "",
) -> str:
    """Configure build-{tool}/ with sanitizer flags injected via CMAKE_C_FLAGS,
    build, then run ctest with the sanitizer's runtime env vars set.
    Each invocation reconfigures (idempotent — cmake re-applies -D values),
    so switching between asan and tsan never sees stale cache state because
    they use disjoint build directories.

    exe_link_extra is appended to CMAKE_EXE_LINKER_FLAGS only (not the shared
    one) — that's the slot for things like -no-pie which executables need but
    shared libs must not see (they require -fPIC and -no-pie would break them).

    extra_defines are appended to the cmake configure line (e.g.
    "-DCMAKE_POSITION_INDEPENDENT_CODE=OFF" for tsan, which stops cmake from
    emitting -pie per-target — that override would otherwise win against any
    -no-pie we put in CMAKE_EXE_LINKER_FLAGS because of link-line ordering)."""
    build_dir = f"build-{tool}"
    out_dir = pd / build_dir
    deps_env = _deps_env(pd) or None
    sections: list[str] = []

    exe_link_flags = f"{san_flags} {exe_link_extra}".strip()
    cfg_cmd = [
        "cmake", "-S", ".", "-B", build_dir, "-G", "Ninja",
        f"-DCMAKE_C_FLAGS={san_flags}",
        f"-DCMAKE_EXE_LINKER_FLAGS={exe_link_flags}",
        f"-DCMAKE_SHARED_LINKER_FLAGS={san_flags}",
        *(extra_defines or []),
    ]
    ok, out = await _run_async(cfg_cmd, cwd=str(pd), env=deps_env)
    sections.append(f"-- cmake configure ({'ok' if ok else 'failed'}) --\n{out}")
    if not ok:
        result = f"ANALYZE FAILED ✗\n\n" + "\n\n".join(sections)
        _report("analyze", {"project": project, "tool": tool}, result, False)
        return result

    ok, out = await _run_async(
        ["cmake", "--build", build_dir], cwd=str(pd), env=deps_env,
    )
    sections.append(f"-- cmake build ({'ok' if ok else 'failed'}) --\n{out}")
    if not ok:
        result = f"ANALYZE FAILED ✗\n\n" + "\n\n".join(sections)
        _report("analyze", {"project": project, "tool": tool, "scope": scope}, result, False)
        return result

    # Enumerate with deps_env only; ctest itself isn't sanitizer-instrumented
    # and is kept out of the san_env scope for safety even though we no longer
    # LD_PRELOAD libasan (see ASAN_RUN_ENV docstring for history).
    tests_meta = await _enumerate_ctest_tests(pd, build_dir, deps_env)
    if scope:
        try:
            rx = re.compile(scope)
        except re.error:
            rx = re.compile(re.escape(scope))
        tests_meta = [t for t in tests_meta if rx.search(t["name"])]

    recorder = _ForensicsRecorder(pd, tool)
    findings: list[dict] = []
    overall_ok = True

    if not tests_meta:
        # Fall back to a one-shot ctest run if json-v1 returned nothing
        # (project with non-ctest tests, an older cmake, or an empty
        # filter).
        if scope:
            msg = f"(no ctest tests matched scope={scope!r})"
        else:
            msg = "(ctest --show-only=json-v1 returned no tests; running a single ctest pass)"
        sections.append(f"-- ctest enumerate --\n{msg}\n")
        if not scope:
            fallback_env = {**(deps_env or {}), **san_env}
            fallback_cmd = [
                *(test_wrapper or []),
                "ctest", "--test-dir", build_dir, "--output-on-failure",
            ]
            ok, out = await _run_async(fallback_cmd, cwd=str(pd), env=fallback_env)
            sections.append(f"-- ctest ({'clean' if ok else 'errors'}) --\n{out}")
            findings = _parse_ctest(out) or [{"node_id": f"ctest-{tool}", "passed": ok}]
            overall_ok = ok
    else:
        # Run each test's binary directly (bypassing ctest at execution).
        # Per-test ENVIRONMENT properties (from add_test) get merged in.
        base_run_env = {**(deps_env or {}), **san_env}
        for t in tests_meta:
            name = t["name"]
            test_env = {**base_run_env, **t["env"]}
            cmd = [
                *(test_wrapper or []),
                "stdbuf", "-o0", "-e0",
                *t["command"],
            ]
            result = await _run_one_test_with_forensics(
                cmd, t["cwd"], test_env, name, recorder,
            )
            ok = result["success"]
            timeout_note = ""
            if result["kill_reason"]:
                timeout_note = f" [{result['kill_reason']} after {result['elapsed_ms']}ms]"
                ok = False
            sections.append(
                f"-- test {name} ({'clean' if ok else 'errors'}){timeout_note} --\n"
                f"{result['captured']}"
            )
            findings.append({"node_id": name, "passed": ok})
            overall_ok = overall_ok and ok

    log = "\n\n".join(sections)
    header = "ANALYZE CLEAN ✓" if overall_ok else "ANALYZE FOUND ISSUES ✗"
    summary = (
        f"tool={tool} backend=cmake build_dir={build_dir} "
        f"tests={len(findings)} "
        f"clean={sum(1 for f in findings if f['passed'])} "
        f"with_errors={sum(1 for f in findings if not f['passed'])}"
    )
    if scope:
        summary += f" scope={scope!r}"

    artifacts = _artifacts_section(recorder)

    ingest_payload = json.dumps({
        "summary": f"{header}\n{summary}",
        "findings": findings,
        "stdout": log[-12000:],
        "artifacts": recorder.artifacts(),
    })
    _report("analyze", {"project": project, "tool": tool, "scope": scope}, ingest_payload, overall_ok)
    tail = log[-2500:]
    return f"{header}\n\n{summary}\n\n{artifacts}\n--- {tool} output (tail) ---\n{tail}"


async def _analyze_sanitizer_meson(
    project: str,
    pd: Path,
    tool: str,
    san_flags: str,
    san_env: dict[str, str],
    extra_setup_args: list[str] | None = None,
    test_wrapper: list[str] | None = None,
    scope: str = "",
) -> str:
    """Set up build-{tool}/ with sanitizer flags injected via c_args/c_link_args
    (meson's b_sanitize accepts only a fixed set of values, so the explicit
    args route is what works for every flag combo we care about), compile,
    then run meson test with the sanitizer's runtime env vars set.

    extra_setup_args is appended to the `meson setup` invocation — used by
    tsan to pass -Db_pie=false (TSan + PIE + high-entropy ASLR is incompatible;
    b_pie scopes the disable to executables and leaves shared libs PIC)."""
    build_dir = f"build-{tool}"
    out_dir = pd / build_dir
    deps_env = _deps_env(pd) or None
    sections: list[str] = []
    flag_list = san_flags.split()

    if not (out_dir / "build.ninja").exists():
        setup_cmd = [
            "meson", "setup", build_dir,
            f"-Dc_args={' '.join(flag_list)}",
            f"-Dc_link_args={' '.join(flag_list)}",
            *(extra_setup_args or []),
        ]
        ok, out = await _run_async(setup_cmd, cwd=str(pd), env=deps_env)
        sections.append(f"-- meson setup ({'ok' if ok else 'failed'}) --\n{out}")
        if not ok:
            result = f"ANALYZE FAILED ✗\n\n" + "\n\n".join(sections)
            _report("analyze", {"project": project, "tool": tool}, result, False)
            return result

    ok, out = await _run_async(
        ["meson", "compile", "-C", build_dir], cwd=str(pd), env=deps_env,
    )
    sections.append(f"-- meson compile ({'ok' if ok else 'failed'}) --\n{out}")
    if not ok:
        result = f"ANALYZE FAILED ✗\n\n" + "\n\n".join(sections)
        _report("analyze", {"project": project, "tool": tool, "scope": scope}, result, False)
        return result

    run_env = {**(deps_env or {}), **san_env}

    test_names = await _enumerate_meson_tests(pd, build_dir, run_env)
    if scope:
        test_names = [n for n in test_names if scope in n]

    recorder = _ForensicsRecorder(pd, tool)
    findings: list[dict] = []
    overall_ok = True

    if not test_names:
        msg = (
            f"(no meson tests matched scope={scope!r})"
            if scope
            else "(meson test --list returned no tests; running a single meson test pass)"
        )
        sections.append(f"-- meson enumerate --\n{msg}\n")
        if not scope:
            fallback_cmd = [
                *(test_wrapper or []),
                "meson", "test", "-C", build_dir, "--print-errorlogs",
            ]
            ok, out = await _run_async(fallback_cmd, cwd=str(pd), env=run_env)
            sections.append(f"-- meson test ({'clean' if ok else 'errors'}) --\n{out}")
            findings = _parse_meson(pd, build_dir) or [
                {"node_id": f"meson-test-{tool}", "passed": ok}
            ]
            overall_ok = ok
    else:
        for name in test_names:
            cmd = [
                *(test_wrapper or []),
                "stdbuf", "-o0", "-e0",
                "meson", "test", "-C", build_dir, "--verbose", name,
            ]
            result = await _run_one_test_with_forensics(
                cmd, str(pd), run_env, name, recorder,
            )
            ok = result["success"]
            timeout_note = ""
            if result["kill_reason"]:
                timeout_note = f" [{result['kill_reason']} after {result['elapsed_ms']}ms]"
                ok = False
            sections.append(
                f"-- meson test {name} ({'clean' if ok else 'errors'}){timeout_note} --\n"
                f"{result['captured']}"
            )
            findings.append({"node_id": name, "passed": ok})
            overall_ok = overall_ok and ok

    log = "\n\n".join(sections)
    header = "ANALYZE CLEAN ✓" if overall_ok else "ANALYZE FOUND ISSUES ✗"
    summary = (
        f"tool={tool} backend=meson build_dir={build_dir} "
        f"tests={len(findings)} "
        f"clean={sum(1 for f in findings if f['passed'])} "
        f"with_errors={sum(1 for f in findings if not f['passed'])}"
    )
    if scope:
        summary += f" scope={scope!r}"

    artifacts = _artifacts_section(recorder)

    ingest_payload = json.dumps({
        "summary": f"{header}\n{summary}",
        "findings": findings,
        "stdout": log[-12000:],
        "artifacts": recorder.artifacts(),
    })
    _report("analyze", {"project": project, "tool": tool, "scope": scope}, ingest_payload, overall_ok)
    tail = log[-2500:]
    return f"{header}\n\n{summary}\n\n{artifacts}\n--- {tool} output (tail) ---\n{tail}"


async def _analyze_asan(project: str, pd: Path, scope: str = "") -> str:
    backend = _detect_backend(pd)
    if backend == "cmake":
        return await _analyze_sanitizer_cmake(
            project, pd, "asan", ASAN_CFLAGS, ASAN_RUN_ENV, scope=scope,
        )
    if backend == "meson":
        return await _analyze_sanitizer_meson(
            project, pd, "asan", ASAN_CFLAGS, ASAN_RUN_ENV, scope=scope,
        )
    if backend == "direct" and _gather_c_sources(pd):
        return await _analyze_asan_direct(project, pd, scope)
    result = (
        "ANALYZE FAILED ✗\n\n"
        "asan path supports cmake, meson, and direct-compile projects, but not "
        "make. For make projects, set CFLAGS='-fsanitize=address,undefined -g -O1' "
        "in your Makefile and use analyze(tool='valgrind') instead."
    )
    _report("analyze", {"project": project, "tool": "asan", "scope": scope}, result, False)
    return result


async def _analyze_asan_direct(project: str, pd: Path, scope: str = "") -> str:
    sources = _gather_c_sources(pd)
    out_dir = pd / "build-asan"
    out_dir.mkdir(exist_ok=True)
    out_bin = out_dir / f"{project}-asan"

    deps_env = _deps_env(pd)
    cmd = [
        CC, *CFLAGS.split(), *ASAN_CFLAGS.split(),
        *[str(s) for s in sources], "-o", str(out_bin),
    ]
    ok, out = await _run_async(cmd, cwd=str(pd), env=deps_env or None)
    if not ok:
        result = f"ANALYZE FAILED ✗\n\n-- asan rebuild --\n{out}"
        _report("analyze", {"project": project, "tool": "asan", "scope": scope}, result, False)
        return result

    if scope and scope not in out_bin.name:
        result = (
            f"ANALYZE FAILED ✗\n\nscope={scope!r} did not match the rebuilt "
            f"asan binary {out_bin.name!r}; direct-compile asan produces a single binary."
        )
        _report("analyze", {"project": project, "tool": "asan", "scope": scope}, result, False)
        return result

    # Run the rebuilt binary with forensics; ASan exits non-zero on detection.
    env = {**deps_env, **ASAN_RUN_ENV}
    recorder = _ForensicsRecorder(pd, "asan")
    run_cmd = ["stdbuf", "-o0", "-e0", str(out_bin)]
    result = await _run_one_test_with_forensics(
        run_cmd, str(pd), env, out_bin.name, recorder,
    )
    run_ok = result["success"]
    timeout_note = ""
    if result["kill_reason"]:
        timeout_note = f" [{result['kill_reason']} after {result['elapsed_ms']}ms]"
        run_ok = False
    log = (
        f"-- asan rebuild --\n{out}\n\n"
        f"-- asan run ({'clean' if run_ok else 'errors'}){timeout_note} --\n{result['captured']}"
    )

    header = "ANALYZE CLEAN ✓" if run_ok else "ANALYZE FOUND ISSUES ✗"
    findings = [{"node_id": out_bin.name, "passed": run_ok}]
    summary = f"tool=asan binary={out_bin.name} clean={run_ok}"

    artifacts = _artifacts_section(recorder)

    ingest_payload = json.dumps({
        "summary": f"{header}\n{summary}",
        "findings": findings,
        "stdout": log[-12000:],
        "artifacts": recorder.artifacts(),
    })
    _report(
        "analyze",
        {"project": project, "tool": "asan", "scope": scope},
        ingest_payload,
        run_ok,
    )

    tail = log[-2500:]
    return f"{header}\n\n{summary}\n\n{artifacts}\n--- asan output (tail) ---\n{tail}"


async def _analyze_tsan(project: str, pd: Path, scope: str = "") -> str:
    backend = _detect_backend(pd)
    # TSan is incompatible with PIE under high-entropy ASLR ("unexpected memory
    # mapping") — disable PIE on test executables only. Shared libs must stay
    # PIC, so we don't touch CMAKE_SHARED_LINKER_FLAGS / c_link_args directly.
    # For cmake we need both knobs:
    #   POSITION_INDEPENDENT_CODE=OFF stops cmake's per-target machinery from
    #     appending -pie to executable link lines (which would override -no-pie
    #     due to link-line ordering). Static archives don't need PIC; shared
    #     libs are forced-PIC by cmake regardless of this variable.
    #   -no-pie is belt-and-suspenders for projects that hardcode -pie via
    #     target_link_options or similar per-target overrides.
    if backend == "cmake":
        return await _analyze_sanitizer_cmake(
            project, pd, "tsan", TSAN_CFLAGS, TSAN_RUN_ENV,
            exe_link_extra="-no-pie",
            extra_defines=["-DCMAKE_POSITION_INDEPENDENT_CODE=OFF"],
            test_wrapper=TSAN_RUN_WRAPPER,
            scope=scope,
        )
    if backend == "meson":
        return await _analyze_sanitizer_meson(
            project, pd, "tsan", TSAN_CFLAGS, TSAN_RUN_ENV,
            extra_setup_args=["-Db_pie=false"],
            test_wrapper=TSAN_RUN_WRAPPER,
            scope=scope,
        )
    if backend == "direct" and _gather_c_sources(pd):
        return await _analyze_tsan_direct(project, pd, scope)
    result = (
        "ANALYZE FAILED ✗\n\n"
        "tsan path supports cmake, meson, and direct-compile projects, but not "
        "make. For make projects, set "
        "CFLAGS='-fsanitize=thread,undefined -fno-omit-frame-pointer -g' "
        "in your Makefile and use analyze(tool='valgrind') instead."
    )
    _report("analyze", {"project": project, "tool": "tsan", "scope": scope}, result, False)
    return result


async def _analyze_tsan_direct(project: str, pd: Path, scope: str = "") -> str:
    sources = _gather_c_sources(pd)
    test_sources = [s for s in sources if s.name.startswith("test_")]
    lib_sources = [s for s in sources if not s.name.startswith("test_")]
    if not test_sources:
        result = (
            "ANALYZE FAILED ✗\n\n"
            "tsan path needs one or more test_*.c sources under src/ (or root) "
            "to build per-test binaries. None were found."
        )
        _report("analyze", {"project": project, "tool": "tsan", "scope": scope}, result, False)
        return result

    out_dir = pd / "build-tsan"
    out_dir.mkdir(exist_ok=True)
    deps_env = _deps_env(pd)

    sections: list[str] = []
    built_bins: list[Path] = []

    # Compile each test_*.c into its own build-tsan/<basename> binary, linking
    # in the non-test sources. Stop on first compile failure — partial builds
    # would just produce confusing run results.
    for ts in test_sources:
        out_bin = out_dir / ts.stem
        cmd = [
            CC, *CFLAGS.split(), *TSAN_CFLAGS.split(),
            str(ts), *[str(s) for s in lib_sources],
            "-o", str(out_bin),
        ]
        if LDFLAGS:
            cmd += LDFLAGS.split()
        ok, out = await _run_async(cmd, cwd=str(pd), env=deps_env or None)
        sections.append(f"-- tsan rebuild {ts.relative_to(pd)} ({'ok' if ok else 'failed'}) --\n{out}")
        if not ok:
            log = "\n\n".join(sections)
            result = f"ANALYZE FAILED ✗\n\n{log}"
            _report("analyze", {"project": project, "tool": "tsan", "scope": scope}, result, False)
            return result
        built_bins.append(out_bin)

    # Filter by scope after build (we still need to compile the libs).
    run_bins = (
        [b for b in built_bins if scope in b.name] if scope else built_bins
    )
    if not run_bins:
        result = (
            f"ANALYZE FAILED ✗\n\nscope={scope!r} matched none of: "
            + ", ".join(b.name for b in built_bins)
        )
        _report("analyze", {"project": project, "tool": "tsan", "scope": scope}, result, False)
        return result

    # Run each rebuilt binary under forensics; TSan/UBSan exit non-zero
    # on detection.
    env = {**deps_env, **TSAN_RUN_ENV}
    recorder = _ForensicsRecorder(pd, "tsan")
    findings: list[dict] = []
    overall_ok = True
    for b in run_bins:
        cmd = [*TSAN_RUN_WRAPPER, "stdbuf", "-o0", "-e0", str(b)]
        result = await _run_one_test_with_forensics(
            cmd, str(pd), env, b.name, recorder,
        )
        run_ok = result["success"]
        timeout_note = ""
        if result["kill_reason"]:
            timeout_note = f" [{result['kill_reason']} after {result['elapsed_ms']}ms]"
            run_ok = False
        sections.append(
            f"-- tsan run {b.relative_to(pd)} ({'clean' if run_ok else 'errors'}){timeout_note} --\n"
            f"{result['captured']}"
        )
        findings.append({"node_id": b.name, "passed": run_ok})
        overall_ok = overall_ok and run_ok

    log = "\n\n".join(sections)
    header = "ANALYZE CLEAN ✓" if overall_ok else "ANALYZE FOUND ISSUES ✗"
    summary = (
        f"tool=tsan binaries={len(run_bins)} "
        f"clean={sum(1 for f in findings if f['passed'])} "
        f"with_errors={sum(1 for f in findings if not f['passed'])}"
    )
    if scope:
        summary += f" scope={scope!r}"

    artifacts = _artifacts_section(recorder)

    ingest_payload = json.dumps({
        "summary": f"{header}\n{summary}",
        "findings": findings,
        "stdout": log[-12000:],
        "artifacts": recorder.artifacts(),
    })
    _report(
        "analyze",
        {"project": project, "tool": "tsan", "scope": scope},
        ingest_payload,
        overall_ok,
    )

    tail = log[-2500:]
    return f"{header}\n\n{summary}\n\n{artifacts}\n--- tsan output (tail) ---\n{tail}"


# ── install_dep helpers ───────────────────────────────────────────────────────

# arch slot in the manifest. The c-build container ships only the native amd64
# toolchain; cross-compile arches will extend _SUPPORTED_ARCHES once the matching
# crossbuild-essential-* packages get baked into the image.
_NATIVE_DEB_ARCH = "amd64"
_SUPPORTED_ARCHES = {"native", "amd64"}
_DEB_TO_MULTIARCH = {
    "amd64": "x86_64-linux-gnu",
    # Future: "arm64": "aarch64-linux-gnu", "armhf": "arm-linux-gnueabihf", ...
}


def _normalize_arch(arch: str) -> str:
    """Resolve a user-facing arch label to a Debian arch identifier."""
    a = (arch or "native").lower()
    if a == "native":
        return _NATIVE_DEB_ARCH
    if a not in _SUPPORTED_ARCHES:
        raise ValueError(
            f"arch={arch!r} not supported by this build container. "
            f"Supported: {sorted(_SUPPORTED_ARCHES)}. "
            "Cross-compile toolchains aren't installed; the c-build image only "
            "ships the native amd64 toolchain."
        )
    return a


def _multiarch_for(deb_arch: str) -> str:
    if deb_arch not in _DEB_TO_MULTIARCH:
        raise ValueError(f"No multiarch tuple known for deb arch {deb_arch!r}")
    return _DEB_TO_MULTIARCH[deb_arch]


def _splat_deb_staging(staging: Path, deps_root: Path, multiarch: str) -> list[str]:
    """
    Copy useful artefacts from a `dpkg-deb -x` staging tree into <deps_root>
    AND return the deps/-relative paths written. The full list (not a diff)
    is what gets recorded in the manifest, so a package's ownership claim
    survives even when another package shipped the same file first.

    Layout in the deb: /usr/include, /usr/lib/<multiarch>, /usr/lib/pkgconfig,
    /usr/share/pkgconfig. Layout in deps_root: include/, lib/, lib/pkgconfig/.
    """
    written: list[str] = []

    inc_src = staging / "usr" / "include"
    if inc_src.is_dir():
        written += _copy_tree_collect(inc_src, deps_root / "include", "include")

    lib_src_multi = staging / "usr" / "lib" / multiarch
    if lib_src_multi.is_dir():
        written += _copy_tree_collect(lib_src_multi, deps_root / "lib", "lib")

    lib_src = staging / "usr" / "lib"
    if lib_src.is_dir():
        for entry in lib_src.iterdir():
            if entry.name == multiarch:
                continue
            if entry.is_dir() and entry.name == "pkgconfig":
                written += _copy_tree_collect(entry, deps_root / "lib" / "pkgconfig", "lib/pkgconfig")
            elif entry.is_file() or entry.is_symlink():
                _copy_file(entry, deps_root / "lib" / entry.name)
                written.append(f"lib/{entry.name}")

    share_pc = staging / "usr" / "share" / "pkgconfig"
    if share_pc.is_dir():
        written += _copy_tree_collect(share_pc, deps_root / "lib" / "pkgconfig", "lib/pkgconfig")

    return sorted(set(written))


def _copy_tree_collect(src: Path, dst: Path, rel_prefix: str) -> list[str]:
    """
    Merge-copy src into dst, preserving symlinks, and return the deps/-relative
    paths of every file/symlink written (with rel_prefix prepended). Pre-existing
    files at the destination ARE included in the returned list — the goal is to
    record what THIS install would contribute, not just what was new.
    """
    written: list[str] = []
    dst.mkdir(parents=True, exist_ok=True)
    for item in src.iterdir():
        target = dst / item.name
        rel = f"{rel_prefix}/{item.name}"
        if item.is_symlink():
            link = os.readlink(item)
            if target.exists() or target.is_symlink():
                target.unlink()
            os.symlink(link, target)
            written.append(rel)
        elif item.is_dir():
            written += _copy_tree_collect(item, target, rel)
        else:
            _copy_file(item, target)
            written.append(rel)
    return written


def _merge_staging_into(src_root: Path, dst_root: Path) -> list[str]:
    """
    Copy every file under src_root into the same relative location under
    dst_root, preserving symlinks and overwriting existing files. Returns
    deps/-relative paths of every entry written (full list, not diff).

    Used for source installs that staged via DESTDIR. The structure under
    src_root mirrors the prefix layout already (e.g. include/, lib/), so no
    remapping is needed.
    """
    written: list[str] = []
    if not src_root.is_dir():
        return written
    for p in src_root.rglob("*"):
        if p.is_dir() and not p.is_symlink():
            continue
        rel = p.relative_to(src_root)
        target = dst_root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() or target.is_symlink():
            target.unlink()
        if p.is_symlink():
            os.symlink(os.readlink(p), target)
        else:
            shutil.copy2(p, target)
        written.append(str(rel))
    return sorted(set(written))


def _copy_file(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists() or dst.is_symlink():
        dst.unlink()
    if src.is_symlink():
        os.symlink(os.readlink(src), dst)
    else:
        shutil.copy2(src, dst)


def _patch_pc_files(pc_dir: Path, deps_root: Path, multiarch: str) -> None:
    """
    Rewrite pkg-config prefix paths so consumers find headers/libs in deps/.

    Debian .pc files set prefix=/usr and libdir=${exec_prefix}/lib/<multiarch>.
    Repoint prefix to deps_root and flatten the libdir to ${exec_prefix}/lib.
    """
    if not pc_dir.is_dir():
        return
    for pc in pc_dir.glob("*.pc"):
        text = pc.read_text()
        text = re.sub(r"^prefix=.*$", f"prefix={deps_root}", text, flags=re.MULTILINE)
        text = re.sub(
            rf"^libdir=\$\{{exec_prefix\}}/lib/{re.escape(multiarch)}\s*$",
            "libdir=${exec_prefix}/lib",
            text,
            flags=re.MULTILINE,
        )
        pc.write_text(text)


# ── install_dep ───────────────────────────────────────────────────────────────

@mcp.tool()
async def install_dep(project: str, packages: list[str], arch: str = "native") -> str:
    """
    Install Debian bookworm packages into <project>/deps/ without polluting
    the c-build container or other projects.

    Each package's contents are extracted (not installed system-wide):
    headers go to deps/include/, shared/static libs to deps/lib/, pkg-config
    files to deps/lib/pkgconfig/ (with prefix paths rewritten). Pulled from
    Debian bookworm so glibc ABI matches the c-build container by construction.

    Pass both the dev package and its runtime sibling so build- and run-time
    artefacts are present, e.g.

        install_dep("ChessGen", ["libyaml-dev", "libyaml-0-2"])

    Idempotent per package: a package already in the manifest is skipped.
    The list of files extracted by each package is recorded in the manifest
    so remove_dep can undo cleanly.

    Args:
        project: Folder name under ~/Projects.
        packages: Debian package names (dev + runtime).
        arch: Target arch. 'native' (default) → amd64 in the current
              c-build image. Other arches are reserved — only 'native' /
              'amd64' are accepted today (no cross-toolchain).
    """
    try:
        pd = _project_dir(project)
        deb_arch = _normalize_arch(arch)
        multiarch = _multiarch_for(deb_arch)
    except Exception as e:
        result = f"INSTALL_DEP FAILED ✗\n\n{e}"
        _report("install_dep", {"project": project, "packages": packages, "arch": arch}, result, False)
        return result

    if not packages:
        return "INSTALL_DEP ✓\n\nNo packages requested."

    deps_root = _deps_root(pd)
    deps_root.mkdir(parents=True, exist_ok=True)
    manifest = _read_manifest(pd)
    apt_manifest = manifest.setdefault("apt", {})
    todo = [p for p in packages if p not in apt_manifest]
    skipped = [p for p in packages if p in apt_manifest]

    if not todo:
        msg = "INSTALL_DEP ✓\n\nAll packages already installed: " + ", ".join(skipped)
        _report("install_dep", {"project": project, "packages": packages, "arch": arch}, msg, True)
        return msg

    log_chunks: list[str] = [f"arch={deb_arch} multiarch={multiarch}"]
    if skipped:
        log_chunks.append(f"Already installed (skipped): {', '.join(skipped)}")

    with tempfile.TemporaryDirectory(prefix="mcp-c-apt-") as tmpdir:
        tmp = Path(tmpdir)

        ok, out = await _run_async(["apt-get", "update", "-qq"])
        log_chunks.append(f"-- apt-get update ({'ok' if ok else 'failed'}) --\n{out}")
        if not ok:
            return _install_dep_failed(project, packages, arch, log_chunks)

        # Pin the apt-get download to the chosen arch with `pkg:arch` syntax.
        qualified = [f"{p}:{deb_arch}" for p in todo]
        ok, out = await _run_async(
            ["apt-get", "download", *qualified],
            cwd=str(tmp),
        )
        log_chunks.append(f"-- apt-get download ({'ok' if ok else 'failed'}) --\n{out}")
        if not ok:
            return _install_dep_failed(project, packages, arch, log_chunks)

        debs = sorted(tmp.glob("*.deb"))
        if not debs:
            log_chunks.append("No .debs landed after download — package names invalid?")
            return _install_dep_failed(project, packages, arch, log_chunks)

        # Extract one .deb at a time and snapshot deps/ around each so the file
        # list attributed to each package is precise.
        installed_pkgs: list[str] = []
        for deb in debs:
            ok_v, ver_out = await _run_async(["dpkg-deb", "-f", str(deb), "Package", "Version"])
            pkg_name = ""
            pkg_ver = ""
            if ok_v:
                for line in ver_out.splitlines():
                    if line.startswith("Package:"):
                        pkg_name = line.split(":", 1)[1].strip()
                    elif line.startswith("Version:"):
                        pkg_ver = line.split(":", 1)[1].strip()
            if not pkg_name:
                pkg_name = deb.stem.split("_")[0]

            pkg_staging = tmp / f"staging-{pkg_name}"
            pkg_staging.mkdir()
            ok, out = await _run_async(["dpkg-deb", "-x", str(deb), str(pkg_staging)])
            log_chunks.append(f"-- dpkg-deb -x {deb.name} ({'ok' if ok else 'failed'}) --\n{out}")
            if not ok:
                return _install_dep_failed(project, packages, arch, log_chunks)

            written = _splat_deb_staging(pkg_staging, deps_root, multiarch)
            _patch_pc_files(deps_root / "lib" / "pkgconfig", deps_root, multiarch)

            apt_manifest[pkg_name] = {
                "version": pkg_ver or "installed",
                "arch": deb_arch,
                "files": written,
            }
            installed_pkgs.append(pkg_name)

        # Any requested name we never saw in deb metadata gets a stub entry so
        # remove_dep can at least delete the manifest reference.
        for pkg in todo:
            apt_manifest.setdefault(pkg, {"version": "installed", "arch": deb_arch, "files": []})

    _write_manifest(pd, manifest)

    result = "INSTALL_DEP ✓\n\nInstalled: " + ", ".join(installed_pkgs or todo)
    if skipped:
        result += f"\nAlready present: {', '.join(skipped)}"
    result += "\n\n" + "\n\n".join(log_chunks)
    result = result[:6000]
    _report("install_dep", {"project": project, "packages": packages, "arch": arch}, result, True)
    return result


def _install_dep_failed(project: str, packages: list[str], arch: str, log_chunks: list[str]) -> str:
    body = "INSTALL_DEP FAILED ✗\n\n" + "\n\n".join(log_chunks)
    body = body[:6000]
    _report("install_dep", {"project": project, "packages": packages, "arch": arch}, body, False)
    return body


# ── install_dep_source ────────────────────────────────────────────────────────

_VALID_BUILD_SYSTEMS = ("auto", "autotools", "cmake", "meson")


@mcp.tool()
async def install_dep_source(
    project: str,
    name: str,
    url: str,
    sha256: str = "",
    configure_args: str = "",
    build_system: str = "auto",
    arch: str = "native",
) -> str:
    """
    Build a library from a source tarball and install into <project>/deps/.

    Use this for libraries that aren't in Debian bookworm, or when you need
    HEAD / a custom version. For stock Debian libs prefer install_dep — much
    faster (no compilation).

    Build runs inside the c-build container, so resulting binaries are ABI-
    compatible. The source tree is configured with --prefix=<project>/deps
    so artefacts land in the same layout install_dep uses. Files installed
    are recorded in the manifest so remove_dep can undo cleanly.

    Args:
        project: Folder name under ~/Projects.
        name: Logical identifier for the manifest entry (e.g. "libyaml-head").
        url: Tarball URL (.tar.gz, .tar.xz, .tar.bz2 — tar autodetects).
        sha256: Optional integrity check; if set and mismatched, install fails.
        configure_args: Extra args appended to ./configure / cmake / meson setup.
            Pass as a single string; split on whitespace.
        build_system: 'auto' (default), 'autotools', 'cmake', 'meson'.
            'auto' picks based on which build files are present in the tarball.
            Standalone-Makefile builds aren't supported — every supported
            backend honours DESTDIR, which is what lets us track installed
            files reliably for remove_dep.
        arch: Target arch. 'native' (default) → amd64. Cross-compile arches
              are reserved — only 'native' / 'amd64' work today.
    """
    try:
        pd = _project_dir(project)
        deb_arch = _normalize_arch(arch)
        multiarch = _multiarch_for(deb_arch)
    except Exception as e:
        result = f"INSTALL_DEP_SOURCE FAILED ✗\n\n{e}"
        _report("install_dep_source", {"project": project, "name": name, "url": url, "arch": arch}, result, False)
        return result

    if build_system not in _VALID_BUILD_SYSTEMS:
        return _install_src_failed(
            project, name, url, arch,
            [f"Unknown build_system={build_system!r}; expected one of {_VALID_BUILD_SYSTEMS}"],
        )

    deps_root = _deps_root(pd)
    deps_root.mkdir(parents=True, exist_ok=True)
    manifest = _read_manifest(pd)
    src_manifest = manifest.setdefault("source", {})
    if name in src_manifest:
        msg = (
            f"INSTALL_DEP_SOURCE ✓\n\n'{name}' already installed. "
            f"Use remove_dep('{project}', ['{name}']) to clear it before reinstalling."
        )
        _report("install_dep_source", {"project": project, "name": name, "url": url, "arch": arch}, msg, True)
        return msg

    log_chunks: list[str] = [f"arch={deb_arch}"]

    with tempfile.TemporaryDirectory(prefix="mcp-c-src-") as tmpdir:
        tmp = Path(tmpdir)
        archive = tmp / "archive"

        ok, out = await _run_async(
            ["curl", "-fsSL", "-o", str(archive), url],
        )
        log_chunks.append(f"-- curl ({'ok' if ok else 'failed'}) --\n{out}")
        if not ok:
            return _install_src_failed(project, name, url, arch, log_chunks)

        if sha256:
            actual = hashlib.sha256(archive.read_bytes()).hexdigest()
            if actual != sha256:
                log_chunks.append(f"sha256 mismatch:\n  expected {sha256}\n  got      {actual}")
                return _install_src_failed(project, name, url, arch, log_chunks)
            log_chunks.append(f"sha256 ok ({actual[:16]}…)")

        src_root = tmp / "src"
        src_root.mkdir()
        ok, out = await _run_async(
            ["tar", "-xf", str(archive), "-C", str(src_root)],
        )
        log_chunks.append(f"-- tar -xf ({'ok' if ok else 'failed'}) --\n{out}")
        if not ok:
            return _install_src_failed(project, name, url, arch, log_chunks)

        # Most tarballs root themselves in a single subdir; if not, build in src_root itself.
        entries = [e for e in src_root.iterdir() if e.is_dir()]
        src_dir = entries[0] if len(entries) == 1 else src_root
        log_chunks.append(f"src_dir={src_dir.relative_to(tmp)}")

        bs = build_system
        if bs == "auto":
            if (src_dir / "CMakeLists.txt").exists():
                bs = "cmake"
            elif (src_dir / "meson.build").exists():
                bs = "meson"
            elif (src_dir / "configure").exists():
                bs = "autotools"
            else:
                log_chunks.append(
                    "Couldn't auto-detect a supported build system — no CMakeLists.txt, "
                    "meson.build, or configure in src_dir. Standalone-Makefile builds "
                    "aren't supported (DESTDIR semantics aren't reliable enough to track "
                    "installed files for remove_dep). Pass build_system=… if auto-detect "
                    "is wrong, or repackage with autotools/cmake/meson."
                )
                return _install_src_failed(project, name, url, arch, log_chunks)
        log_chunks.append(f"build_system={bs}")

        extra = configure_args.split() if configure_args else []
        prefix = str(deps_root)
        # All supported backends honour DESTDIR; we install into a staging dir
        # so the manifest can record the FULL set of files this build would
        # contribute (not just files new to deps/), which is what makes
        # remove_dep refcount-aware.
        destdir = tmp / "destdir"

        if bs == "autotools":
            ok, out = await _run_async(
                ["./configure", f"--prefix={prefix}", *extra],
                cwd=str(src_dir),
            )
            log_chunks.append(f"-- ./configure ({'ok' if ok else 'failed'}) --\n{out}")
            if not ok:
                return _install_src_failed(project, name, url, arch, log_chunks)
            ok, out = await _run_async(["make", "-j"], cwd=str(src_dir))
            log_chunks.append(f"-- make ({'ok' if ok else 'failed'}) --\n{out}")
            if not ok:
                return _install_src_failed(project, name, url, arch, log_chunks)
            ok, out = await _run_async(
                ["make", "install"],
                cwd=str(src_dir),
                env={"DESTDIR": str(destdir)},
            )
            log_chunks.append(f"-- make install (DESTDIR) ({'ok' if ok else 'failed'}) --\n{out}")
            if not ok:
                return _install_src_failed(project, name, url, arch, log_chunks)

        elif bs == "cmake":
            build_dir = src_dir / "build"
            ok, out = await _run_async(
                ["cmake", "-S", str(src_dir), "-B", str(build_dir), "-G", "Ninja",
                 f"-DCMAKE_INSTALL_PREFIX={prefix}", *extra],
            )
            log_chunks.append(f"-- cmake configure ({'ok' if ok else 'failed'}) --\n{out}")
            if not ok:
                return _install_src_failed(project, name, url, arch, log_chunks)
            ok, out = await _run_async(["cmake", "--build", str(build_dir)])
            log_chunks.append(f"-- cmake build ({'ok' if ok else 'failed'}) --\n{out}")
            if not ok:
                return _install_src_failed(project, name, url, arch, log_chunks)
            ok, out = await _run_async(
                ["cmake", "--install", str(build_dir)],
                env={"DESTDIR": str(destdir)},
            )
            log_chunks.append(f"-- cmake --install (DESTDIR) ({'ok' if ok else 'failed'}) --\n{out}")
            if not ok:
                return _install_src_failed(project, name, url, arch, log_chunks)

        else:  # meson
            ok, out = await _run_async(
                ["meson", "setup", "build", f"--prefix={prefix}", *extra],
                cwd=str(src_dir),
            )
            log_chunks.append(f"-- meson setup ({'ok' if ok else 'failed'}) --\n{out}")
            if not ok:
                return _install_src_failed(project, name, url, arch, log_chunks)
            ok, out = await _run_async(
                ["meson", "install", "-C", "build", f"--destdir={destdir}"],
                cwd=str(src_dir),
            )
            log_chunks.append(f"-- meson install (--destdir) ({'ok' if ok else 'failed'}) --\n{out}")
            if not ok:
                return _install_src_failed(project, name, url, arch, log_chunks)

        # DESTDIR puts everything under <destdir><prefix>/... — strip the
        # prefix's leading slash and concatenate.
        stage_install_root = destdir / Path(str(deps_root).lstrip("/"))
        new_files = _merge_staging_into(stage_install_root, deps_root)
        log_chunks.append(f"merged {len(new_files)} file(s) from DESTDIR staging")
        if not new_files:
            log_chunks.append(
                "No files landed in the DESTDIR staging dir. The build's install step "
                "may not honour DESTDIR — install_dep_source can't track files for "
                "remove_dep without it. Treating as a failure."
            )
            return _install_src_failed(project, name, url, arch, log_chunks)

    # Source builds may install pkgconfig with a libdir multiarch suffix; patch
    # if present so it stays consistent with install_dep's layout.
    _patch_pc_files(deps_root / "lib" / "pkgconfig", deps_root, multiarch)

    src_manifest[name] = {
        "url": url,
        "sha256": sha256,
        "build_system": bs,
        "configure_args": configure_args,
        "arch": deb_arch,
        "files": new_files,
    }
    _write_manifest(pd, manifest)

    result = f"INSTALL_DEP_SOURCE ✓\n\nInstalled '{name}' into deps/.\n\n" + "\n\n".join(log_chunks)
    result = result[:6000]
    _report("install_dep_source", {"project": project, "name": name, "url": url, "arch": arch}, result, True)
    return result


def _install_src_failed(project: str, name: str, url: str, arch: str, log_chunks: list[str]) -> str:
    body = "INSTALL_DEP_SOURCE FAILED ✗\n\n" + "\n\n".join(log_chunks)
    body = body[:6000]
    _report("install_dep_source", {"project": project, "name": name, "url": url, "arch": arch}, body, False)
    return body


# ── remove_dep ────────────────────────────────────────────────────────────────

def _prune_empty_dirs(deps_root: Path, files: list[str]) -> int:
    """rmdir empty parents of `files`, walking up to but not crossing deps_root."""
    pruned = 0
    seen: set[Path] = set()
    for rel in files:
        parent = (deps_root / rel).parent
        while parent != deps_root and parent not in seen:
            seen.add(parent)
            try:
                parent.rmdir()
                pruned += 1
            except OSError:
                break
            parent = parent.parent
    return pruned


def _entry_files(entry) -> list[str]:
    """Pull a file list out of a manifest entry, tolerating the legacy v1
    format where apt entries were bare version strings."""
    if isinstance(entry, dict):
        return list(entry.get("files", []))
    return []


def _claimed_files(manifest: dict, exclude_apt: set[str], exclude_src: set[str]) -> set[str]:
    """
    Union of every deps/-relative file claimed by manifest entries OTHER than
    the named ones. Used by remove_dep to decide which files are still owned.
    """
    claimed: set[str] = set()
    for name, entry in manifest.get("apt", {}).items():
        if name in exclude_apt:
            continue
        claimed.update(_entry_files(entry))
    for name, entry in manifest.get("source", {}).items():
        if name in exclude_src:
            continue
        claimed.update(_entry_files(entry))
    return claimed


@mcp.tool()
async def remove_dep(project: str, packages: list[str], arch: str = "native") -> str:
    """
    Remove apt packages and/or source builds previously added to <project>/deps/.

    Each name is matched first against the apt manifest, then the source
    manifest. Files recorded for that entry are deleted ONLY if no other
    manifest entry still claims them — so a shared file shipped by two
    packages survives until the last claimant is removed. Empty parent
    directories under deps/ are pruned. The manifest entry is cleared
    regardless of whether its files were physically deleted. No-op for
    names that aren't currently installed.

    The `arch` parameter is informational right now — only one arch ('native'
    → amd64) is supported per project — but is validated so the API stays
    consistent with install_dep / install_dep_source.

    Args:
        project: Folder name under ~/Projects.
        packages: Manifest entry names (apt package names or install_dep_source
                  `name` identifiers).
        arch: Reserved; defaults to 'native'.
    """
    try:
        pd = _project_dir(project)
        _normalize_arch(arch)  # validate only
    except Exception as e:
        result = f"REMOVE_DEP FAILED ✗\n\n{e}"
        _report("remove_dep", {"project": project, "packages": packages, "arch": arch}, result, False)
        return result

    deps_root = _deps_root(pd)
    if not deps_root.is_dir():
        msg = f"REMOVE_DEP ✓\n\nNo deps/ directory for {project}; nothing to remove."
        _report("remove_dep", {"project": project, "packages": packages, "arch": arch}, msg, True)
        return msg

    manifest = _read_manifest(pd)
    apt = manifest.get("apt", {})
    src = manifest.get("source", {})

    # Resolve each requested name → which manifest section it lives in.
    # Tracked separately so _claimed_files can exclude all of them as a set
    # (handles the case where two requested names share files).
    target_apt: list[str] = []
    target_src: list[str] = []
    missing: list[str] = []
    no_file_record: list[str] = []
    target_files: dict[str, list[str]] = {}

    for name in packages:
        if name in apt:
            target_apt.append(name)
            target_files[name] = _entry_files(apt[name])
            if not isinstance(apt[name], dict) or "files" not in apt[name]:
                no_file_record.append(name)
        elif name in src:
            target_src.append(name)
            target_files[name] = _entry_files(src[name])
        else:
            missing.append(name)

    # Files still claimed by *other* entries (everyone NOT being removed).
    still_claimed = _claimed_files(manifest, set(target_apt), set(target_src))

    # Build the union of files we'd consider deleting, then filter by ownership.
    union_to_consider: set[str] = set()
    for files in target_files.values():
        union_to_consider.update(files)
    safe_to_delete = sorted(union_to_consider - still_claimed)
    kept_due_to_sharing = sorted(union_to_consider & still_claimed)

    deleted_count = 0
    delete_errors: list[str] = []
    for rel in safe_to_delete:
        p = deps_root / rel
        try:
            if p.is_symlink() or p.is_file():
                p.unlink()
                deleted_count += 1
        except FileNotFoundError:
            pass
        except OSError as e:
            delete_errors.append(f"{rel}: {e}")

    # Clear manifest entries (whether or not their files were deleted).
    for name in target_apt:
        del apt[name]
    for name in target_src:
        del src[name]

    pruned = _prune_empty_dirs(deps_root, safe_to_delete)
    _write_manifest(pd, manifest)

    parts = [
        f"apt removed:    {', '.join(target_apt) if target_apt else '(none)'}",
        f"source removed: {', '.join(target_src) if target_src else '(none)'}",
        f"files deleted:  {deleted_count}",
        f"files kept (still claimed by other entries): {len(kept_due_to_sharing)}",
        f"empty dirs pruned: {pruned}",
    ]
    if kept_due_to_sharing:
        sample = kept_due_to_sharing[:5]
        more = "" if len(kept_due_to_sharing) <= 5 else f" (+{len(kept_due_to_sharing) - 5} more)"
        parts.append("  shared (kept): " + ", ".join(sample) + more)
    if missing:
        parts.append(f"not in manifest: {', '.join(missing)}")
    if no_file_record:
        parts.append(
            f"no recorded file list (legacy entry?): {', '.join(no_file_record)} — "
            "manifest entry cleared but files remain"
        )
    if delete_errors:
        parts.append("delete errors:\n  " + "\n  ".join(delete_errors[:20]))

    ok = not missing and not delete_errors
    header = "REMOVE_DEP ✓" if ok else "REMOVE_DEP (with warnings)"
    result = f"{header}\n\n" + "\n".join(parts)
    _report("remove_dep", {"project": project, "packages": packages, "arch": arch}, result, ok)
    return result


# ── list_deps ─────────────────────────────────────────────────────────────────

@mcp.tool()
async def list_deps(project: str) -> str:
    """
    Show what's currently installed in <project>/deps/.

    Reads deps/.installed.json and renders the apt + source entries with
    version, arch, and file count. Returns a brief note if no deps/
    directory or manifest exists.
    """
    try:
        pd = _project_dir(project)
    except Exception as e:
        return f"LIST_DEPS FAILED ✗\n\n{e}"

    deps_root = _deps_root(pd)
    if not deps_root.is_dir():
        return f"LIST_DEPS ✓\n\nNo deps/ directory in {project}."

    manifest = _read_manifest(pd)
    if not manifest:
        return f"LIST_DEPS ✓\n\ndeps/ exists but no manifest yet (try install_dep / install_dep_source)."

    lines = [f"deps/ for project '{project}':"]
    apt = manifest.get("apt", {})
    if apt:
        lines.append("\napt:")
        for pkg in sorted(apt):
            entry = apt[pkg]
            if isinstance(entry, dict):
                ver = entry.get("version", "?")
                arch = entry.get("arch", "?")
                nfiles = len(entry.get("files", []))
                lines.append(f"  {pkg} = {ver}  [arch={arch}, {nfiles} file(s)]")
            else:
                # Legacy v1 entry (bare version string).
                lines.append(f"  {pkg} = {entry}  [legacy entry, no file list]")
    src = manifest.get("source", {})
    if src:
        lines.append("\nsource:")
        for n in sorted(src):
            entry = src[n]
            bs = entry.get("build_system", "?")
            url = entry.get("url", "?")
            arch = entry.get("arch", "?")
            nfiles = len(entry.get("files", []))
            lines.append(f"  {n} ({bs}) ← {url}  [arch={arch}, {nfiles} file(s)]")
    if not apt and not src:
        lines.append("\n(manifest is empty)")
    return "LIST_DEPS ✓\n\n" + "\n".join(lines)


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
