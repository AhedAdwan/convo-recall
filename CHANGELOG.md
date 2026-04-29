# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

[Unreleased]: https://github.com/ahed-isir/convo-recall/commits/main
