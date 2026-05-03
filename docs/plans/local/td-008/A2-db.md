## A2: Extract `db.py` — schema, migrations, open_db, connection helpers

**Status:** not started
**Dependencies:** A1

### Scope

Move all schema definition, migration code, connection lifecycle, and FTS bootstrap out of `ingest.py` into `src/convo_recall/db.py`. This is the second-from-bottom of the dependency tree — `db` imports from `identity` (for migration backfill) and is imported by `embed`, `query`, `backfill`, `admin`, and the `ingest/` subpackage. Pure file-move; zero behavior change.

### Key Components

- `db.DB_PATH` — `Path(os.environ.get("CONVO_RECALL_DB", "~/.local/share/convo-recall/conversations.db"))`.
- `db.EMBED_DIM = 1024`.
- `db.open_db(readonly=False)` — apsw connection factory. Loads sqlite-vec, sets WAL, runs `_init_schema` + migrations.
- `db.close_db(con)` — connection teardown.
- `db._init_schema(con)` — `CREATE TABLE IF NOT EXISTS` for `messages`, `sessions`, `ingested_files`, `projects`, `messages_fts`, `message_vecs`.
- `db._init_vec_tables(vc)` — sqlite-vec `vec0` virtual tables.
- `db._upsert_project(con, project_id, display_name, cwd_realpath)`.
- `db._has_column`, `db._ensure_migrations_table`, `db._migration_applied`, `db._record_migration`.
- `db._migrate_add_agent_column`, `db._migrate_fts_porter`, `db._migrate_project_id` — the three v2/v3/v4 migrations.
- `db._enable_wal_mode(con)` — `PRAGMA journal_mode=WAL`.
- `db._harden_perms(path, mode)` — chmod to 0600.

### Rough File Inventory

- New: 1 file (`src/convo_recall/db.py`) — ~520 LOC moved.
- Modified: 1 file (`src/convo_recall/ingest.py`) — call sites and re-exports.
- Modified: 0 test files (tests reach `ingest.open_db`, `ingest.DB_PATH` via the shim; will rewire in A8).

### Risks & Blockers

- **`_migrate_project_id` calls identity helpers** (`_project_id`, `_legacy_*_slug`, `_scan_*_cwd`). After A1 these are in `identity.py`. Add `from .identity import _project_id, _display_name, _legacy_project_id, _legacy_claude_slug, _legacy_codex_slug, _legacy_gemini_slug, _scan_claude_cwd, _scan_codex_cwd, _scan_gemini_cwd, _gemini_hash_project_id` at the top of `db.py`. One-way dependency; no cycle.
- **WAL mode side effect** — `_enable_wal_mode` touches the DB file's WAL header. Re-confirm `tests/test_migration_project_id.py` still creates a fresh tmp DB (not real one) before running.
- **sqlite-vec extension load** — `open_db` calls `apsw.Connection.enable_load_extension(True)` and loads sqlite-vec. The `_VEC_ENABLED` per-connection state lives in `embed.py` (A3), so A2's `open_db` must defer the vec-init to a function it imports from `embed` — OR the `_VEC_ENABLED` dict moves to `db.py` and `embed` reads it. **Decision for A2:** keep `_init_vec_tables` and the load-extension call in `db.py`; `_VEC_ENABLED` dict moves to `db.py` too (it's keyed by connection). `embed.py` reads `db._VEC_ENABLED[con]` to decide if vec is available. (See README → "Cross-module conventions" for the rule.)
- **`tests/test_ingest_docstring_truth.py`** — parses `ingest.py` module docstring asserting `CONVO_RECALL_*` defaults including `CONVO_RECALL_DB`. Even though `DB_PATH` / `EMBED_DIM` move to `db.py` in this PR, the docstring stays in `ingest.py` until A8. Do NOT remove the lines referencing `CONVO_RECALL_DB` from the `ingest.py` docstring; A8 is the canonical move-and-update. (Mirrors A3's entry for `CONVO_RECALL_SOCK`.)

### Done Criteria

- [ ] `src/convo_recall/db.py` exists with all symbols listed above.
- [ ] `from convo_recall.ingest import open_db, close_db, DB_PATH` still works (re-export).
- [ ] `pytest tests/test_migration_project_id.py` → green.
- [ ] Cold-open of a v0.3.x DB applies no new migrations (`_record_migration` doesn't fire).
- [ ] `recall doctor` runs (touches `open_db`, `_init_schema`, project listing).
- [ ] `recall --version` runs (no import-time crashes).

### Verification Artifacts

| File | Must Contain | Why |
|------|-------------|-----|
| `src/convo_recall/db.py` | `def open_db(` and `def close_db(` | Connection lifecycle present |
| `src/convo_recall/db.py` | `def _migrate_project_id(` | v4 migration moved |
| `src/convo_recall/db.py` | `from .identity import` | Dep direction enforced (db → identity) |
| `src/convo_recall/ingest.py` | `from .db import` | Re-export block in place |
