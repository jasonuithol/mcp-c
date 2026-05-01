# Ingest contract — c-build → c-knowledge

`c-build` POSTs every tool execution to
`$KNOWLEDGE_URL` (default `http://localhost:5194/ingest`) as
fire-and-forget JSON. The router in `knowledge/ingest/router.py`
decides what to chunk and index.

## Payload shape

```json
{
    "tool":      "run_tests",
    "args":      {"project": "MyProj", "test_filter": ""},
    "result":    "<full text result returned by the tool>",
    "success":   false,
    "service":   "mcp-build",
    "timestamp": "2026-04-30T08:00:00Z"
}
```

`result` is a JSON envelope for some tools:

- `run_tests`: `{"summary", "json_report" or "ctest_log", "stdout"}`
- `analyze`:   `{"summary", "valgrind_xml" or "asan_log", "stdout"}`
- All others: plain text.

## Routing table

| Tool | Success → | Failure → |
|------|-----------|-----------|
| `build` | skip | index `build-error` chunk |
| `run_tests` | record `test-fix` for every previously-failing node id that now passes | index `test-failure` chunk per failing node id |
| `lint` | skip | index `lint-error` chunk |
| `analyze` | skip if no findings; otherwise index `sanitizer-report` | index `sanitizer-report` chunk |

`build` and `lint` failures are coarse — one chunk per invocation. Test
failures are per-node so `ask_tagged("...", ["test-failure"])` returns
specific failing tests, not whole logs.

## State

`router.py` keeps a per-node-id failure buffer at
`/opt/knowledge/test_failure_buffer.json` so fail→pass transitions can
be detected across container restarts. Capped at 500 entries by
most-recently-touched.
