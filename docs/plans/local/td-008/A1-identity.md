## A1: Extract `identity.py` — project_id / display_name / legacy slug helpers

**Status:** not started
**Dependencies:** none

### Scope

Move project-identity helpers out of `ingest.py` into a new `src/convo_recall/identity.py`. Pure file-move refactor — zero behavior change. All call sites in `ingest.py` are updated to `from .identity import …`. Legacy import path (`from convo_recall.ingest import _project_id`) keeps working via re-export at the bottom of `ingest.py`. Identity is the bottom of the dependency tree (nothing else in the package depends on it being in `ingest.py`), so this PR lands first and unblocks A2.

### Key Components

- `identity._project_id(cwd) -> str` — `sha1(realpath(cwd))[:12]`.
- `identity._display_name(cwd) -> str` — basename of nearest `_ROOT_MARKERS` ancestor.
- `identity._legacy_project_id(old_slug)` — used only by the v4 migration backfill path.
- `identity._legacy_claude_slug` / `_legacy_codex_slug` / `_legacy_gemini_slug` — per-agent slug derivation kept for v4 migration.
- `identity._gemini_hash_project_id(hash_dir)` — SHA-256-dir → project_id fallback.
- `identity._scan_claude_cwd` / `_scan_codex_cwd` / `_scan_gemini_cwd` — read cwd from session header (used by v4 migration backfill).
- `identity._ROOT_MARKERS` — `(".git", "pyproject.toml", "package.json", …)` tuple.

### Rough File Inventory

- New: 1 file (`src/convo_recall/identity.py`) — ~280 LOC moved.
- Modified: 1 file (`src/convo_recall/ingest.py`) — call sites updated to `from .identity import …`; bottom of file gets a re-export block for back-compat.
- Modified: 0 test files (tests reach identity helpers via `ingest.X`, which keeps working through the shim).

### Risks & Blockers

- **`_scan_*_cwd` helpers read JSONL files** — they touch `_iter_*_files` from ingest. Pull only the project-identity-relevant logic; if a `_scan_*_cwd` helper currently calls a JSONL parser, that parser stays in `ingest.py` and `identity` imports it (one-way) — but check the dependency direction first; if it's circular, defer the `_scan_*_cwd` helpers to A7.
- **No import cycles** — `identity.py` must not import anything from `convo_recall.ingest`. If migration backfill in `db.py` (A2) needs identity helpers, the dependency arrow goes `db → identity`, never the reverse.

### Done Criteria

- [ ] `src/convo_recall/identity.py` exists with all symbols listed above.
- [ ] `grep -nE '^def (_project_id|_display_name|_legacy_|_gemini_hash_project_id|_scan_)' src/convo_recall/ingest.py` returns nothing (all moved).
- [ ] `from convo_recall.ingest import _project_id, _display_name` still works (re-export).
- [ ] `pytest tests/` → all currently-passing tests stay green.
- [ ] `recall search foo --cwd $PWD` resolves the project correctly (identity is on the search hot path).

### Verification Artifacts

| File | Must Contain | Why |
|------|-------------|-----|
| `src/convo_recall/identity.py` | `def _project_id(` and `def _display_name(` | Core API present |
| `src/convo_recall/identity.py` | `_ROOT_MARKERS = (` | Constants moved |
| `src/convo_recall/ingest.py` | `from .identity import` | Re-export block in place |
