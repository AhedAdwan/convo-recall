## A7: Extract `ingest/` subpackage — claude/gemini/codex/writer/scan

**Status:** not started
**Dependencies:** A1, A2, A3, A4, A5

### Scope

Move the entire write-path out of the monolithic `ingest.py` into a new `src/convo_recall/ingest/` subpackage with five sibling modules: per-agent parsers, the shared persistence writer, and the scan/dispatch layer. This is the largest extraction (~1,200 LOC) and depends on A1–A5 because every write-path function needs DB / embed / identity / backfill helpers from those modules.

After A7, the original top-level `ingest.py` file collides with the new `ingest/` subpackage name. Resolution: delete `ingest.py` and let `ingest/__init__.py` carry the back-compat shim. The package layout becomes `convo_recall/ingest/__init__.py` (shim) + the five subfiles. The shim re-exports legacy names from the new sibling modules (`db`, `embed`, `query`, `backfill`, `admin`, `identity`) AND from inside the subpackage itself (`from .writer import _persist_message; from .scan import scan_all; …`).

### Key Components

- `ingest/__init__.py` — re-exports for back-compat (replaces the old `ingest.py` shim).
- `ingest/claude.py` — `ingest_file`, `_iter_claude_files`, `_extract_text`, `_extract_tool_result_text`, `_is_error_result`, `_ERROR_PATTERNS`, `_session_id_from_path`.
- `ingest/gemini.py` — `ingest_gemini_file`, `_iter_gemini_files`, `_load_gemini_aliases`, `_gemini_record_error`, `_gemini_tool_call_error`.
- `ingest/codex.py` — `ingest_codex_file`, `_iter_codex_files`, `_codex_event_msg_error`, `_codex_fco_error`.
- `ingest/writer.py` — `_persist_message`, `_upsert_session`, `_upsert_ingested_file`, `_clean_content`, `_expand_code_tokens`. The shared insert layer that all three per-agent parsers call into.
- `ingest/scan.py` — `scan_one_agent`, `scan_all`, `_dispatch_ingest`, `watch_loop`, `_AGENT_INGEST` (dispatch table), `_AGENT_ITERATORS`, `detect_agents`, `load_config`, `save_config`. The "front door" for all write operations.

### Rough File Inventory

- New: 6 files (`ingest/__init__.py`, `claude.py`, `gemini.py`, `codex.py`, `writer.py`, `scan.py`) — ~1,200 LOC moved.
- New: 1 dir (`src/convo_recall/ingest/`).
- Removed: 1 file (`src/convo_recall/ingest.py` deleted; `ingest/__init__.py` carries the shim).
- Modified: 1 file (`src/convo_recall/cli.py`) — its `from convo_recall import ingest` continues to work because `ingest/` is now a package, but call sites that referenced `ingest.X` for symbols now in sibling modules need pointer updates. Most of those updates are deferred to A8; for A7, `cli.py` keeps using `ingest.X` and the `ingest/__init__.py` shim resolves them.
- Modified: backfill.py and admin.py (A5/A6) — remove the temporary `from .ingest import …` lines; replace with `from .ingest.writer import _clean_content`, `from .ingest.claude import _iter_claude_files, _session_id_from_path, _extract_tool_result_text, _is_error_result`, `from .ingest.codex import _iter_codex_files, _codex_event_msg_error, _codex_fco_error`, `from .ingest.gemini import _iter_gemini_files, _gemini_record_error, _gemini_tool_call_error, _load_gemini_aliases`, `from .ingest.scan import detect_agents`, etc.

### Risks & Blockers

- **File-vs-directory replacement** — `git mv ingest.py ingest/__init__.py.tmp` then `mkdir ingest && mv ingest/__init__.py.tmp ingest/__init__.py`. Watch out for `__pycache__/ingest.cpython-*.pyc` confusing the import system mid-PR; clean before committing.
- **Hatch wheel must include `ingest/`** — Hatch auto-includes everything under `packages = ["src/convo_recall"]`, so the subpackage ships automatically. Smoke test: `hatch build && unzip -l dist/*.whl | grep ingest/`.
- **Import cycles** — every sibling module in `ingest/` imports from `..writer`, `..db`, `..embed`, `..identity`, `..backfill`. Strict one-way: `claude.py / gemini.py / codex.py → writer.py → db.py + identity.py + embed.py`. `scan.py → claude / gemini / codex / writer + db + ingest config helpers`. `__init__.py` imports from every sibling for re-exports — no sibling imports from `__init__.py`.
- **`_AGENT_INGEST` dispatch table** — currently lives at module top of `ingest.py` and references `ingest_file`, `ingest_gemini_file`, `ingest_codex_file`. Now lives in `scan.py` and references the same names imported from the per-agent modules. Update all call sites.
- **Hot-path TD-006 fix preserved** — `ingest_file` lines 1286-1311 (the tool_result harvesting independent of text) is the regression-protected hot path. Run `tests/test_ingest.py::test_tool_error_*` (whichever covers TD-006 regression) immediately after A7 lands to verify the structure didn't drift.

### Done Criteria

- [ ] `src/convo_recall/ingest/` exists with 6 files (`__init__.py`, `claude.py`, `gemini.py`, `codex.py`, `writer.py`, `scan.py`).
- [ ] `src/convo_recall/ingest.py` no longer exists (replaced by `ingest/__init__.py`).
- [ ] `pytest tests/` → full suite green.
- [ ] `recall ingest` produces 0 new rows when run against an already-ingested project (idempotency preserved).
- [ ] TD-006 regression test green (the hot-path tool_error harvesting test).
- [ ] `pip install -e .` and `hatch build` both succeed; built wheel contains `convo_recall/ingest/__init__.py` and the 5 sibling files.
- [ ] Temporary `from .ingest import …` lines in `backfill.py` and `admin.py` are gone; replaced with canonical `from .ingest.X import Y`.

### Verification Artifacts

| File | Must Contain | Why |
|------|-------------|-----|
| `src/convo_recall/ingest/__init__.py` | `from .claude import` and `from .scan import` | Shim re-exports per-agent + scan |
| `src/convo_recall/ingest/claude.py` | `def ingest_file(` | Claude parser present |
| `src/convo_recall/ingest/gemini.py` | `def ingest_gemini_file(` | Gemini parser present |
| `src/convo_recall/ingest/codex.py` | `def ingest_codex_file(` | Codex parser present |
| `src/convo_recall/ingest/writer.py` | `def _persist_message(` and `def _clean_content(` | Shared writer present |
| `src/convo_recall/ingest/scan.py` | `def scan_all(` and `_AGENT_INGEST = {` | Dispatch + scan present |
