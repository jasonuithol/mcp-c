# mcp-knowledge — C Knowledge Service

A RAG-backed MCP service that accumulates knowledge from indexing
project source, curated docs, and the build/test/lint/analyze signals
fired by the sibling `service/` (`c-mcp-build`).

The pygame and Valheim variants of this service are siblings; all three
share the `mcp-knowledge-base` scaffolding but stay isolated — only one
sandbox runs at a time per host.

---

## Design principle: domain-scoped collection

All C projects share a single ChromaDB collection: `c_knowledge`.
Retrieval crosses project boundaries deliberately — a memory bug fixed
in project A is discoverable while working in project B. Each chunk
carries a `project` metadata field, so scoped queries are still
possible via `ask_project(question, project)`.

---

## Passive ingest, active query

Tool executions in `c-mcp-build` fire fire-and-forget POSTs to
`/ingest`. The router (`ingest/router.py`) decides what to index. See
`docs/INGEST_MCP.md` for the payload shape and routing table.

Signals we care about:

- **Build failures** (from `build`) — one chunk per failed invocation.
  Tagged with `backend-cmake` / `backend-meson` / `backend-make` /
  `backend-direct` so retrieval can scope by build system.
- **Test failures** (from `run_tests`) — indexed per node id. The
  `node_id` for a ctest run is the test name; for meson it's the test
  name from `meson-logs/testlog.json`; for make it's a single
  synthetic `make-test` node; for the direct path it's the binary
  name.
- **Test fixes** — when a previously-failing node id now passes, a
  `test-fix` chunk pairs the old failure with the resolution
  timestamp. Buffer persists across container restarts at
  `/opt/knowledge/test_failure_buffer.json`.
- **Lint errors** (from `lint`) — indexed on failure; skipped on
  success. clang-tidy + cppcheck output combined.
- **Sanitizer reports** (from `analyze`) — indexed on failure, one
  chunk per failing binary. Tagged with `sanitizer-valgrind` or
  `sanitizer-asan`.

Successful builds, clean lints, and clean analyses are skipped.

---

## MCP tools

### Query

| Tool | Purpose |
|------|---------|
| `ask(question)` | Semantic search across the whole collection |
| `ask_module(module)` | Filter by module path (e.g. `src.net.listener`) |
| `ask_tagged(question, tags)` | Filter by one or more tags — most relevant within that subset |
| `ask_project(question, project)` | Scope to one project |

### Maintenance

| Tool | Purpose |
|------|---------|
| `list_sources()` | Every indexed source with chunk count |
| `stats()` | Totals by project, source, type, tag |
| `forget(source)` | Delete all chunks matching a source (supports prefix, e.g. `c-source/MyProj`) |
| `seed_docs(docs_path)` | Index every `.md` under a directory by `##` section |
| `seed_c_source(project, source_dir, extra_tags=[...])` | Index a C source tree |
| `retag_all()` | Re-run tag auto-detection across every chunk |

---

## Chunking strategy

C has no Python-grade ast in the stdlib, so we use regex to extract
top-level definitions. It's heuristic but good-enough for retrieval —
the embedding model forgives noise.

| Source | Boundary | Typical size |
|--------|----------|--------------|
| .c/.h file | One chunk per top-level function / struct / enum / typedef | 5-200 lines |
| .c/.h file (no extractable nodes) | One whole-file chunk | variable |
| Markdown doc | One chunk per `## ` section | 10-100 lines |
| Test failure | One chunk per failing node id per run | small |
| Test fix | One chunk per fail→pass transition | small |
| Build error | One chunk per failed build invocation | medium |
| Lint error | One chunk per failed lint invocation | medium |
| Sanitizer report | One chunk per failing binary per run | medium |

The regex extractor (`ingest/extractors.py`) walks brace pairs naïvely,
which means an unbalanced `{` inside a string literal could skew chunk
boundaries. In practice this is rare in C source; if it happens the
worst case is a chunk that spans more than its true definition, which
the embedding query is robust to.

---

## Metadata schema

```python
{
    "source":      "c-source/MyProj/src.net.listener",
    "type":        "function",          # function | struct | enum | typedef | module | section | error | pattern
    "module":      "src.net.listener",
    "class_name":  "",                  # struct/enum/typedef name lives here
    "func_name":   "accept_loop",       # or "" for non-functions
    "tags":        "memory,socket,errno,MyProj,successful-example",
    "indexed_at":  "2026-04-30T08:00:00Z",
    "project":     "MyProj",
    # Plus per-tag boolean keys for filtering:
    "tag_memory":              True,
    "tag_socket":              True,
    "tag_errno":               True,
    "tag_myproj":              True,
    "tag_successful_example":  True,
}
```

Tag keys are normalised via `tag_key()` (lowercase, non-alphanumeric →
underscore). ChromaDB's metadata filter has no `$contains` operator,
so the boolean keys are how `ask_tagged` works.

---

## Container layout

```
mcp-c/knowledge/
├── CLAUDE.md              ← this file
├── Dockerfile             ← CUDA base for GPU-accelerated embeddings
├── requirements.txt       ← fastmcp, chromadb, mcp-knowledge-base, onnxruntime-gpu
├── build-container.sh     ← builds image
├── start-container.sh     ← runs with --device nvidia.com/gpu=all
├── setup-gpu.sh           ← one-shot NVIDIA Container Toolkit install
├── reset-knowledge.sh     ← wipe ChromaDB and restart
├── seed.sh                ← seed docs (and optionally project sources)
├── mcp-service.py         ← FastMCP server + /ingest HTTP endpoint
├── ingest/
│   ├── chunker.py         ← C source chunking + tag flag scaffolding
│   ├── extractors.py      ← C PATTERN_TAGS, regex node walker
│   └── router.py          ← build / run_tests / lint / analyze routing
└── knowledge/             ← ChromaDB persistent storage (gitignored)
```

---

## Seeding workflow

First-time setup after `start-container.sh`:

```bash
./seed.sh
```

Out of the box this only seeds the mcp-c docs. Extend `seed.sh` once
you have a C project to index — there's a commented example template
at the bottom of the file.

After seeding, the knowledge base grows automatically from `build` /
`run_tests` / `lint` / `analyze` invocations.

---

## Known concerns

### 1. Regex chunking is approximate

K&R-style function definitions, definitions with the brace on the next
line, and source containing `{` inside string literals can throw off
chunk boundaries. Output is still indexable; the chunk just may
include a bit more or less than its true definition. For accurate
parsing, libclang would be the right tool — out of scope for v1.

### 2. Per-test failure detail is coarse for non-cmake/meson runners

ctest and meson give us per-test outcomes; make and direct give us
overall pass/fail (or per-binary for direct). When a `make test` run
fails, the chunk records the run output, not which subtest within
make failed. Bias your projects toward ctest or meson if granular
retrieval matters.

### 3. Buffer grows without explicit eviction

`test_failure_buffer.json` is capped at 500 entries by
most-recently-touched. Heavy churn on many unique node ids could push
older still-failing tests out before they get fixed. Bump
`MAX_BUFFER_ENTRIES` in `router.py` if this matters.

### 4. No deduplication on re-seed

`seed_c_source` uses deterministic IDs
(`c-source/{project}/{module}/{kind}/{name}`), so re-seeding upserts
rather than duplicating. But if a function is renamed or deleted, the
old chunk sits in the index until explicitly forgotten. Run
`forget("c-source/<project>")` before re-seeding a refactored project.

---

## Non-goals

- Not a replacement for the curated `docs/*.md` reference.
- Not a general-purpose knowledge base — scoped to C systems work.
  C++ projects could in principle work but the chunker hasn't been
  exercised on them.
- No fine-tuning or model training — pure retrieval.
