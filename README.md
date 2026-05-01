# convo-recall

[![Tests](https://github.com/AhedAdwan/convo-recall/actions/workflows/test.yml/badge.svg)](https://github.com/AhedAdwan/convo-recall/actions/workflows/test.yml)

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

## Why not the agent's built-in memory or compaction?

Most coding agents ship with two related-but-distinct features for surviving long conversations: **in-session compaction** and a **curated memory layer**. Names vary — Claude has *compaction* + *memory*, Codex has *resumable sessions*, Gemini has `/chat save` + the *Memory MCP* — but the shape is similar everywhere.

| | In-session compaction | Curated memory layer | convo-recall |
|---|---|---|---|
| Survives across sessions | — | ✅ | ✅ |
| Full verbatim transcript | — | — | ✅ |
| Semantic search | — | — | ✅ |
| Automatic, zero-setup | ✅ | ✅ | ✅ (launchd / systemd / cron) |
| Cross-project recall | — | — | ✅ |
| Cross-agent recall (Claude ↔ Codex ↔ Gemini) | — | — | ✅ |
| Source-traceable | — | — | ✅ |

**Compaction** summarizes and discards — the detail is gone. Useful for staying within a context window, but it only knows the current session.

**Curated memory layers** are agent-written prose — the model decides what's worth saving, which means everything it didn't think to record is permanently lost. No semantic search, no source tracing. Each agent's memory layer is also walled off from the others: Claude's memory file can't see what Codex did yesterday.

**convo-recall** indexes everything automatically across every supported agent, keeps the full transcript, and lets you query it with natural language. The two approaches are complementary: use the agent's own memory for high-signal curated facts (intent, preferences, ongoing goals), use convo-recall for full verbatim history on demand — including across agents.

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

- macOS or Linux. Python **3.11, 3.12, 3.13, or 3.14** — CI tests every version on both OSes; the lower bound is 3.11.
  - macOS: launchd watcher (default).
  - Linux: systemd-user, cron, or polling fallback — auto-detected. See [Schedulers](#schedulers).
- Claude Code, Codex, or Gemini CLI (any subset; hooks work across all three).

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

`--with-embeddings` keeps the embedding model warm in the background (launchd job on macOS, systemd-user `.service` on Linux, fallback to a Popen child elsewhere). The model (BAAI/bge-large-en-v1.5, ~1.3 GB) downloads on first use.

Long texts are chunked with a 450-token sliding window (50-token overlap) and mean-pooled — no silent truncation at 512 tokens.

### Verbose / audit install

The plain install is terse. For first-time installs or security audits where you want to see every wheel + URL + hash:

```bash
pipx install 'convo-recall[embeddings]' --verbose                  # show pipx's own steps
pipx install 'convo-recall[embeddings]' --verbose --pip-args="-v"  # also pass -v to pip
```

⚠ pipx pipes pip's stdout through `subprocess.PIPE`, which strips pip's TTY-aware progress bars. Even with `--pip-args="-v"` the heaviest step (`torch` ~750 MB download) prints once at the start and then appears to "hang" until done. **For real progress bars**, install the core first and then add deps directly:

```bash
pipx install convo-recall                                          # fast core, no embeddings
pipx runpip convo-recall install \
    sentence-transformers 'torch>=2.2,<3' 'aiohttp>=3.10.11'       # tqdm progress bars
```

`pipx runpip` execs pip with stdout attached to your real terminal — same end-state as `pipx install '.[embeddings]'`, but you get live download bars per wheel.

To audit what landed afterward:

```bash
pipx runpip convo-recall list             # all packages + versions
pipx environment                          # pipx's directory layout
pipx runpip convo-recall show torch       # provenance of any one package
```

### Uninstall

Two-step chain. Order matters — run `recall uninstall` **before** `pipx uninstall`, otherwise the bundled hook script can't be located and entries get left dangling in each agent's settings file:

```bash
recall uninstall              # removes hooks + watchers + sidecar (DB + config preserved)
pipx uninstall convo-recall   # removes the package itself
```

Full purge (also deletes the indexed conversation DB, logs, runtime cruft):

```bash
recall uninstall --purge-data
pipx uninstall convo-recall
```

`--purge-data` removes `~/.local/share/convo-recall/` (DB + config), `~/Library/Caches/convo-recall/` on macOS or `$XDG_RUNTIME_DIR/convo-recall/` on Linux (sockets + cron backups), and `convo-recall-*.log` files in your platform log dir.

**Always preserved** (clean up manually if desired):

- Settings backups: `~/.claude/settings.json.bak.*`, `~/.codex/hooks.json.bak.*`, `~/.gemini/settings.json.bak.*` — kept as a recovery safety net.
- Embedding model cache: `~/.cache/huggingface/hub/models--BAAI--bge-large-en-v1.5/` — shared across tools (delete only if no other tool uses BGE).

---

## Schedulers

`recall install` picks one of four schedulers automatically. Override with `--scheduler X`:

| Scheduler | Detected when | Survives reboot | Notes |
|---|---|---|---|
| `launchd` | macOS | yes | Uses `~/Library/LaunchAgents` plists. Default on Darwin. |
| `systemd` | Linux + `systemctl --user is-system-running` succeeds | yes (with linger) | `.service` + `.path` units, file-event driven. Run `loginctl enable-linger $USER` if you want watchers to survive logout — the wizard offers to do this for you. |
| `cron` | Linux + `crontab` available, no usable systemd-user | yes | `@reboot` lines tagged `# convo-recall:*`. Tagged-line filtering on uninstall preserves your other crontab entries. |
| `polling` | always | NO | Last-resort fallback: `recall watch` runs as a `Popen` child. Dies at logout/reboot — re-run `recall install` after restart. |

The wizard prints `Selected scheduler: <X>` so you always know which tier you're on. To force a specific tier (e.g. for CI containers where systemd-user reports `running` but doesn't actually work):

```bash
recall install --scheduler polling -y     # universal Popen fallback
recall install --scheduler systemd -y     # explicit systemd-user, Linux
recall install --scheduler launchd -y     # explicit launchd, macOS
```

`recall uninstall` walks every scheduler so a host that switched OS gets clean teardown.

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
recall backfill-redact          # apply secret redaction to existing rows
recall tool-error-backfill      # index tool error blocks from existing sessions

# Health checks
recall doctor                   # general DB health
recall doctor --scan-secrets    # count credential-shaped tokens in indexed content
```

---

## Project scope

Search auto-scopes to your current project when you're inside a Claude Code project directory. The slug is derived from the path after your projects root, with both slashes AND hyphens collapsed to underscores:

```
~/Projects/apps/my-app  →  apps_my_app
~/Projects/libs/convo-recall  →  libs_convo_recall
```

Hyphen collapsing is required because Claude's session storage flattens path separators using hyphens, so distinct hyphens in original names become indistinguishable from path separators at ingest time. Both `app-claude` and `app_claude` map to the same slug `app_claude`.

Override with `--project <slug>` or search everything with `--all-projects`. If a search returns zero results for a project, `recall` prints a `Did you mean: <slug>?` hint when a near-miss slug exists in the DB.

**Search snippet highlighting:** matched query tokens in result snippets are wrapped in `[brackets]` (SQLite FTS5's `snippet()` highlighter). For example, searching `"claude codex"` will return snippets with `[claude]` and `[codex]` bracketed. Tokens that aren't in your query are unbracketed — this isn't redaction or asymmetry, it's just where the match landed.

---

## Pre-prompt hooks (Claude / Codex / Gemini)

convo-recall ships a single shell hook that auto-runs `recall search` against your prompt and injects the top hits as context on every substantive user turn. Same script works in all three CLIs — it auto-detects the event from the JSON payload each CLI sends on stdin and echoes back the right `hookEventName` so each accepts the response.

The script is at `src/convo_recall/hooks/conversation-memory.sh`. Without these hooks wired, your AI agents won't know convo-recall exists and will keep guessing/web-searching despite the indexed history sitting right there.

**Behavior:**
- Substantive prompts (≥12 chars, not pure interjections like "yes" / "ok" / "hmm"): hook runs `recall search "$prompt" -n 3 --json` and prepends top hits to context.
- Trivial prompts: hook returns empty context, zero token bloat.
- Opt out entirely: set `CONVO_RECALL_HOOK_AUTO_SEARCH=off` in your env.

### Custom instructions

The hook also reads two optional files and prepends their content to the injected context — useful for per-machine and per-repo guidance:

- **Global**: `~/.config/convo-recall/instructions.md` (or `$XDG_CONFIG_HOME/convo-recall/instructions.md`)
- **Per-project**: `.recall-instructions.md` in the cwd

Each is capped at 2 KB. Both are optional. If both exist, global content goes first, then per-project, then prior-context, then the static reminder.

### Quickest path

```bash
recall install-hooks            # interactive: confirms each detected CLI before wiring
recall install-hooks -y         # non-interactive: wires every detected CLI
recall install-hooks --dry-run  # shows what would change without writing
recall install-hooks --agent claude --agent codex   # subset
recall uninstall-hooks          # removes only the convo-recall block; user's other hooks stay
```

`recall install` runs this as one stage of the full wizard and asks before modifying any settings file. Every operation backs up the original file with a `.bak.<timestamp>` suffix.

### Wire it up manually

**Claude Code** (`~/.claude/settings.json`):
```json
{
  "hooks": {
    "UserPromptSubmit": [
      {
        "hooks": [
          {"type": "command", "command": "/path/to/conversation-memory.sh", "timeout": 5}
        ]
      }
    ]
  }
}
```

**Codex CLI** (`~/.codex/hooks.json`):
```json
{
  "hooks": {
    "UserPromptSubmit": [
      {
        "hooks": [
          {"type": "command", "command": "/path/to/conversation-memory.sh", "timeout": 5}
        ]
      }
    ]
  }
}
```

**Gemini CLI** (`~/.gemini/settings.json`):
```json
{
  "hooks": {
    "BeforeAgent": [
      {
        "matcher": "*",
        "hooks": [
          {"name": "convo-recall", "type": "command", "command": "/path/to/conversation-memory.sh", "timeout": 5000}
        ]
      }
    ]
  }
}
```

Notes:
- Gemini's `timeout` is in **milliseconds** (default 60000). Claude/Codex use **seconds**. Easy to get wrong.
- Set `CONVO_RECALL_HOOK_LOG=/some/path.log` to log every hook firing for debugging.
- Tested e2e in the claude-sandbox container: see `tests/sandbox-hooks-e2e.sh`. The test wires the hook into all three CLIs, runs each headless (`claude -p`, `codex exec`, `gemini -p --yolo --skip-trust`), and verifies the model actually receives the hint by asking it to echo the word "convo-recall" back.

---

## Privacy

convo-recall ingests your conversation history verbatim into a local SQLite DB. To reduce the chance that secrets pasted into chats end up indexed and searchable, ingestion runs a **secret redaction** pass that replaces well-known credential token shapes with stable placeholders before they reach the FTS / vector index.

Patterns redacted by default:

| Shape | Placeholder |
|---|---|
| OpenAI keys (`sk-…`) | `«REDACTED-OPENAI-KEY»` |
| Anthropic keys (`sk-ant-…`) | `«REDACTED-ANTHROPIC-KEY»` |
| GitHub tokens (`ghp_/gho_/ghs_/ghu_/ghr_`) | `«REDACTED-GITHUB-TOKEN»` |
| AWS access keys (`AKIA…`) | `«REDACTED-AWS-KEY»` |
| JWTs (`eyJ….….…`) | `«REDACTED-JWT»` |
| Slack tokens (`xoxb-/xoxp-/…`) | `«REDACTED-SLACK-TOKEN»` |

Redaction is **on by default**. Set `CONVO_RECALL_REDACT=off` to disable it (e.g. for security-research workflows where you want to grep across raw content).

Helpers for an existing DB that pre-dates redaction:

```bash
recall doctor --scan-secrets   # count what's already indexed
recall backfill-redact         # re-apply redaction to existing rows + rebuild FTS
```

The DB and its WAL/SHM sidecars are chmod-0600 (owner-only) on a multi-user system. The parent directory is chmod-0700.

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
| `CONVO_RECALL_REDACT` | _on_ | Set to `off` to disable secret redaction during ingest |

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

**Source-available, noncommercial, no government, no military.** convo-recall is licensed under a modified PolyForm Noncommercial 1.0.0 (SPDX: `LicenseRef-PolyForm-Noncommercial-1.0.0-convo-recall`). It is not OSI-open source. The base license text is the upstream [PolyForm Noncommercial 1.0.0](https://polyformproject.org/licenses/noncommercial/1.0.0); the licensor has removed government use and added an explicit prohibition on military/defense/weapons/intelligence/mass-surveillance use.

**You may** — for free, with no further permission:
- Read, fork, modify, and redistribute the source.
- Use it for personal projects, hobby work, research, experimentation, education, and personal study.
- Use it inside charitable organizations, educational institutions (including public schools and universities for teaching and academic research), public research organizations, public safety / public health organizations, and environmental protection organizations.

**You may not** — under any circumstance:
- Use convo-recall in or with any commercial software, product, or service.
- Embed convo-recall as a dependency in software that any company sells, hosts, or distributes commercially.
- Use convo-recall internally at a for-profit company for work-related purposes.
- Use convo-recall by, on behalf of, or for the benefit of any government institution at any level (national, state, provincial, regional, county, municipal, or quasi-public) — except for public schools/universities used solely for teaching and academic research.
- Use convo-recall for any military, defense, weapons system, intelligence service, or armed-conflict purpose — including reconnaissance, targeting, autonomous-weapons, command-and-control, or any training/deployment/evaluation thereof.
- Use convo-recall in any mass-surveillance, social-credit, or biometric-identification system operated against a general population.

If you want to use convo-recall commercially, reach out for a commercial license — open to discussion for genuinely civilian, non-military use cases. See [LICENSE](LICENSE) for the full text.
