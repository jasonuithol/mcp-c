"""Source-specific extraction for C knowledge.

C has no Python-grade ast module in the stdlib, so we lean on regex to
chunk source by top-level function and struct/typedef/enum definitions.
This is good-enough for retrieval — perfect parsing isn't the goal.
"""

from __future__ import annotations

import re
from pathlib import Path

# ── C pattern → tag map ──────────────────────────────────────────────────────

PATTERN_TAGS: list[tuple[re.Pattern, str]] = [
    # memory
    (re.compile(r"\b(?:malloc|calloc|realloc|free)\s*\("),                "memory"),
    (re.compile(r"\b(?:memcpy|memmove|memset|memcmp)\s*\("),              "mem-ops"),
    (re.compile(r"\b(?:alloca|VLA)\b"),                                   "stack-alloc"),

    # strings
    (re.compile(r"\b(?:strcpy|strncpy|strlcpy|strdup|strndup)\s*\("),     "string-copy"),
    (re.compile(r"\b(?:strcat|strncat|strlcat)\s*\("),                    "string-concat"),
    (re.compile(r"\b(?:sprintf|snprintf|asprintf|vsprintf|vsnprintf)\s*\("), "string-format"),
    (re.compile(r"\b(?:strcmp|strncmp|strcasecmp|memcmp)\s*\("),          "string-compare"),

    # IO
    (re.compile(r"\b(?:fopen|fclose|fread|fwrite|fprintf|fscanf|fgets|fputs)\s*\("), "stdio"),
    (re.compile(r"\b(?:open|close|read|write|lseek|fsync)\s*\("),         "posix-io"),
    (re.compile(r"\b(?:printf|puts|getchar|putchar|scanf)\s*\("),         "console"),

    # threads / sync
    (re.compile(r"\bpthread_(?:create|join|mutex|cond|rwlock)"),          "pthread"),
    (re.compile(r"\b(?:atomic_|_Atomic|stdatomic\.h)"),                   "atomic"),

    # signals / process
    (re.compile(r"\b(?:fork|execve?|wait|waitpid|kill|signal|sigaction)\s*\("), "process"),

    # network
    (re.compile(r"\b(?:socket|bind|listen|accept|connect|send|recv)\s*\("), "socket"),
    (re.compile(r"\b(?:htons|htonl|ntohs|ntohl|inet_(?:aton|ntoa|pton|ntop))\s*\("), "endian-net"),

    # data structures (heuristic)
    (re.compile(r"\b(?:struct\s+\w+\s*\*\s*next|->next\b)"),              "linked-list"),
    (re.compile(r"\b(?:hash|hashmap|hashtable|HASH_)"),                   "hashmap"),
    (re.compile(r"\b(?:ringbuffer|ring_buffer|circular_buffer)"),         "ringbuffer"),

    # error patterns
    (re.compile(r"\b(?:errno|perror|strerror)\b"),                        "errno"),
    (re.compile(r"\b(?:assert|static_assert|_Static_assert)\s*\("),       "assert"),
    (re.compile(r"\bgoto\s+(?:err|cleanup|fail)\w*"),                     "goto-cleanup"),

    # build / preprocessor
    (re.compile(r"^\s*#\s*include\s*<", re.MULTILINE),                    "include"),
    (re.compile(r"^\s*#\s*define\s+\w+\([^)]*\)", re.MULTILINE),          "function-macro"),
    (re.compile(r"\b__attribute__\s*\(\("),                               "gcc-attr"),

    # standards
    (re.compile(r"\b(?:_Generic|_Thread_local|alignas|alignof|noreturn)\b"), "c11"),
]


def detect_tags(text: str) -> list[str]:
    """Scan text for known patterns and return matching tags (deduped, ordered)."""
    tags: list[str] = []
    seen: set[str] = set()
    for pattern, tag in PATTERN_TAGS:
        if tag in seen:
            continue
        if pattern.search(text):
            tags.append(tag)
            seen.add(tag)
    return tags


# ── C source structure (regex-based) ──────────────────────────────────────────

# A function definition at file scope — heuristic that works on
# reasonably-formatted code. Matches a return type, name, parens, and a
# brace on the same line (with whitespace). Static and inline modifiers OK.
# Won't catch K&R-style or definitions with the brace on the next line,
# but those are rare in modern code.
_FUNC_DEF_RE = re.compile(
    r"""
    ^(?P<head>
        (?:(?:static|inline|extern|_Noreturn|noreturn)\s+)*    # optional modifiers
        (?:[\w\s\*]+?)                                          # return type (greedy stops at ident)
        \s+\*?\s*
        (?P<name>\w+)                                           # function name
        \s*\([^;{}]*\)                                          # arg list
        \s*
    )
    \{                                                          # opening brace
    """,
    re.MULTILINE | re.VERBOSE,
)

# Top-level type definitions worth pulling out as their own chunk.
_STRUCT_RE  = re.compile(
    r"^(?:typedef\s+)?struct\s+(?P<name>\w+)\s*\{",
    re.MULTILINE,
)
_TYPEDEF_RE = re.compile(
    r"^typedef\s+[^;{}]+?\s+(?P<name>\w+)\s*;",
    re.MULTILINE,
)
_ENUM_RE = re.compile(
    r"^(?:typedef\s+)?enum\s+(?P<name>\w+)?\s*\{",
    re.MULTILINE,
)


def _find_brace_end(source: str, open_pos: int) -> int:
    """Return index just past the matching '}' for the '{' at open_pos.

    Walks naïvely — does not skip over comments or string literals. Good
    enough for chunk boundaries; a stray '}' in a string would mis-cut a
    chunk but not crash. Returns -1 if unmatched.
    """
    depth = 0
    i = open_pos
    n = len(source)
    while i < n:
        c = source[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return -1


def extract_top_level_nodes(source: str) -> list[dict]:
    """Return one dict per top-level function/struct/enum/typedef.

    Each dict: {name, kind, body, start_line}.

    'function' chunks include the body. 'struct' / 'enum' chunks include
    the whole brace block. 'typedef' chunks are single-line.
    """
    out: list[dict] = []
    seen_spans: list[tuple[int, int]] = []

    def add(kind: str, name: str, start: int, end: int) -> None:
        # Skip if this span is contained within a span we've already added
        # (e.g. an inner struct inside a function).
        for s, e in seen_spans:
            if start >= s and end <= e:
                return
        seen_spans.append((start, end))
        body = source[start:end].rstrip()
        start_line = source.count("\n", 0, start)
        out.append({"name": name, "kind": kind, "body": body, "start_line": start_line})

    for m in _FUNC_DEF_RE.finditer(source):
        # The match's position is at the start of the head; find the open
        # brace within the match, then walk for the close.
        head_start = m.start()
        brace_open = source.find("{", head_start)
        if brace_open < 0:
            continue
        brace_close = _find_brace_end(source, brace_open)
        if brace_close < 0:
            continue
        add("function", m.group("name"), head_start, brace_close)

    for m in _STRUCT_RE.finditer(source):
        head_start = m.start()
        brace_open = source.find("{", head_start)
        if brace_open < 0:
            continue
        brace_close = _find_brace_end(source, brace_open)
        if brace_close < 0:
            continue
        # Include trailing typedef name + ';' if present.
        tail = source[brace_close:brace_close + 200]
        end = brace_close + (tail.find(";") + 1 if ";" in tail else 0)
        add("struct", m.group("name"), head_start, end if end > brace_close else brace_close)

    for m in _ENUM_RE.finditer(source):
        head_start = m.start()
        brace_open = source.find("{", head_start)
        if brace_open < 0:
            continue
        brace_close = _find_brace_end(source, brace_open)
        if brace_close < 0:
            continue
        tail = source[brace_close:brace_close + 200]
        end = brace_close + (tail.find(";") + 1 if ";" in tail else 0)
        name = m.group("name") or "(anonymous)"
        add("enum", name, head_start, end if end > brace_close else brace_close)

    for m in _TYPEDEF_RE.finditer(source):
        # Skip typedefs that are already covered by a struct/enum capture.
        add("typedef", m.group("name"), m.start(), m.end())

    out.sort(key=lambda x: x["start_line"])
    return out


def extract_module_name(source_path: str, project_root: str) -> str:
    """Derive a dotted-ish module name from a file path relative to its project root.

    e.g. /opt/projects/MyProj/src/net/listener.c
         + /opt/projects/MyProj
         -> src.net.listener
    """
    try:
        p = Path(source_path).resolve()
        root = Path(project_root).resolve()
        rel = p.relative_to(root)
    except (ValueError, OSError):
        return Path(source_path).stem
    parts = list(rel.with_suffix("").parts)
    return ".".join(parts) or Path(source_path).stem
