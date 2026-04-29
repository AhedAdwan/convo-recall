# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
