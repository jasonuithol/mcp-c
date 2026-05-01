"""Ingest router for c-knowledge.

Differences from the pygame router:

- Tests come from multiple runners (ctest / meson / make / direct), so
  the build service normalises them into a `tests: [{node_id, passed}]`
  list before reporting. Failure detail comes from the stdout tail.
- analyze() emits a `findings: [{node_id, passed}]` list with the full
  valgrind / ASan output in stdout. We index the whole report on
  failure rather than parsing per-error.
- build() failures are coarse — one chunk per failed invocation.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from mcp_knowledge_base import IngestRouter

from .chunker import (
    chunk_build_error,
    chunk_lint_error,
    chunk_sanitizer_report,
    chunk_test_failure,
    chunk_test_fix,
    upsert_chunks,
)

if TYPE_CHECKING:
    import chromadb

logger = logging.getLogger("mcp-knowledge.router")

BUFFER_PATH = Path("/opt/knowledge/test_failure_buffer.json")
MAX_BUFFER_ENTRIES = 500


class CIngestRouter(IngestRouter):
    """Routes incoming tool payloads to chunking/indexing logic."""

    def __init__(self, collection: "chromadb.Collection"):
        self.collection = collection
        self._pending_failures: dict[str, dict] = self._load_buffer()

    # ── buffer persistence ────────────────────────────────────────────────

    def _load_buffer(self) -> dict[str, dict]:
        try:
            if BUFFER_PATH.exists():
                data = json.loads(BUFFER_PATH.read_text())
                if isinstance(data, dict):
                    return data
        except Exception:
            logger.warning("Failed to load test failure buffer, starting fresh")
        return {}

    def _save_buffer(self) -> None:
        try:
            BUFFER_PATH.parent.mkdir(parents=True, exist_ok=True)
            if len(self._pending_failures) > MAX_BUFFER_ENTRIES:
                items = sorted(
                    self._pending_failures.items(),
                    key=lambda kv: kv[1].get("timestamp", ""),
                    reverse=True,
                )[:MAX_BUFFER_ENTRIES]
                self._pending_failures = dict(items)
            BUFFER_PATH.write_text(json.dumps(self._pending_failures))
        except Exception:
            logger.warning("Failed to persist test failure buffer")

    # ── indexing helper ──────────────────────────────────────────────────

    def _index_chunks(self, chunks: list[dict]) -> None:
        if not chunks:
            return
        upsert_chunks(self.collection, chunks)
        logger.info("Indexed %d chunks", len(chunks))

    # ── route ────────────────────────────────────────────────────────────

    def route(self, payload: dict) -> dict:
        tool = payload.get("tool", "")
        success = payload.get("success", True)
        result = payload.get("result", "")
        args = payload.get("args", {})
        timestamp = payload.get("timestamp", "")

        if tool == "build":
            if success:
                return {"action": "skipped_build_success", "chunks": 0}
            project = args.get("project", "unknown")
            backend = args.get("backend", "")
            self._index_chunks([chunk_build_error(result, project, backend)])
            return {"action": "indexed_build_error", "chunks": 1}

        if tool == "run_tests":
            return self._handle_run_tests(result, args, timestamp)

        if tool == "lint":
            if success:
                return {"action": "skipped_lint_clean", "chunks": 0}
            project = args.get("project", "unknown")
            self._index_chunks([chunk_lint_error(result, project)])
            return {"action": "indexed_lint_error", "chunks": 1}

        if tool == "analyze":
            return self._handle_analyze(result, args, success)

        logger.debug("Unhandled tool: %s", tool)
        return {"action": "skipped_unknown", "chunks": 0}

    # ── run_tests ────────────────────────────────────────────────────────

    def _handle_run_tests(self, result: str, args: dict, timestamp: str) -> dict:
        project = args.get("project", "unknown")
        try:
            envelope = json.loads(result) if isinstance(result, str) else result
        except Exception:
            return {"action": "skipped_unparseable", "chunks": 0}

        tests = envelope.get("tests", []) if isinstance(envelope, dict) else []
        stdout = envelope.get("stdout", "") if isinstance(envelope, dict) else ""

        if not isinstance(tests, list):
            return {"action": "skipped_no_tests", "chunks": 0}

        new_chunks: list[dict] = []
        fix_count = 0

        for t in tests:
            node_id = t.get("node_id", "")
            if not node_id:
                continue
            passed = bool(t.get("passed"))
            if not passed:
                # Build a per-node longrepr from the run's stdout tail —
                # we don't have a structured per-test failure message
                # from arbitrary C runners, so the run log is the best we
                # have. It at least keeps the failing test discoverable.
                longrepr = stdout[-4000:] if stdout else "(no output)"
                new_chunks.append(chunk_test_failure(node_id, longrepr, project))
                self._pending_failures[node_id] = {
                    "project": project,
                    "longrepr": longrepr,
                    "timestamp": timestamp,
                }
            else:
                pending = self._pending_failures.pop(node_id, None)
                if pending:
                    new_chunks.append(chunk_test_fix(
                        node_id=node_id,
                        failure_longrepr=pending.get("longrepr", ""),
                        project=pending.get("project", project),
                    ))
                    fix_count += 1

        self._save_buffer()
        self._index_chunks(new_chunks)

        failures = [t for t in tests if not t.get("passed")]
        action = (
            "indexed_test_failures_and_fixes" if failures and fix_count
            else "indexed_test_failures" if failures
            else "indexed_test_fixes" if fix_count
            else "skipped_routine_success"
        )
        return {"action": action, "chunks": len(new_chunks)}

    # ── analyze ──────────────────────────────────────────────────────────

    def _handle_analyze(self, result: str, args: dict, success: bool) -> dict:
        project = args.get("project", "unknown")
        tool = args.get("tool", "valgrind")

        if success:
            return {"action": "skipped_analyze_clean", "chunks": 0}

        try:
            envelope = json.loads(result) if isinstance(result, str) else result
        except Exception:
            envelope = {}

        if not isinstance(envelope, dict):
            envelope = {}
        stdout = envelope.get("stdout", "") if isinstance(envelope, dict) else ""
        findings = envelope.get("findings", []) if isinstance(envelope, dict) else []

        # One chunk per failing binary so retrieval can land on the
        # specific reproduction.
        new_chunks: list[dict] = []
        if isinstance(findings, list) and findings:
            for f in findings:
                if f.get("passed"):
                    continue
                binary = f.get("node_id", "")
                # Pull the section of stdout for this binary if recognisable;
                # otherwise fall back to the full tail.
                section = _section_for_binary(stdout, binary) or stdout
                new_chunks.append(
                    chunk_sanitizer_report(section, project, tool, binary)
                )
        else:
            # No findings list — index the whole output as one chunk.
            new_chunks.append(chunk_sanitizer_report(stdout, project, tool))

        self._index_chunks(new_chunks)
        return {"action": "indexed_sanitizer_report", "chunks": len(new_chunks)}


def _section_for_binary(stdout: str, binary: str) -> str:
    """Best-effort: extract the section between '-- <binary> ...' headers."""
    if not binary or not stdout:
        return ""
    # The build service emits headers like "-- path/to/binary (clean) --"
    marker = f" {binary} "
    idx = stdout.find(marker)
    if idx < 0:
        return ""
    # Find the next "-- " section header after this one
    next_idx = stdout.find("\n-- ", idx + len(marker))
    if next_idx < 0:
        return stdout[idx:]
    return stdout[idx:next_idx]
