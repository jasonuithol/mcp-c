"""mcp-knowledge: RAG-backed C knowledge service.

Built on `mcp-knowledge-base`, which provides the FastMCP + ChromaDB +
/ingest scaffolding. This module adds only the C-specific pieces:
the chunker, the tag taxonomy, the bespoke MCP tools, and a stats()
that also breaks down by project.

Collection is *domain-scoped*, not project-scoped: all C projects share
`c_knowledge` so cross-project patterns surface during retrieval. The
`project` metadata field identifies origin when that matters.
"""

from __future__ import annotations

import os
from pathlib import Path

from mcp_knowledge_base import KnowledgeService, ServiceConfig

from ingest.chunker import (
    chunk_c_source,
    chunk_docs,
    upsert_chunks,
)
from ingest.extractors import PATTERN_TAGS, detect_tags
from ingest.router import CIngestRouter

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

PROJECTS_DIR = os.environ.get("PROJECTS_DIR", "/opt/projects")

# ---------------------------------------------------------------------------
# Service assembly
# ---------------------------------------------------------------------------

svc = KnowledgeService(ServiceConfig.from_env(
    name="c-knowledge",
    collection_name="c_knowledge",
    port=5194,
    header_keys=["project", "module", "class_name", "func_name"],
))

svc.register_default_tools(exclude={"stats"})
svc.register_retag_all(PATTERN_TAGS, detect_tags)
svc.set_ingest_router(CIngestRouter(svc.collection))

collection = svc.collection
mcp = svc.mcp


# ---- Domain-specific query tools -----------------------------------------


@svc.tool()
def ask_module(module: str) -> str:
    """Find knowledge about a specific module path (e.g. 'src.net.listener')."""
    results = collection.query(
        query_texts=[module],
        n_results=10,
        where={"module": module},
    )
    return svc.format_query(results)


@svc.tool()
def ask_project(question: str, project: str) -> str:
    """Scope a semantic search to one project's chunks."""
    results = collection.query(
        query_texts=[question],
        n_results=5,
        where={"project": project},
    )
    return svc.format_query(results)


# ---- stats() override (adds Projects breakdown) --------------------------


@svc.tool()
def stats() -> str:
    """Collection size, project breakdown, source/type/tag distributions."""
    count = collection.count()
    if count == 0:
        return "Knowledge base is empty."

    all_meta = collection.get(include=["metadatas"])

    sources: dict[str, int] = {}
    tags_count: dict[str, int] = {}
    types: dict[str, int] = {}
    projects: dict[str, int] = {}

    for meta in all_meta["metadatas"]:
        src = meta.get("source", "unknown")
        sources[src] = sources.get(src, 0) + 1

        chunk_type = meta.get("type", "unknown")
        types[chunk_type] = types.get(chunk_type, 0) + 1

        proj = meta.get("project", "") or "(none)"
        projects[proj] = projects.get(proj, 0) + 1

        for tag in meta.get("tags", "").split(","):
            tag = tag.strip()
            if tag:
                tags_count[tag] = tags_count.get(tag, 0) + 1

    lines = [f"Total chunks: {count}", ""]

    lines.append(f"Projects ({len(projects)}):")
    for p, c in sorted(projects.items(), key=lambda x: -x[1]):
        lines.append(f"  {p}: {c}")

    lines.append(f"\nTop sources ({len(sources)}):")
    for src, c in sorted(sources.items(), key=lambda x: -x[1])[:20]:
        lines.append(f"  {src}: {c}")

    lines.append("\nTypes:")
    for t, c in sorted(types.items(), key=lambda x: -x[1]):
        lines.append(f"  {t}: {c}")

    lines.append("\nTop tags:")
    for tag, c in sorted(tags_count.items(), key=lambda x: -x[1])[:20]:
        lines.append(f"  {tag}: {c}")

    return "\n".join(lines)


# ---- Domain-specific seeding tools ---------------------------------------


@svc.tool()
def seed_docs(docs_path: str) -> str:
    """Index every *.md under a directory as curated docs.

    Chunks by `## ` header. Adds a tag derived from the filename.
    """
    docs_dir = Path(docs_path)
    if not docs_dir.is_dir():
        return f"Directory not found: {docs_path}"

    total_chunks = 0
    files_indexed: list[str] = []

    for md_file in sorted(docs_dir.rglob("*.md")):
        name = md_file.name
        text = md_file.read_text(encoding="utf-8", errors="replace")
        chunks = chunk_docs(text, name)
        if chunks:
            upsert_chunks(collection, chunks)
            total_chunks += len(chunks)
            files_indexed.append(f"  {md_file.relative_to(docs_dir)}: {len(chunks)} chunks")

    if not files_indexed:
        return f"No .md files found under {docs_path}"

    return (
        f"Indexed {total_chunks} chunks from {len(files_indexed)} files:\n"
        + "\n".join(files_indexed)
    )


@svc.tool()
def seed_c_source(
    project: str,
    source_dir: str,
    extra_tags: list[str] = None,
) -> str:
    """Index a C source tree into the knowledge base.

    Walks *.c and *.h under source_dir (skipping build/, build-make/,
    build-asan/, .git, and hidden dirs). One chunk per top-level
    function / struct / enum / typedef, plus a whole-file chunk for
    files that have no extracted nodes.

    Args:
        project: Project name (used in metadata.project and as a tag).
        source_dir: Directory to walk. Absolute paths used as-is; relative
                    paths resolve under PROJECTS_DIR.
        extra_tags: Additional tags prepended to every chunk. Defaults to
                    ['successful-example']. Pass e.g. ['library','glibc']
                    when indexing third-party reference source.
    """
    if extra_tags is None:
        extra_tags = ["successful-example"]

    src_dir = Path(source_dir)
    if not src_dir.is_absolute():
        src_dir = Path(PROJECTS_DIR) / source_dir
    if not src_dir.is_dir():
        return f"Directory not found: {src_dir}"

    EXCLUDE_PARTS = {
        "build", "build-make", "build-asan",
        ".git", "node_modules", "vendor",
    }

    def excluded(p: Path) -> bool:
        for part in p.parts:
            if part in EXCLUDE_PARTS:
                return True
            if part.startswith(".") and part not in (".",):
                return True
        return False

    src_files = [
        p for p in (*src_dir.rglob("*.c"), *src_dir.rglob("*.h"))
        if not excluded(p.relative_to(src_dir))
    ]
    if not src_files:
        return f"No .c or .h files found under {src_dir}"

    total_chunks = 0
    files_done = 0
    errors: list[str] = []

    BATCH = 500
    pending: list[dict] = []

    for src in src_files:
        try:
            text = src.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            errors.append(f"  read failed {src}: {e}")
            continue

        chunks = chunk_c_source(
            source=text,
            file_path=str(src),
            project=project,
            project_root=str(src_dir),
            extra_tags=extra_tags,
        )
        pending.extend(chunks)
        total_chunks += len(chunks)
        files_done += 1

        if len(pending) >= BATCH:
            upsert_chunks(collection, pending)
            pending = []

    if pending:
        upsert_chunks(collection, pending)

    summary = (
        f"Indexed {total_chunks} chunks from {files_done} .c/.h files "
        f"in project '{project}' (tags: {extra_tags})"
    )
    if errors:
        summary += "\n\nErrors:\n" + "\n".join(errors[:10])
        if len(errors) > 10:
            summary += f"\n  ... and {len(errors) - 10} more"
    return summary


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    svc.run()
