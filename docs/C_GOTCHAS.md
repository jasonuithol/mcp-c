# C gotchas (curated)

These are the recurring traps the knowledge base picks up over time.
Treat this file as the seed; the index grows as test failures and
sanitizer reports accumulate.

## Memory

- `malloc` returning NULL on OOM is rarely tested. Either check it or
  wrap allocations in an xmalloc-style helper that aborts.
- `free` on a pointer not returned by `malloc/calloc/realloc` is UB.
  Includes freeing the middle of a heap allocation.
- `realloc(p, 0)` is implementation-defined since C23 — prefer
  `free(p); p = NULL;`.
- Returning a pointer to a stack local is the classic dangling-pointer
  bug. ASan catches this as `stack-use-after-return`.

## Strings

- `strncpy` does not always NUL-terminate. Use `snprintf` or zero-init
  the destination first.
- `strcat` walks the destination from start each call — quadratic in a
  loop. Track the end yourself or use `memcpy`.
- Buffer sizes are bytes, not characters. UTF-8 strings are a sequence
  of bytes for `strlen`.

## Integer rules

- Signed overflow is UB. Unsigned overflow wraps. Mixing them in
  comparisons does the usual integer promotion dance — easy to get
  wrong.
- `size_t` is unsigned; subtracting two of them and storing in
  `ssize_t` may overflow.
- `int` is at least 16 bits per the standard. In practice 32 on
  Linux x86_64, but don't bake that in.

## Build / link

- A function declared `static` in a header that's included from
  multiple TUs creates a separate copy per TU. Surprising but legal.
- Forgetting `-lm` for math functions still compiles — the link step
  is what fails.
- `-Wall -Wextra` are not exhaustive. `-Wpedantic` and
  `-Wconversion` catch more.

## Undefined behaviour worth memorising

- Reading uninitialised memory.
- Aliasing (writing through a pointer of one type and reading through
  another, except `char*`).
- Modifying a string literal.
- `i = i++ + ++i;` — sequence point hell.

The knowledge base will accumulate project-specific traps over time
via `analyze` and `run_tests` failure ingest. This file is the static
seed; query the KB for the dynamic part.
