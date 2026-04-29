# convo-recall

> **AI agents are stateless by design. convo-recall makes them stateful by infrastructure.**

Every coding-agent session starts blind. Decisions made last week, approaches that failed, the exact fix for that recurring bug — all of it vanishes when the context window closes. As sessions grow longer, the cost of keeping that context alive skews toward noise over signal. And when multiple agents work on the same project, each one starts from zero with no knowledge of what the others have done.

convo-recall fixes this. It indexes every conversation from your coding agents — **Claude Code, Gemini CLI, and Codex** — into one local SQLite database and makes it searchable by keyword, by semantic meaning, or both. Your agents get a shared memory that survives the context window AND crosses tool boundaries.

```bash
recall search "how did we fix the auth middleware"
recall search "approaches we tried for the chunking problem" --recent
recall search "deployment config" --all-projects
recall search "the prompt that worked" --agent gemini      # filter to one agent
```

---

## Why not Claude's built-in memory or compaction?

| | Claude compaction | Claude memory | convo-recall |
|---|---|---|---|
| Survives across sessions | — | ✅ | ✅ |
| Full verbatim transcript | — | — | ✅ |
| Semantic search | — | — | ✅ |
| Automatic, zero-setup | ✅ | ✅ | ✅ (launchd) |
| Cross-project recall | — | — | ✅ |
| Source-traceable | — | — | ✅ |

**Compaction** summarizes and discards — the detail is gone. Useful for staying within a context window, but it only knows the current session.

**Claude memory** is curated prose — an agent decides what's worth saving, which means it misses everything it didn't think to record. No semantic search, no source tracing.

**convo-recall** indexes everything automatically, keeps the full transcript, and lets you query it with natural language. The two approaches are complementary: use Claude memory for high-signal curated facts, convo-recall for full verbatim history on demand.

---

## What this enables

### For single-agent workflows

- **No more context amnesia** — start a new session and immediately retrieve what was decided, tried, and rejected in prior sessions.
- **Replace long context with precise retrieval** — instead of cramming weeks of history into the context window, ask for the 5 most relevant fragments. The model stays focused.
- **Reasoning is searchable, not just code** — git history stores what changed. convo-recall stores *why*. "Why did we switch from X to Y?" is here, not in the diff.

### For multi-agent workflows

- **Shared memory pool** — agents working on different sub-tasks of the same project index into the same DB. Each agent can retrieve what others have done without coordination overhead.
- **Dead ends as first-class data** — approaches that were tried and abandoned are indexed. The next agent doesn't re-explore the same dead end.
- **Subagent transparency** — parallel agents' full transcripts are indexed, not just their summaries. You can query what a sub-agent actually did.
- **Cross-project knowledge transfer** — a decision made in one project surfaces when you're solving the same class of problem in another.

---

## How it works

Each coding agent writes session transcripts as `.jsonl` files in its own location. convo-recall watches each directory via a per-agent launchd job, parses the JSONL, cleans the content, and indexes it with:

- **FTS5** — SQLite full-text search with porter stemming. Instant. No model required.
- **Vector KNN** (optional) — semantic search via BAAI/bge-large-en-v1.5 (1024-dim), running locally on MPS/CPU. Results fused with FTS via Reciprocal Rank Fusion.

New conversations are searchable within ~10 seconds of being written. The embedding sidecar stays warm in the background so hybrid search stays fast.

Without embeddings, search is FTS-only. With the `[embeddings]` extra and `recall serve`, search becomes hybrid — keyword and semantic together.

### Supported agents

| Agent  | Source dir                 | Pattern                                    |
|--------|----------------------------|--------------------------------------------|
| claude | `~/.claude/projects/`      | `*/*.jsonl`, `*/subagents/*.jsonl`         |
| gemini | `~/.gemini/tmp/`           | `*/chats/session-*.jsonl`                  |
| codex  | `~/.codex/sessions/`       | `{YYYY}/{MM}/{DD}/rollout-*.jsonl`         |

`recall install` auto-detects which agents are present on your machine and installs one launchd job per agent. The set of enabled agents is persisted in `~/.local/share/convo-recall/config.json` and can be edited directly.

For Codex sessions, the project slug is derived from the session's `cwd` so cross-agent search-by-project works the same way it does for Claude.

Tool calls and reasoning blocks are NOT indexed — only human-readable user/assistant text. Codex `~/.codex/history.jsonl` is intentionally skipped (rollout files are the source of truth).

---

## Requirements

- macOS (launchd watcher; Linux support planned)
- Python 3.11+
- Claude Code

---

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

`--with-embeddings` installs a second launchd job that keeps the embedding model warm in the background. The model (BAAI/bge-large-en-v1.5, ~1.3 GB) downloads on first use.

Long texts are chunked with a 450-token sliding window (50-token overlap) and mean-pooled — no silent truncation at 512 tokens.

---

## Usage

```bash
# Search (auto-scopes to current project if inside a Claude Code project dir)
recall search "sqlite vector search"
recall search "chunking strategy" --recent        # bias toward recent conversations
recall search "embedding model" --all-projects    # search across all projects
recall search "bug fix" -n 20                     # more results
recall search "the failing test" --agent codex    # filter to one agent

# Maintenance
recall ingest                   # manual ingest trigger (all enabled agents)
recall ingest --agent gemini    # ingest only one agent
recall watch                    # polling watcher for Linux/sandbox (no launchd)
recall stats                    # DB statistics (with per-agent counts)
recall serve                    # start embedding sidecar manually (if not using launchd)

# One-time backfills (run after upgrading)
recall embed-backfill           # embed any un-embedded messages
recall chunk-backfill           # re-embed long messages with chunked mean-pooling
recall backfill-clean           # re-clean content and rebuild FTS index
recall tool-error-backfill      # index tool error blocks from existing sessions
```

---

## Project scope

Search auto-scopes to your current project when you're inside a Claude Code project directory. The slug is derived from the path after your projects root, with slashes replaced by underscores:

```
~/Projects/apps/my-app  →  apps_my-app
```

Override with `--project <slug>` or search everything with `--all-projects`.

---

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `CONVO_RECALL_DB` | `~/.local/share/convo-recall/conversations.db` | SQLite database path |
| `CONVO_RECALL_PROJECTS` | `~/.claude/projects` | Claude Code projects directory |
| `CONVO_RECALL_GEMINI_TMP` | `~/.gemini/tmp` | Gemini CLI session root |
| `CONVO_RECALL_CODEX_SESSIONS` | `~/.codex/sessions` | Codex rollout root |
| `CONVO_RECALL_CONFIG` | `~/.local/share/convo-recall/config.json` | Enabled-agents config file |
| `CONVO_RECALL_SOCK` | `~/.local/share/convo-recall/embed.sock` | Embedding sidecar socket path |

---

## Embedding sidecar protocol

The sidecar exposes HTTP over a Unix socket. Bring your own embedding service by pointing `CONVO_RECALL_SOCK` at a socket that implements:

```
POST /embed
  Body:     {"text": "...", "mode": "query"|"document"}
  Response: {"vector": [...N floats...], "dim": N, "protocol": 1}

GET /healthz
  Response: {"model": "...", "dim": N, "device": "...", "protocol": 1}
```

The bundled sidecar uses 1024-dim vectors. If your service uses a different dimension, run `recall embed-backfill` after switching.

---

## License

MIT with Commons Clause — free to use and build products with. Redistribution as a commercial product is not permitted. See [LICENSE](LICENSE).
