## A6: Extract `admin.py` — stats, doctor, forget

**Status:** not started
**Dependencies:** A2, A4

### Scope

Move administrative / observability commands out of `ingest.py` into `src/convo_recall/admin.py`. These are CLI-only entry points (`recall stats`, `recall doctor`, `recall forget`) plus their formatting helpers. Pure file-move; zero behavior change.

### Key Components

- `admin.stats(con)` — print DB stats: row counts, embedding coverage, agent breakdown, top projects, sidecar status, hook status.
- `admin._render_phase_bar(phase)` — sidecar-init progress phase line.
- `admin._render_progress_bar(status)` — embedding-progress bar used by stats.
- `admin.doctor(con, scan_secrets=False)` — DB integrity / hook installation / orphan messages / stale `.bak` files.
- `admin._scan_stale_bak_files(db_dir)` — list `.bak` files older than 30 days.
- `admin.forget(con, *, session=None, pattern=None, before=None, project=None, agent=None, uuid=None, confirm=False)` — scoped deletion API with mutually-exclusive scope flags.

### Rough File Inventory

- New: 1 file (`src/convo_recall/admin.py`) — ~700 LOC moved.
- Modified: 1 file (`src/convo_recall/ingest.py`) — call sites and re-exports.
- Modified: 0 test files (`tests/test_safety_cli.py`, etc. reach via shim).

### Risks & Blockers

- **`stats()` reaches into many places** — embedding count via `_vec_count` (from `embed.py`, A3), project listing via `_upsert_project` / `projects` table (from `db.py`, A2), agent detection via `detect_agents` (will land in `ingest/scan.py` in A7). Same temporary-import pattern as A5: `admin.py` imports `detect_agents` from `convo_recall.ingest` until A7 moves it. Document at the top of `admin.py`.
- **`doctor()` checks hook installation** — calls into `convo_recall.install._hooks`. That's already its own module; keep the import as-is.
- **`forget()` rebuilds FTS** — `INSERT INTO messages_fts(messages_fts) VALUES('rebuild')`. No new coupling.
- **Output stability test:** before A6, capture `recall stats` and `recall doctor` output to a fixture. After A6, run again and `diff`. Must be byte-identical except for any timestamps / row counts that drift in normal operation (i.e., compare in a quiesced state — sidecar idle, no in-flight ingest).

### Done Criteria

- [ ] `src/convo_recall/admin.py` exists with all symbols listed above.
- [ ] `from convo_recall.ingest import stats, doctor, forget` still works (re-export).
- [ ] `pytest tests/test_safety_cli.py tests/test_uninstall_walks_all_tiers.py` → green.
- [ ] `recall stats` and `recall doctor` produce output identical to v0.3.6 baseline (byte-diff in quiesced state).
- [ ] `recall forget --pattern 'test' --dry-run` produces identical match-count output.

### Verification Artifacts

| File | Must Contain | Why |
|------|-------------|-----|
| `src/convo_recall/admin.py` | `def stats(` and `def doctor(` and `def forget(` | Public entry points present |
| `src/convo_recall/admin.py` | `def _render_progress_bar(` | Internal formatter moved |
| `src/convo_recall/ingest.py` | `from .admin import` | Re-export block in place |
