## A5: Extract `backfill.py` — embed/tool_error/clean/redact/chunk backfills

**Status:** not started
**Dependencies:** A2, A3

### Scope

Move all backfill operations out of `ingest.py` into `src/convo_recall/backfill.py`. These are batch-mode functions invoked by CLI subcommands (`recall embed-backfill`, `recall tool-error-backfill`, `recall backfill-clean`, `recall backfill-redact`, `recall chunk-backfill`); they don't run on the ingest hot-path. Pure file-move; zero behavior change. Can run in parallel with A4 (no shared symbols).

### Key Components

- `backfill.embed_backfill(con)` — re-embeds rows with missing `message_vecs`, newest-first, capped at 2000.
- `backfill._confirm_destructive(label, n_changed, samples, …)` — shared user-confirm prompt for backfill-clean / backfill-redact.
- `backfill.backfill_clean(con, confirm=False)` — re-runs `_clean_content` over historical rows; ANSI/box-drawing removal.
- `backfill.backfill_redact(con, confirm=False)` — re-runs secret-redaction patterns over historical `messages.content` and rebuilds FTS.
- `backfill.chunk_backfill(con, confirm=False)` — re-embeds long messages through new sidecar chunker (semantics shifted post-TD-007 reframe; comment clarifies).
- `backfill._backfill_insert_tool_error(con, agent, project_id, session_id, uuid, text, timestamp)` — shared insert helper used by all three per-agent walkers.
- `backfill._backfill_claude_tool_errors(con)`, `_backfill_codex_tool_errors(con)`, `_backfill_gemini_tool_errors(con)` — per-agent tool_error walkers (multi-agent capture from v0.3.4).
- `backfill.tool_error_backfill(con)` — public entry point dispatching to all three per-agent walkers.

### Rough File Inventory

- New: 1 file (`src/convo_recall/backfill.py`) — ~600 LOC moved.
- Modified: 1 file (`src/convo_recall/ingest.py`) — call sites and re-exports.
- Modified: 0 test files (`tests/test_backfill_safety.py` reaches via shim).

### Risks & Blockers

- **`backfill_clean` calls `_clean_content`** — that helper currently lives in `ingest.py` and is part of the write-path (A7). For A5, import it from the eventual A7 home: `from .ingest.writer import _clean_content` — but A7 hasn't landed yet. Two options:
  1. **Defer the `backfill_clean` extraction to A7** — split A5 into A5a (everything else) and A5b (`backfill_clean` after writer.py exists).
  2. **Temporary internal import**: A5 imports `_clean_content` from the still-monolithic `ingest.py` (`from .ingest import _clean_content`); A7 moves it to `writer.py` and updates A5's import.
  **Decision:** Option 2 — keeps the PR boundary clean. A5 lands first; A7 will adjust the one import line in `backfill.py`.
- **`_backfill_*_tool_errors` helpers iterate Claude/Codex/Gemini JSONL files** — they need `_iter_claude_files` etc., which currently live in `ingest.py`. Same temporary-import pattern as above; A7 cleans up.
- **Order-of-extraction note:** the temporary `from .ingest import _clean_content, _iter_claude_files, _iter_codex_files, _iter_gemini_files, _extract_tool_result_text, _is_error_result, _codex_event_msg_error, _codex_fco_error, _gemini_record_error, _gemini_tool_call_error, _legacy_claude_slug, _legacy_codex_slug, _legacy_gemini_slug, _session_id_from_path, _load_gemini_aliases` is acceptable as long as it's documented at the top of `backfill.py` and removed in A7.

### Done Criteria

- [ ] `src/convo_recall/backfill.py` exists with all symbols listed above.
- [ ] `from convo_recall.ingest import embed_backfill, tool_error_backfill, backfill_clean, backfill_redact, chunk_backfill` still works (re-export).
- [ ] `pytest tests/test_backfill_safety.py` → green.
- [ ] `recall tool-error-backfill` indexes 0 new rows on the maintainer DB (already-current → no new errors found).
- [ ] `recall backfill-redact` (no `--confirm`) shows DRY-RUN output identical to v0.3.6 baseline.
- [ ] `recall embed-backfill` is a no-op when 100% of messages have vecs.

### Verification Artifacts

| File | Must Contain | Why |
|------|-------------|-----|
| `src/convo_recall/backfill.py` | `def embed_backfill(` and `def tool_error_backfill(` | Public entry points present |
| `src/convo_recall/backfill.py` | `def _backfill_claude_tool_errors(` and `def _backfill_codex_tool_errors(` and `def _backfill_gemini_tool_errors(` | Per-agent walkers moved |
| `src/convo_recall/ingest.py` | `from .backfill import` | Re-export block in place |
