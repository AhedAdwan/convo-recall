# convo-recall

[![Tests](https://github.com/AhedAdwan/convo-recall/actions/workflows/test.yml/badge.svg)](https://github.com/AhedAdwan/convo-recall/actions/workflows/test.yml)

> **Searchable memory for your coding-agent conversations — across sessions, across projects, across Claude Code™, Codex™, and Gemini™.**

Coding agents are stateless by design. convo-recall makes them stateful by infrastructure: every conversation lands in one local SQLite index, searchable by keyword and semantic meaning, and auto-fed back into the agent's context on every prompt.

```bash
recall search "how did we fix the auth middleware"
recall search "approaches we tried for the chunking problem" --recent
recall search "deployment config" --all-projects
recall search "the prompt that worked" --agent gemini
```

---

## Key features

- **One memory across every agent.** Claude Code™, Codex™, and Gemini™ sessions all land in the same SQLite DB. Claude™ can find what Codex did yesterday.
- **Verbatim, source-traceable.** Full transcripts indexed — not LLM summaries. Every hit links back to the originating session and timestamp.
- **Hybrid FTS + vector search.** SQLite FTS5 (porter stemming) fused with semantic recall (BAAI/bge-large-en-v1.5, 1024-dim, running locally on MPS/CPU) via Reciprocal Rank Fusion.
- **Cross-project recall.** Auto-scopes to the current repo by default; `--all-projects` for global search.
- **Zero-upkeep ingest.** Response-completion hooks index every turn within ~50 ms — no daemons to babysit.
- **Auto-context injection.** A pre-prompt hook runs `recall search` on every substantive prompt and feeds the top hits to the agent, so it actually knows what you've already worked on.
- **Local-only, secrets redacted.** Everything stays on your machine. OpenAI/Anthropic/GitHub/AWS/JWT/Slack token shapes are stripped on the way in. No cloud, no telemetry.

Runs on macOS or Linux. Python 3.11–3.14. Works with any subset of Claude Code™, Codex™, or Gemini™ CLI.

---

## Install

Pick one path. Both install with hybrid FTS + vector search (the `[embeddings]` extra) and both end with `recall install` running the full interactive wizard.

**A — From the public Git repo (no clone):**

```bash
pipx install 'convo-recall[embeddings] @ git+https://github.com/AhedAdwan/convo-recall.git'
recall install
```

**B — From a local clone (use this if you want to edit the source):**

```bash
git clone https://github.com/AhedAdwan/convo-recall.git
cd convo-recall
pipx install -e '.[embeddings]'
recall install
```

`recall install` runs an interactive wizard that walks through every decision (response-completion ingest hooks, embed sidecar, pre-prompt search hooks, initial ingest) — each prompt prints what happens if you answer yes vs. no.

The wizard kicks off the initial ingest + embed-backfill in a **detached background process** so it returns control immediately. Watch progress with `recall stats` (one-shot tqdm bar at the top while the job is active) or `tail -f ~/Library/Logs/convo-recall-wizard-backfill.log`.

The embedding model (BAAI/bge-large-en-v1.5, ~1.3 GB) downloads on first use. Long texts are chunked with a 450-token sliding window (50-token overlap) and mean-pooled — no silent truncation at 512 tokens.

### Uninstall

```bash
recall uninstall              # removes hooks + sidecar (DB + config preserved)
pipx uninstall convo-recall   # removes the package itself
```

Order matters — `recall uninstall` first, otherwise the bundled hook script can't be located and entries get left dangling in each agent's settings file.

Full purge (also drops the indexed DB, logs, runtime cruft):

```bash
recall uninstall --purge-data
pipx uninstall convo-recall
```

Settings backups (`~/.claude/settings.json.bak.*`, etc.) and the BGE model cache (`~/.cache/huggingface/hub/models--BAAI--bge-large-en-v1.5/`) are always preserved — clean up manually if you want them gone.

---

## Schedulers

`recall install` picks one of four schedulers automatically — used to supervise the embed sidecar. Since v0.3.5, the response-completion ingest hook handles ingestion, so the scheduler tier matters mainly for keeping the embedding service warm.

| Scheduler | Detected when | Survives reboot | Notes |
|---|---|---|---|
| `launchd` | macOS | yes | `~/Library/LaunchAgents` plists. Default on Darwin. |
| `systemd` | Linux + `systemctl --user is-system-running` succeeds | yes (with linger) | User-mode `.service` units. Run `loginctl enable-linger $USER` to keep the sidecar alive after logout. |
| `cron` | Linux + `crontab` available, no usable systemd-user | yes | `@reboot` lines tagged `# convo-recall:*`. Tagged-line filtering on uninstall preserves your other crontab entries. |
| `polling` | always | NO | Last-resort fallback: `Popen` child. Dies at logout/reboot. |

`recall uninstall` walks every scheduler, so a host that switched OS gets clean teardown.

---

## Usage

```bash
# Search (auto-scopes to current project)
recall search "sqlite vector search"
recall search "chunking strategy" --recent        # bias toward recent conversations
recall search "embedding model" --all-projects    # search every project
recall search "the failing test" --agent codex    # filter to one agent
recall search "bug fix" -n 20

# Recent conversation tail
recall tail                       # last 30 messages of the latest session in this project
recall tail 50 --all-projects     # latest session across all projects

# Maintenance
recall ingest                     # manual ingest trigger
recall ingest --agent gemini      # one agent only
recall stats                      # DB statistics, per-agent counts
recall doctor                     # health check + per-agent hook state

# One-time backfills (run after upgrading)
recall embed-backfill             # embed any un-embedded messages
recall chunk-backfill             # re-embed long messages with chunked mean-pooling
recall backfill-clean             # re-clean content + rebuild FTS index
recall backfill-redact            # apply secret redaction to existing rows
recall tool-error-backfill        # index tool error blocks from existing sessions
```

---

## Project identity

Every project is identified by:

- **`project_id`** = `sha1(realpath(cwd))[:12]` — collision-free, deterministic, hyphen-vs-slash safe. Symlinked paths that resolve to the same target collapse to one id.
- **`display_name`** = basename of the nearest ancestor containing a project-root marker (`.git`, `package.json`, `Cargo.toml`, `pyproject.toml`, `go.mod`, …). Falls back to the basename of `realpath(cwd)`.

Search and tail accept `--project <display_name>` and resolve to the right `project_id` (exact match first, LIKE fallback with a multi-match warning). `recall forget --project X` requires an *exact* display_name match — no fallback.

**Cross-machine limitation:** project identity is path-based. The same repo at `~/work/repo` on machine A and `/srv/repo` on machine B has different `project_id`s. If you sync the DB across machines, the same logical project appears twice — search across both with `recall search foo --project repo` (display_name match).

---

## Hooks

convo-recall ships two shell hooks that auto-detect each CLI's payload shape and work across all three:

- **`conversation-memory.sh`** (pre-prompt) — runs `recall search "$prompt" -n 3 --json` on every substantive user turn (≥12 chars, not pure interjections like "yes" / "ok" / "hmm") and injects the top hits into the agent's context. Trivial prompts are no-ops. Opt out with `CONVO_RECALL_HOOK_AUTO_SEARCH=off`.
- **`conversation-ingest.sh`** (response-completion) — fires on Claude™ `Stop` / Codex™ `Stop` / Gemini™ `AfterAgent`, spawns `recall ingest` detached. Throttled to one ingest per 5 s via a lock file. Opt out with `CONVO_RECALL_INGEST_HOOK=off`.

`recall install` wires both into every detected CLI. To wire later or selectively:

```bash
recall install-hooks                       # both kinds, all detected CLIs
recall install-hooks --kind ingest         # ingest only
recall install-hooks --kind memory         # search only
recall install-hooks --agent claude        # one CLI
recall doctor                              # show per-agent wired/NOT-wired state
recall uninstall-hooks                     # remove only convo-recall blocks
```

Every operation backs up the original settings file with a `.bak.<timestamp>` suffix.

### Custom instructions

The pre-prompt hook also reads two optional files and prepends them to injected context (each capped at 2 KB):

- **Global**: `~/.config/convo-recall/instructions.md`
- **Per-project**: `.recall-instructions.md` in cwd

Order: global → per-project → prior-context → static reminder.

### Continuous ingest

Since v0.3.5, ingestion runs entirely off response-completion hooks. `conversation-ingest.sh` fires on each CLI's end-of-turn event:

| CLI | Event | Per-turn? |
|---|---|---|
| Claude Code™ | `Stop` | ✅ yes |
| Gemini™ CLI | `AfterAgent` | ✅ yes (default-on since v0.26.0) |
| Codex™ CLI | `Stop` | ⚠ session-end only — Codex hook system limitation |

**Codex™ caveats:** Codex hooks are experimental and gated behind `[features] codex_hooks = true` in `~/.codex/config.toml`. `recall install` writes the flag automatically when safely mergeable; skips with a warning when the file is invalid TOML or when running on Windows.

Scheduler-tier watchers (launchd / systemd `.path` units / cron) are no longer installed by default — the response-completion hook makes them redundant. The watcher install code remains in the codebase for users with bespoke flows; re-enable by uncommenting the `_ask` block in `_wizard.py`.

---

## Privacy

### What gets stored — and where

convo-recall reads your existing agent conversation logs and indexes them **verbatim** (full text, not LLM summaries) into a single local SQLite database. **Nothing leaves your machine** — no telemetry, no central server, no third-party API calls beyond the embedding model running locally on your CPU/MPS/GPU. Source files in `~/.claude/projects/`, `~/.codex/sessions/`, and `~/.gemini/tmp/` are read-only; convo-recall does not modify them.

| Path | Holds | Mode |
|---|---|---|
| `~/.local/share/convo-recall/conversations.db` | Full message corpus + FTS index + 1024-d embeddings (one vector per message) | `0600` |
| `~/.local/share/convo-recall/conversations.db-{wal,shm}` | SQLite WAL sidecars | `0600` |
| `~/.local/share/convo-recall/embed.sock` | Unix-domain socket for the embed sidecar (local-only IPC) | `0600` |
| `~/.local/share/convo-recall/` (parent dir) | All of the above | `0700` |
| `~/.claude/settings.json`, `~/.codex/hooks.json`, `~/.gemini/settings.json` | Hook entries written by `recall install`; removed by `recall uninstall` | unchanged |

### Inspecting + wiping

```bash
recall stats                       # row count, agent breakdown, embed coverage
recall doctor --scan-secrets       # count residual secret-shaped tokens in indexed text
recall forget --session <id>       # delete one session
recall forget --project <name>     # delete every row from a project (exact display_name)
recall forget --pattern '<regex>'  # delete rows whose content matches a Python regex
recall uninstall                   # remove hooks + sidecar (DB preserved)
rm -rf ~/.local/share/convo-recall # full data wipe after uninstall
```

### Secret redaction

To reduce the chance that secrets pasted into chats end up indexed, ingestion runs a **secret redaction** pass that replaces well-known credential token shapes with stable placeholders before they reach the FTS / vector index.

| Shape | Placeholder |
|---|---|
| OpenAI keys (`sk-…`) | `«REDACTED-OPENAI-KEY»` |
| Anthropic keys (`sk-ant-…`) | `«REDACTED-ANTHROPIC-KEY»` |
| GitHub tokens (`ghp_/gho_/ghs_/ghu_/ghr_`) | `«REDACTED-GITHUB-TOKEN»` |
| AWS access keys (`AKIA…`) | `«REDACTED-AWS-KEY»` |
| JWTs (`eyJ….….…`) | `«REDACTED-JWT»` |
| Slack tokens (`xoxb-/xoxp-/…`) | `«REDACTED-SLACK-TOKEN»` |

Redaction is on by default. Set `CONVO_RECALL_REDACT=off` to disable (e.g. for security-research workflows where you want raw content). For a DB that pre-dates redaction:

```bash
recall doctor --scan-secrets   # count what's already indexed
recall backfill-redact         # re-apply redaction + rebuild FTS
```

The DB and its WAL/SHM sidecars are chmod-0600; the parent directory is chmod-0700.

---

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `CONVO_RECALL_DB` | `~/.local/share/convo-recall/conversations.db` | SQLite database path |
| `CONVO_RECALL_PROJECTS` | `~/.claude/projects` | Claude Code™ projects directory |
| `CONVO_RECALL_GEMINI_TMP` | `~/.gemini/tmp` | Gemini™ CLI session root |
| `CONVO_RECALL_CODEX_SESSIONS` | `~/.codex/sessions` | Codex™ rollout root |
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

**Fair Source.** convo-recall is licensed under the [Functional Source License v1.1, Apache 2.0 Future License](https://fsl.software/) (SPDX: `FSL-1.1-Apache-2.0`) — the same Fair Source license used by Sentry, Codecov, Liquibase, GitButler, and Keygen. After two years, each released version converts automatically to the **Apache License, Version 2.0**.

In addition, all use is subject to the [convo-recall Acceptable Use Policy](ACCEPTABLE_USE.md), which is a perpetual ethical-use rider that survives the Apache 2.0 conversion.

### What you may do — for free, with no further permission

- Use convo-recall internally at any company, including a for-profit company — engineers can run it on their machines while building their own products.
- Modify, fork, redistribute the source under these same terms.
- Use it for personal projects, hobby work, non-commercial education, non-commercial research.
- Provide professional services to a licensee that is itself using convo-recall in compliance with these terms.
- After the two-year conversion, use it under Apache 2.0 (still subject to the AUP).

### What the FSL does not permit — needs a commercial license from the author

- A **Competing Use**: making convo-recall available to others as a commercial product or service that substitutes for, or offers substantially similar functionality to, convo-recall itself. Hosting convo-recall as a SaaS, reselling it as the product, or offering managed convo-recall is a Competing Use.

If your use case falls outside the FSL — e.g., you want to bundle convo-recall in a commercial product, host it as a managed service, or remove the AUP rider — **email the author** to discuss commercial licensing terms.

### What the AUP forbids — under all licenses, perpetually

Notwithstanding any license grant (including the post-conversion Apache 2.0), convo-recall may not be used in or for:

- Any government institution at any level (except public schools / universities for teaching and academic research).
- Any military, defense, weapons, intelligence, or armed-conflict purpose — including reconnaissance, targeting, autonomous-weapons systems, command-and-control, or any training, deployment, or evaluation thereof.
- Any mass-surveillance, social-credit, or biometric-identification system operated against a general population without specific informed consent.

Full FSL text in [`LICENSE`](LICENSE). AUP details in [`ACCEPTABLE_USE.md`](ACCEPTABLE_USE.md). Third-party dependency attributions in [`NOTICE`](NOTICE). Contributor terms in [`CLA.md`](CLA.md).

### Trademarks

**Claude™** and **Claude Code™** are trademarks of Anthropic, PBC. **Codex™** is a trademark of OpenAI OpCo, LLC. **Gemini™** is a trademark of Google LLC. All other trademarks are the property of their respective owners. **convo-recall is an independent open project** and is NOT affiliated with, endorsed by, sponsored by, or otherwise associated with Anthropic, OpenAI, or Google. References to these CLIs identify the third-party tools whose session files convo-recall reads — they do not imply endorsement.

---

## Disclaimer

convo-recall is provided **as-is**, without warranty of any kind. The codebase ships with 360+ tests and several end-to-end sandbox runs, but your environment is not our environment.

Before installing on a workstation you care about: read [`SECURITY.md`](SECURITY.md), run a sandbox first (`tests/sandbox-*.sh` spin up disposable Docker environments that exercise install / search / uninstall), and do your own due diligence on the source (~3K lines of Python).

You run convo-recall at your own risk. See [`LICENSE`](LICENSE) § *No Liability* for binding terms.
