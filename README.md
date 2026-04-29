# convo-recall

Searchable memory for Claude Code sessions — FTS5 + optional vector hybrid search.

Every Claude Code conversation is indexed into a local SQLite database. Ask it anything you've discussed before: decisions, bugs, approaches, rejected ideas.

```
recall search "how did we fix the auth middleware"
recall search "approaches we tried for X" --recent
recall search "deployment config" --all-projects
```

## How it works

Claude Code writes session transcripts as `.jsonl` files under `~/.claude/projects/`. `convo-recall` watches that directory, parses the JSONL, cleans the content, and indexes it with:

- **FTS5** (SQLite full-text search with porter stemming) — keyword search, instant
- **Vector KNN** (optional) — semantic search via BAAI/bge-large-en-v1.5, fused with FTS via Reciprocal Rank Fusion

Without embeddings, search is FTS-only. With the `[embeddings]` extra and `recall serve`, search becomes hybrid — both keyword and semantic.

## Requirements

- macOS (launchd watcher; Linux support planned)
- Python 3.11+
- Claude Code

## Install

### FTS-only (fast, no GPU, no large download)

```bash
pipx install convo-recall
recall install
```

### Hybrid FTS + vector search

```bash
pipx install 'convo-recall[embeddings]'
recall install --with-embeddings
```

`--with-embeddings` also installs a second launchd job that keeps the embedding model warm in the background. The model (BAAI/bge-large-en-v1.5, ~1.3 GB) downloads on first use.

## Usage

```bash
# Search (auto-scopes to current project if inside a Claude Code project dir)
recall search "sqlite vector search"
recall search "chunking strategy" --recent        # bias toward recent conversations
recall search "embedding model" --all-projects    # search across all projects
recall search "bug fix" -n 20                     # more results
recall search "context window" -c 0               # no surrounding context turns

# Maintenance
recall ingest                   # manual ingest trigger
recall stats                    # DB statistics
recall serve                    # start embedding sidecar manually (if not using launchd)

# One-time backfills (run after upgrading)
recall embed-backfill           # embed any un-embedded messages
recall chunk-backfill           # re-embed long messages with chunked mean-pooling
recall backfill-clean           # re-clean content and rebuild FTS index
recall tool-error-backfill      # index tool error blocks from existing sessions
```

## Project slug

Search auto-scopes to your current project when you're inside a Claude Code project directory. The slug is derived from the path after your projects root, with slashes replaced by underscores:

```
~/Projects/apps/my-app  →  apps_my-app
```

Override with `--project <slug>` or disable with `--all-projects`.

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `CONVO_RECALL_DB` | `~/.local/share/convo-recall/conversations.db` | SQLite database path |
| `CONVO_RECALL_PROJECTS` | `~/.claude/projects` | Claude Code projects directory |
| `CONVO_RECALL_SOCK` | `~/.local/share/convo-recall/embed.sock` | Embedding sidecar socket path |

## Embedding sidecar protocol

The embedding sidecar exposes a simple HTTP-over-Unix-socket protocol. If you want to use your own embedding service, set `CONVO_RECALL_SOCK` to point at a socket that implements:

```
POST /embed
  Body:     {"text": "...", "mode": "query"|"document"}
  Response: {"vector": [...N floats...], "dim": N, "protocol": 1}

GET /healthz
  Response: {"model": "...", "dim": N, "device": "...", "protocol": 1}
```

The bundled sidecar uses 1024-dim vectors. If your service uses a different dimension, run `recall embed-backfill` after switching.

## License

MIT with Commons Clause — free to use and build products with. Redistribution as a commercial product is not permitted. See [LICENSE](LICENSE).
