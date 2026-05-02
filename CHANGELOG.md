# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- **Response-completion ingest hooks** — convo-recall now installs a sibling hook (`conversation-ingest.sh`) on each agent CLI's response-end event (`Stop` for Claude/Codex, `AfterAgent` for Gemini). When your agent finishes a turn, ingest fires within ~50ms (background-detached, lock-file dedup'd to a 5-second window). Closes the long-standing Linux gap where systemd `.path` units silently missed appends inside existing project subdirs (`PathChanged=` is non-recursive — see TD-003).
- `recall install-hooks --kind {memory,ingest,both}` flag (default: `both`); same flag on `recall uninstall-hooks`.
- New wizard step (Step 2/5) asks whether to install ingest hooks. Default Y.
- `recall doctor` now reports per-agent ingest-hook installation state with a one-line repair hint.
- `CONVO_RECALL_INGEST_HOOK=off` env var to opt out without uninstalling.

### Changed
- `recall uninstall-hooks` (and the hook-cleanup pass inside `recall uninstall`) now walks BOTH the search hook and the new ingest hook by default.
- Codex install path writes `[features] codex_hooks = true` to `~/.codex/config.toml` if missing or safely mergeable. Skipped with warning on invalid TOML.

### Notes for upgraders
- **Existing installs don't get the new hook automatically** — re-run `recall install` or `recall install-hooks --kind ingest` to wire it. `recall doctor` shows which agents are missing the hook.
- The ingest hook is **additive** to existing schedulers (systemd `.path`, cron, polling) — they all stay installed in this release. A future release will demote systemd `.path` to opt-in.
- **Codex `Stop` fires at session-end**, not per-turn (Codex hook system limitation). Most Codex sessions are short, so this is acceptable.
- Background ingest cost: ~50ms hot DB; up to 1–3s cold. Detached + backgrounded — user-perceived latency is the spawn cost (~30ms).

### Added
- **Stable `project_id`** — every project gets a `sha1(realpath(cwd))[:12]` id and a separate human-readable `display_name`, normalized in a new `projects` table. Replaces the four divergent slug-derivation paths (`slug_from_cwd` / `_slug_from_cwd` / `_slug_from_path` / `_gemini_slug_from_path`) and the hardcoded `/Projects/` substring check that broke on Linux paths.
- `recall search --cwd PATH` and `recall tail --cwd PATH` — explicit cwd override for hooks and other callers that can't rely on `os.getcwd()`.

### Changed
- `recall forget --project X` now requires an **exact** `display_name` match (was: substring). Search and tail accept exact-first with a LIKE fallback that warns on multi-match.
- `recall search --json` and `recall tail --json` output now includes `project_id` and `display_name`. The legacy `project_slug` field remains as a deprecated alias (= display_name) for one release.
- The pre-prompt hook (`conversation-memory.sh`) no longer hard-codes `/Projects/` in path parsing; it passes `--cwd "$cwd"` to `recall search` and lets recall resolve the project.
- `recall doctor` adds an orphan-message integrity check + a `Projects` count line.

### Removed
- Helpers `slug_from_cwd`, `_slug_from_cwd`, `_slug_from_path`, `_gemini_slug_from_path`, and the `_codex_slug_from_cwd` alias. Use `_project_id(cwd)` and `_display_name(cwd)` instead. Migration-internal renamed equivalents (`_legacy_claude_slug`, `_legacy_codex_slug`, `_legacy_gemini_slug`) remain for the v4 backfill path only.
- The `/Projects/` substring hardcode in `conversation-memory.sh`.

### Notes for upgraders
- Migration `_MIGRATION_PROJECT_ID = 4` runs on first open after upgrade. Snapshot saved to `<db>.pre-project-id.<ts>.bak` next to the DB. FTS index is rebuilt — expect 10–30s pause on a 60K-row DB. After verifying, delete the `.bak` manually or wait for `recall doctor` to flag it (>30 days).
- **Cross-machine project identity: out of scope.** A repo at `/a/repo` on machine A and `/b/repo` on machine B will appear as two projects (same display_name, different project_id) if you sync DBs across machines. Workaround: `recall search foo --project repo` matches by display_name across both.
- The JSON `project_slug` field is **deprecated** and will be removed in the release after this one. Migrate consumers to `display_name`.

### Added
- **Cross-platform install.** `recall install` now works on Linux as well as macOS. The `_require_macos()` gate is gone.
- **Scheduler abstraction** with four implementations: `LaunchdScheduler` (macOS), `SystemdUserScheduler` (Linux native, file-event driven `.service` + `.path` units), `CronScheduler` (Linux fallback, `@reboot` lines tagged `# convo-recall:*`), `PollingScheduler` (universal `Popen` fallback).
- `--scheduler {auto,launchd,systemd,cron,polling}` flag to override auto-detection.
- `recall uninstall` walks every scheduler so a host that switched OS gets clean teardown across tiers.
- pexpect-driven wizard tests covering full-yes, decline-watchers, decline-hooks, and abort-at-final-confirm flows; `tests/sandbox-linux-port-e2e.sh` exercises real polling/systemd/cron lifecycles in a Linux sandbox.
- CI matrix: tests run on `macos-latest` AND `ubuntu-latest`, both Python 3.11 and 3.12.
- **Secret redaction during ingest.** `_clean_content` now replaces well-known credential token shapes (OpenAI, Anthropic, GitHub, AWS, JWT, Slack) with `«REDACTED-…»` placeholders before content reaches the FTS / vector index. Always-on; opt out with `CONVO_RECALL_REDACT=off`.
- `recall doctor` — DB health checks. With `--scan-secrets`, counts how many existing rows match each redaction pattern (so users discover what already leaked into their DB pre-redaction). Also surfaces stray `*.bak` files older than 30 days in the DB directory.
- `recall backfill-redact` — re-applies secret redaction to all existing rows + rebuilds FTS. Use after upgrading from a pre-redaction version.
- `recall forget` — scoped deletion API with mutually-exclusive scope flags (`--session`, `--pattern`, `--before`, `--project`, `--agent`, `--uuid`). Dry-run by default; pass `--confirm` to actually delete. Cleans up `messages`, FTS, `message_vecs`, and prunes empty `sessions` / `ingested_files` rows.
- Privacy section in README documenting redaction patterns, opt-out env var, and `recall forget`. New Schedulers section documenting the auto-detection ladder. CI status badge.

### Changed
- **Wizard's Step 1 prompt adapts to the chosen scheduler** via `consequence_yes/no()` and `describe()`. Same code path on every platform — different consequence text per tier.
- **Filter-aware retrieval** in `search()` — when `--project` or `--agent` filter is a small fraction of the corpus, the FTS query receives `rowid IN (filter_set)` directly and vec search becomes brute-force exact (Python-side cosine over the filtered subset). Fixes the recall cliff where `--agent codex foo` returned 0 hits against a corpus dominated by another agent. No regression at high cardinality (>5000 rows) — existing top-100 prefilter path is preserved.
- **Self-heal walks newest-first** (`ORDER BY m.rowid DESC`) and the cap is bumped from 500 → 2000 per pass. Most recent (and most-queried) messages heal first after a fresh install against a backup-imported DB.
- `recall install --with-embeddings` now runs `embed-backfill` once after the initial ingest. Catches the "fresh-install on existing 60K-row DB" case in one sweep instead of multi-hour self-heal cycles.
- **Per-connection vec state.** `_VEC_ENABLED` is a `WeakKeyDictionary` keyed by apsw connection rather than a module-level `_vc` flag, so multiple `open_db()` calls in one process (test harnesses, in-memory bench tools) don't clobber each other.
- **Gemini slug resolution** is now three-layer: header `cwd` first (matches Claude/Codex `Projects/X` convention), then `~/.local/share/convo-recall/gemini-aliases.json` map, then the SHA-hash dir name as last resort.
- `pyproject.toml` adds `Operating System :: POSIX :: Linux` classifier and broadens `Operating System :: MacOS :: MacOS X` → `Operating System :: MacOS`. `pexpect` and `pyyaml` added to `[dev]` extras.

### Internal
- `install.py` (700 lines, launchd-only) extracted into `install/` package: `_paths.py` (XDG-aware path resolution), `_hooks.py` (pre-prompt hook wiring), `_wizard.py` (interactive setup), `schedulers/{base,launchd,systemd,cron,polling}.py`. Public API (`run`, `uninstall`, `install_hooks`, `uninstall_hooks`) unchanged — `cli.py` keeps importing from `convo_recall.install` exactly as before.

### Notes for upgraders
- The v0.2.0 in-place migration creates `<db>.pre-v020.<ts>.bak` next to the DB. After verifying your DB is healthy, you can delete it manually — or run `recall doctor` to surface stale `.bak` files older than 30 days.

## [0.2.0] — 2026-04-30

### Added
- **Multi-agent ingestion.** Index sessions from Claude (`~/.claude/projects/`), Gemini (`~/.gemini/tmp/`), and Codex (`~/.codex/sessions/`) into a single hybrid index.
- `agent` column on `messages`, `sessions`, and `ingested_files`. Existing single-Claude DBs are migrated in place — every existing row gets `agent='claude'`, FTS rebuilds with the new column, and stored vectors are preserved.
- `detect_agents()` — returns presence + file count for each supported agent.
- `~/.local/share/convo-recall/config.json` — persists which agents are enabled (default: claude only).
- `recall ingest --agent {claude|gemini|codex}` — scan one agent.
- `recall search --agent {name}` — exclusive filter on search results.
- `recall watch` — polling watcher loop for Linux / sandbox use (no launchd).
- `recall install` now generates one launchd plist per enabled agent (`com.convo-recall.ingest.{agent}.plist`), each watching its own source dir.
- New parsers: `ingest_gemini_file`, `ingest_codex_file`. Codex `cwd`-derived project slugs align with Claude's `Projects/X` convention so cross-agent search-by-project works.
- `By agent` section in `recall stats`.

### Changed
- **Single apsw connection** for FTS, vec, and message ops. Replaces the previous dual `sqlite3` (stdlib) + `apsw` design that corrupted vec0 shadow tables when stdlib sqlite was several minor versions behind apsw's bundled libsqlite3 (e.g. Ubuntu 24.04's 3.45.1 vs apsw's 3.53.0). Cross-platform; eliminates the libsqlite3 version coupling.
- Search result lines now show `[{agent}]` tag in addition to `[{project_slug}]` and role.

### Removed
- `_open_vec_con` helper (folded into `open_db`).
- Legacy single `com.convo-recall.ingest.plist` no longer generated. `recall uninstall` still cleans it up if present from a 0.1.x install.

## [0.1.0]

### Added
- FTS5 full-text search with porter stemming and camelCase/snake_case token splitting
- Optional hybrid vector search via BAAI/bge-large-en-v1.5 (1024-dim, RRF fusion)
- `recall install` — one-shot launchd setup for macOS file watcher and optional embedding sidecar
- `recall uninstall [--purge-data]` — clean removal of launchd agents
- `recall serve` — embedding sidecar (HTTP over Unix domain socket)
- `recall search` — hybrid search with `--recent`, `--project`, `--all-projects`, `--context`
- `recall ingest` — manual ingest trigger
- `recall stats` — database statistics
- Backfill commands: `embed-backfill`, `chunk-backfill`, `backfill-clean`, `tool-error-backfill`
- Long-message chunking (1600-char chunks, 200-char overlap) with mean-pool embedding
- Tool result error indexing (`role=tool_error`)
- Corpus coverage guard: falls back to FTS-only when vector coverage < 95%
- Custom embedding sidecar support via `CONVO_RECALL_SOCK`

[Unreleased]: https://github.com/ahed-isir/convo-recall/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/ahed-isir/convo-recall/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/ahed-isir/convo-recall/releases/tag/v0.1.0
