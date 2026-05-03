## A4: Extract `query.py` — search, tail, RRF, decay

**Status:** not started
**Dependencies:** A2, A3

### Scope

Move the read-path (search, tail, and their helpers) out of `ingest.py` into `src/convo_recall/query.py`. This is the largest single extraction (~750 LOC) and the highest-leverage one for testability — once query is its own module, tests touching only search no longer transitively pull JSONL parsers, the embed HTTP client, etc. Pure file-move; zero behavior change.

### Key Components

- `query.MAX_QUERY_LEN = 2048`, `query.RRF_K = 60`, `query.DECAY_HALF_LIFE_DAYS = 90`.
- `query._decay(timestamp, half_life_days=...)` — exponential time-decay weight for RRF.
- `query._safe_fts_query(query)` — escape FTS5 special characters; cap at `MAX_QUERY_LEN`.
- `query._resolve_project_ids(con, project, all_projects)` — display_name → project_id list with exact-first / LIKE-fallback warning.
- `query._resolve_tail_session(con, project, all_projects, session)` — session-id resolver for `recall tail`.
- `query._fetch_context(con, session_id, ...)` — fetch surrounding messages around a hit.
- `query._tail_*` helpers and `_TAIL_*` constants (formatting, wrapping, glyphs, ago-formatting, clock).
- `query.search(con, query, limit=10, ...) -> list[dict]` — the headline read function.
- `query.tail(con, n=..., ...) -> list[dict]`.

### Rough File Inventory

- New: 1 file (`src/convo_recall/query.py`) — ~750 LOC moved.
- Modified: 1 file (`src/convo_recall/ingest.py`) — call sites and re-exports.
- Modified: 0 test files in this PR (rewiring deferred to A8). `tests/test_ingest.py::test_recall_cliff_*` and similar reach `ingest.search` via shim.

### Risks & Blockers

- **`search()` calls `embed("…")` for query embedding** — A3 must already be done. `query.py` imports `from .embed import embed, _vec_search`.
- **`search()` calls `_decay`, `_safe_fts_query`, `_resolve_project_ids`** — all internal to `query.py`. No new cross-module coupling.
- **`tail()` calls `_resolve_tail_session`, `_fetch_context`, `_tail_*`** — also internal.
- **`query.py` must NOT import from any ingest path** — verify with `grep -E "from \.ingest|from .writer|from .scan|from .claude|from .gemini|from .codex" src/convo_recall/query.py` returns nothing. Read-path is fully independent of write-path.
- **Golden snapshot test for behavior preservation:** before this PR, capture `recall search foo --json --limit 5` output for ~5 representative queries. After A4, run the same queries and `diff` — must be byte-identical (modulo timestamps in `_tail_format_ago` if any creep into search output, which they shouldn't).

### Done Criteria

- [ ] `src/convo_recall/query.py` exists with all symbols listed above.
- [ ] `from convo_recall.ingest import search, tail` still works (re-export).
- [ ] `pytest tests/` → all currently-passing tests stay green.
- [ ] `recall search foo --limit 5 --json` and `recall tail 30 --json` produce byte-identical output to v0.3.6 baseline (golden snapshot diff).
- [ ] `grep -E "from \\.ingest|from \\.writer" src/convo_recall/query.py` returns nothing.

### Verification Artifacts

| File | Must Contain | Why |
|------|-------------|-----|
| `src/convo_recall/query.py` | `def search(` and `def tail(` | Headline functions present |
| `src/convo_recall/query.py` | `def _decay(` and `def _safe_fts_query(` | RRF/FTS helpers moved |
| `src/convo_recall/query.py` | `from .embed import` | Dep direction (query → embed) |
| `src/convo_recall/ingest.py` | `from .query import` | Re-export block in place |
