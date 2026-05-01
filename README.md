# mcp-c

MCP service pair for C development. Two containers:

| Subdir | Container | Port | Purpose |
|--------|-----------|------|---------|
| `service/` | `c-mcp-build` | 5192 | `build`, `run_tests`, `lint`, `analyze` for C projects |
| `knowledge/` | `c-mcp-knowledge` | 5194 | RAG over C source, project source, curated docs |

The two halves are paired: `service/` fires fire-and-forget POSTs at
`knowledge/`'s `/ingest` endpoint, so test failures, fixes, lint errors,
and sanitizer reports accumulate as retrievable context.

The build tool auto-detects the project's build system in this order:
`CMakeLists.txt` → cmake, `meson.build` → meson, `Makefile` → make,
otherwise compile `*.c` directly with gcc.

## Consumers

Launched by [`claude-sandbox-core`](https://github.com/jasonuithol/claude-sandbox-core)
via `bin/start.sh c <project>` (the `c` domain conf lists this repo in
`MCP_REPOS`). Any MCP client speaking streamable HTTP can mount these
services — the protocol is provider-agnostic.

## Usage

```bash
./setup.sh                # one-time, idempotent (builds both images)
./start.sh                # bring up both containers
./stop.sh                 # shut them down (containers preserved for revival)
./clean.sh                # remove containers + images (full teardown)

knowledge/seed.sh         # first-time KB seed
```

To validate setup works from bare state:

```bash
./clean.sh && ./setup.sh && ./start.sh
```

Both containers use host networking (ports above). The knowledge
container needs an NVIDIA GPU + container toolkit for accelerated
embeddings (see `knowledge/setup-gpu.sh`).

## Design

See `knowledge/CLAUDE.md` for the knowledge service's design
(chunking strategy, ingest routing, metadata schema, known concerns).
