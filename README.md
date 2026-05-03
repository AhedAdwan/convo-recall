# convo-recall

[![Tests](https://github.com/AhedAdwan/convo-recall/actions/workflows/test.yml/badge.svg)](https://github.com/AhedAdwan/convo-recall/actions/workflows/test.yml)

> **Searchable memory for your coding-agent conversations — across sessions, across projects, across Claude Code, Codex, and Gemini.**

Coding agents are stateless by design. convo-recall makes them stateful by infrastructure: every conversation lands in one local SQLite index, searchable by keyword and semantic meaning, and auto-fed back into the agent's context on every prompt.

```bash
recall search "how did we fix the auth middleware"
recall search "approaches we tried for the chunking problem" --recent
recall search "deployment config" --all-projects
recall search "the prompt that worked" --agent gemini
```

---

## Key features

- **One memory across every agent.** Claude Code, Codex, and Gemini sessions all land in the same SQLite DB. Claude can find what Codex did yesterday.
- **Verbatim, source-traceable.** Full transcripts indexed — not LLM summaries. Every hit links back to the originating session and timestamp.
- **Hybrid FTS + vector search.** SQLite FTS5 (porter stemming) fused with semantic recall (BAAI/bge-large-en-v1.5, 1024-dim, running locally on MPS/CPU) via Reciprocal Rank Fusion.
- **Cross-project recall.** Auto-scopes to the current repo by default; `--all-projects` for global search.
- **Zero-upkeep ingest.** Response-completion hooks index every turn within ~50 ms — no daemons to babysit.
- **Auto-context injection.** A pre-prompt hook runs `recall search` on every substantive prompt and feeds the top hits to the agent, so it actually knows what you've already worked on.
- **Local-only, secrets redacted.** Everything stays on your machine. OpenAI/Anthropic/GitHub/AWS/JWT/Slack token shapes are stripped on the way in. No cloud, no telemetry.

Runs on macOS or Linux. Python 3.11–3.14. Works with any subset of Claude Code, Codex, or Gemini CLI.

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
- **`conversation-ingest.sh`** (response-completion) — fires on Claude `Stop` / Codex `Stop` / Gemini `AfterAgent`, spawns `recall ingest` detached. Throttled to one ingest per 5 s via a lock file. Opt out with `CONVO_RECALL_INGEST_HOOK=off`.

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
| Claude Code | `Stop` | ✅ yes |
| Gemini CLI | `AfterAgent` | ✅ yes (default-on since v0.26.0) |
| Codex CLI | `Stop` | ⚠ session-end only — Codex hook system limitation |

**Codex caveats:** Codex hooks are experimental and gated behind `[features] codex_hooks = true` in `~/.codex/config.toml`. `recall install` writes the flag automatically when safely mergeable; skips with a warning when the file is invalid TOML or when running on Windows.

Scheduler-tier watchers (launchd / systemd `.path` units / cron) are no longer installed by default — the response-completion hook makes them redundant. The watcher install code remains in the codebase for users with bespoke flows; re-enable by uncommenting the `_ask` block in `_wizard.py`.

---

## Privacy

convo-recall ingests your conversation history verbatim into a local SQLite DB. To reduce the chance that secrets pasted into chats end up indexed, ingestion runs a **secret redaction** pass that replaces well-known credential token shapes with stable placeholders before they reach the FTS / vector index.

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

**Source-available, noncommercial, no government, no military.** Modified PolyForm Noncommercial 1.0.0 (SPDX `LicenseRef-PolyForm-Noncommercial-1.0.0-convo-recall`). Not OSI-open source. The base text is upstream [PolyForm Noncommercial 1.0.0](https://polyformproject.org/licenses/noncommercial/1.0.0); the licensor has removed government use and added an explicit prohibition on military / defense / weapons / intelligence / mass-surveillance use.

**You may** — for free, with no further permission:

- Read, fork, modify, and redistribute the source.
- Use it for personal projects, research, experimentation, education.
- Use it inside charitable orgs, educational institutions (incl. public schools / universities for teaching and academic research), public research / public health / environmental orgs.

**You may not** — under any circumstance:

- Use convo-recall in or with any commercial software, product, or service.
- Embed it as a dependency in software any company sells, hosts, or distributes commercially.
- Use it internally at a for-profit company for work-related purposes.
- Use it by, on behalf of, or for the benefit of any government institution at any level — except public schools / universities for teaching and academic research.
- Use it for any military / defense / weapons / intelligence / armed-conflict purpose — including reconnaissance, targeting, autonomous weapons, or training/deployment/evaluation thereof.
- Use it in mass-surveillance, social-credit, or biometric-identification systems operated against a general population.

For commercial licensing on civilian, non-military use cases, reach out. Full text in [LICENSE](LICENSE).

---

## Disclaimer

convo-recall is provided **as-is**, without warranty of any kind. The codebase ships with 360+ tests and several end-to-end sandbox runs, but your environment is not our environment.

Before installing on a workstation you care about: read [`SECURITY.md`](SECURITY.md), run a sandbox first (`tests/sandbox-*.sh` spin up disposable Docker environments that exercise install / search / uninstall), and do your own due diligence on the source (~3K lines of Python).

You run convo-recall at your own risk. See [`LICENSE`](LICENSE) § *No Liability* for binding terms.
