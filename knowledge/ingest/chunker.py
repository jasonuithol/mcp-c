"""Chunking logic for C knowledge sources.

Cross-domain primitives (`tag_key`, `tag_flags`, `upsert_chunks`,
`sanitize_for_id`, `now_iso`) live in `mcp_knowledge_base.chunks` and
are re-exported here for the convenience of call sites in `router.py` /
`mcp-service.py`.
"""

from __future__ import annotations

import re

from mcp_knowledge_base import (
    now_iso,
    sanitize_for_id,
    tag_flags,
    tag_key,
    upsert_chunks,
)

from .extractors import (
    detect_tags,
    extract_module_name,
    extract_top_level_nodes,
)

__all__ = [
    "chunk_c_source",
    "chunk_docs",
    "chunk_test_failure",
    "chunk_test_fix",
    "chunk_lint_error",
    "chunk_build_error",
    "chunk_sanitizer_report",
    "tag_key",
    "tag_flags",
    "upsert_chunks",
]


# ── C source ─────────────────────────────────────────────────────────────────

def chunk_c_source(
    source: str,
    file_path: str,
    project: str,
    project_root: str,
    extra_tags: list[str] | None = None,
) -> list[dict]:
    """Chunk a C source/header file by top-level function / struct / enum / typedef.

    Files with no extracted nodes (small headers, generated files, etc.)
    become a single whole-file chunk.
    """
    extra_tags = extra_tags or []
    module = extract_module_name(file_path, project_root)
    nodes = extract_top_level_nodes(source)
    now = now_iso()
    chunks: list[dict] = []

    if not nodes:
        tags = [*extra_tags, project.lower(), *detect_tags(source)]
        chunks.append({
            "id": f"c-source/{project}/{sanitize_for_id(module)}",
            "document": source,
            "metadata": {
                "source": f"c-source/{project}/{module}",
                "type": "module",
                "module": module,
                "class_name": "",
                "func_name": "",
                "tags": ",".join(tags),
                "indexed_at": now,
                "project": project,
                **tag_flags(tags),
            },
        })
        return chunks

    seen_ids: set[str] = set()
    for node in nodes:
        body = node["body"]
        tags = [*extra_tags, project.lower(), *detect_tags(body)]
        kind = node["kind"]
        # We re-use the pygame schema's class_name/func_name slots for
        # struct/enum and function names so existing query tooling works.
        class_name = node["name"] if kind in ("struct", "enum", "typedef") else ""
        func_name  = node["name"] if kind == "function" else ""
        base_id = (
            f"c-source/{project}/{sanitize_for_id(module)}/"
            f"{kind}/{sanitize_for_id(node['name'])}"
        )
        # Disambiguate collisions (e.g. #ifdef-guarded twin definitions
        # of the same function) by appending the start line.
        chunk_id = base_id
        if chunk_id in seen_ids:
            chunk_id = f"{base_id}@L{node.get('start_line', 0)}"
        seen_ids.add(chunk_id)
        chunks.append({
            "id": chunk_id,
            "document": body,
            "metadata": {
                "source": f"c-source/{project}/{module}",
                "type": kind,
                "module": module,
                "class_name": class_name,
                "func_name": func_name,
                "tags": ",".join(tags),
                "indexed_at": now,
                "project": project,
                **tag_flags(tags),
            },
        })

    return chunks


# ── Docs ─────────────────────────────────────────────────────────────────────

def chunk_docs(text: str, filename: str) -> list[dict]:
    """Chunk a markdown doc by ## headers."""
    sections = re.split(r"(?=^## )", text, flags=re.MULTILINE)
    now = now_iso()
    chunks: list[dict] = []

    for i, section in enumerate(sections):
        section = section.strip()
        if not section:
            continue

        title_match = re.match(r"^##\s+(.+)", section)
        title = title_match.group(1).strip() if title_match else f"section_{i}"
        safe_title = re.sub(r"[^a-zA-Z0-9_-]", "_", title)[:80]

        tags = detect_tags(section)
        file_tag = filename.replace(".md", "").lower()
        if file_tag and file_tag not in tags:
            tags.insert(0, file_tag)

        chunks.append({
            "id": f"docs/{filename}/{safe_title}",
            "document": section,
            "metadata": {
                "source": f"docs/{filename}",
                "type": "section",
                "module": "",
                "class_name": "",
                "func_name": "",
                "tags": ",".join(tags),
                "indexed_at": now,
                "project": "",
                **tag_flags(tags),
            },
        })

    return chunks


# ── Test failures and fixes ──────────────────────────────────────────────────

def chunk_test_failure(node_id: str, longrepr: str, project: str) -> dict:
    """One chunk for a single failing test."""
    document = f"NODE: {node_id}\n\nFAILURE:\n{longrepr}\n"
    tags = ["test-failure", project.lower()] + detect_tags(document)
    now = now_iso()
    sanitized = sanitize_for_id(node_id)
    return {
        "id": f"test-failure/{project}/{sanitized}/{now}",
        "document": document,
        "metadata": {
            "source": f"test-failure/{project}/{node_id}",
            "type": "error",
            "module": "",
            "class_name": "",
            "func_name": "",
            "tags": ",".join(tags),
            "indexed_at": now,
            "project": project,
            "node_id": node_id,
            **tag_flags(tags),
        },
    }


def chunk_test_fix(node_id: str, failure_longrepr: str, project: str) -> dict:
    """One chunk for a fail→pass transition."""
    document = (
        f"NODE: {node_id}\n\n"
        f"FAILED WITH:\n{failure_longrepr}\n\n"
        f"NOW PASSING.\n"
    )
    tags = ["test-fix", project.lower()] + detect_tags(document)
    now = now_iso()
    sanitized = sanitize_for_id(node_id)
    return {
        "id": f"test-fix/{project}/{sanitized}/{now}",
        "document": document,
        "metadata": {
            "source": f"test-fix/{project}/{node_id}",
            "type": "pattern",
            "module": "",
            "class_name": "",
            "func_name": "",
            "tags": ",".join(tags),
            "indexed_at": now,
            "project": project,
            "node_id": node_id,
            **tag_flags(tags),
        },
    }


# ── Lint errors ──────────────────────────────────────────────────────────────

def chunk_lint_error(output: str, project: str) -> dict:
    """One chunk for a clang-tidy / cppcheck failure."""
    tags = ["lint-error", project.lower()] + detect_tags(output)
    now = now_iso()
    return {
        "id": f"lint-error/{project}/{now}",
        "document": output,
        "metadata": {
            "source": f"lint-error/{project}",
            "type": "error",
            "module": "",
            "class_name": "",
            "func_name": "",
            "tags": ",".join(tags),
            "indexed_at": now,
            "project": project,
            **tag_flags(tags),
        },
    }


# ── Build errors ─────────────────────────────────────────────────────────────

def chunk_build_error(output: str, project: str, backend: str = "") -> dict:
    """One chunk for a build failure."""
    tags = ["build-error", project.lower()]
    if backend:
        tags.append(f"backend-{backend}")
    tags += detect_tags(output)
    now = now_iso()
    return {
        "id": f"build-error/{project}/{now}",
        "document": output,
        "metadata": {
            "source": f"build-error/{project}",
            "type": "error",
            "module": "",
            "class_name": "",
            "func_name": "",
            "tags": ",".join(tags),
            "indexed_at": now,
            "project": project,
            **tag_flags(tags),
        },
    }


# ── Sanitizer / valgrind reports ─────────────────────────────────────────────

def chunk_sanitizer_report(
    output: str,
    project: str,
    tool: str,
    binary: str = "",
) -> dict:
    """One chunk for a valgrind or ASan finding."""
    tags = ["sanitizer-report", f"sanitizer-{tool}", project.lower()] + detect_tags(output)
    now = now_iso()
    bin_part = sanitize_for_id(binary) if binary else "all"
    return {
        "id": f"sanitizer-report/{project}/{tool}/{bin_part}/{now}",
        "document": output,
        "metadata": {
            "source": f"sanitizer-report/{project}/{tool}",
            "type": "error",
            "module": "",
            "class_name": "",
            "func_name": binary,
            "tags": ",".join(tags),
            "indexed_at": now,
            "project": project,
            "tool": tool,
            **tag_flags(tags),
        },
    }
