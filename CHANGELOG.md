# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
