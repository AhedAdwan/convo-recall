## A8: `ingest.py` shim + cli/tests rewiring + v0.4.0 release

**Status:** not started
**Dependencies:** A7

### Scope

Final cleanup pass: thin the `ingest/__init__.py` shim down to the canonical legacy-symbol re-exports + a single `DeprecationWarning`, rewire `cli.py` to import from the new sibling modules directly, update `tests/*.py` to use canonical imports, then bump version to v0.4.0 and cut the release. After A8, the only callers using `from convo_recall.ingest import …` are external consumers (none known) and they get a deprecation warning. Internal code uses the new modules directly.

### Key Components

1. **`ingest/__init__.py` final form** (~50 lines):
   - Module-level `warnings.warn("convo_recall.ingest is deprecated; import from convo_recall.{db,query,embed,backfill,admin,identity,ingest.scan,ingest.writer} instead. This shim will be removed in v0.5.0.", DeprecationWarning, stacklevel=2)`.
   - `from ..db import open_db, close_db, DB_PATH, EMBED_DIM, _record_migration, _migration_applied, _MIGRATION_AGENT_COLUMN, _MIGRATION_FTS_PORTER, …`
   - `from ..embed import embed, EMBED_SOCK, _vec_search, _vec_insert, _vec_count, _vec_ok, _wait_for_embed_socket, …`
   - `from ..query import search, tail, _safe_fts_query, _resolve_project_ids, _tail_format_ago, _decay, …`
   - `from ..backfill import embed_backfill, tool_error_backfill, backfill_clean, backfill_redact, chunk_backfill`
   - `from ..admin import stats, doctor, forget`
   - `from ..identity import _project_id, _display_name, _legacy_project_id, _legacy_claude_slug, _legacy_codex_slug, _legacy_gemini_slug, _gemini_hash_project_id`
   - `from .claude import ingest_file`
   - `from .gemini import ingest_gemini_file`
   - `from .codex import ingest_codex_file`
   - `from .writer import _persist_message, _clean_content, _upsert_session, _upsert_project, _expand_code_tokens`
   - `from .scan import scan_all, scan_one_agent, watch_loop, _dispatch_ingest, detect_agents, load_config, save_config`

2. **`cli.py` rewiring** — replace `import convo_recall.ingest as ingest` (and all `ingest.X` call sites) with direct imports per concern:
   - `from convo_recall import db, embed, query, backfill, admin, identity`
   - `from convo_recall.ingest import scan, writer`
   - Update each `ingest.search(...)` → `query.search(...)`, `ingest.open_db(...)` → `db.open_db(...)`, etc.

3. **`tests/*.py` rewiring** — `import convo_recall.ingest as ingest` and `from convo_recall import ingest` patterns updated to canonical imports per concern. Tests still pass via the shim too, but the canonical paths exercise the new module structure under `python -W error::DeprecationWarning`.

4. **`pyproject.toml`** — `version = "0.3.6"` → `"0.4.0"`.

5. **`CHANGELOG.md`** — promote `[Unreleased]` → `[0.4.0] — YYYY-MM-DD`. Entry covers:
   - **Internal:** `ingest.py` (3,626 lines) decomposed into `db.py`, `embed.py`, `query.py`, `backfill.py`, `admin.py`, `identity.py`, and the `ingest/` subpackage (`claude.py`, `gemini.py`, `codex.py`, `writer.py`, `scan.py`). Closes TD-008.
   - **Deprecated:** the `convo_recall.ingest` namespace now emits `DeprecationWarning` on import. All legacy symbols still resolve via re-exports for one release. Removal scheduled for v0.5.0.
   - **No user-visible behavior change.** Same CLI surface, same DB schema, same hooks.

6. **`docs/TECH_DEBT.md`** — TD-008 → Closed. Reference v0.4.0.

### Rough File Inventory

- Modified: 1 file (`src/convo_recall/ingest/__init__.py`) — thinned to ~50 lines.
- Modified: 1 file (`src/convo_recall/cli.py`) — direct imports per concern, ~17 reference points updated.
- Modified: ~5–10 files under `tests/` — `test_ingest.py`, `test_ingest_docstring_truth.py`, `test_ingest_project_id.py`, `test_install_public_api.py`, etc. Find with `grep -l "from convo_recall.ingest\|import convo_recall.ingest" tests/`.
- Modified: 1 file (`pyproject.toml`) — version bump.
- Modified: 1 file (`CHANGELOG.md`) — `[0.4.0]` entry.
- Modified: 1 file (`docs/TECH_DEBT.md`) — TD-008 closed.
- New: 1 git tag (`v0.4.0`).

### Risks & Blockers

- **`tests/test_ingest_docstring_truth.py`** — the test parses `ingest.py` module docstring asserting `CONVO_RECALL_*` defaults. After A8, the docstring lives in `ingest/__init__.py` (or moves to `db.py`/`embed.py` per actual symbol home). Decision: docstring SOURCE moves to the new module homes (`DB_PATH` doc → `db.py`, `EMBED_SOCK` doc → `embed.py`); the test is updated to parse the new module's docstring instead.
- **`DeprecationWarning` not raised in pytest by default** — Python's default filters silence `DeprecationWarning` in user code. Add a CI step `python -W error::DeprecationWarning -c "from convo_recall.ingest import search"` and assert the warning fires. Conversely, `python -W error::DeprecationWarning -c "from convo_recall.query import search"` must NOT warn.
- **External-consumer backward compat** — to my knowledge there are no external consumers; the deprecation gives any silent ones one release of warning before removal in v0.5.0.
- **Wheel-build smoke test** — `hatch build && pip install dist/convo_recall-0.4.0-*.whl --target /tmp/wheel-test && python -c "from convo_recall import search, tail; from convo_recall.ingest import search as legacy_search"`. Both should work.
- **`recall --version`** — must print `0.4.0` (derives from `importlib.metadata.version("convo-recall")` per v0.3.1 fix, so the bump in `pyproject.toml` is the only edit needed).

### Done Criteria

- [ ] `src/convo_recall/ingest/__init__.py` ≤ 60 lines, only re-exports + one `warnings.warn` call.
- [ ] `cli.py` does not contain any `ingest.X` references for symbols now in sibling modules (`db`, `embed`, `query`, `backfill`, `admin`, `identity`).
- [ ] `pytest tests/` → full suite green.
- [ ] `python -W error::DeprecationWarning -c "from convo_recall.ingest import search"` exits 1 (warning fires).
- [ ] `python -W error::DeprecationWarning -c "from convo_recall import search"` exits 0 (canonical path silent).
- [ ] `recall --version` prints `0.4.0`.
- [ ] `docs/TECH_DEBT.md` shows TD-008 status `Closed`.
- [ ] `git tag -l v0.4.0` returns the tag.
- [ ] `hatch build && unzip -l dist/convo_recall-0.4.0-*.whl | grep -E "ingest/__init__|db.py|embed.py|query.py|backfill.py|admin.py|identity.py"` shows all 7 modules in the wheel.

### Verification Artifacts

| File | Must Contain | Why |
|------|-------------|-----|
| `src/convo_recall/ingest/__init__.py` | `warnings.warn(` and `DeprecationWarning` | Deprecation notice in place |
| `src/convo_recall/ingest/__init__.py` | `from ..db import` and `from ..query import` | Cross-package re-exports wired |
| `pyproject.toml` | `version = "0.4.0"` | Version bump landed |
| `CHANGELOG.md` | `## [0.4.0]` | Release entry promoted |
| `docs/TECH_DEBT.md` | `TD-008` row contains `Closed` | TD register updated |
