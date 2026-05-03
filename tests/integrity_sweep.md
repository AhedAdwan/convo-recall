# Full integrity sweep — convo-recall

This is the consolidated battery I've been running across the upgrades (0.2.0 → 0.3.1 → 0.3.4 → 0.3.5). Below is the canonical version: every probe, what it tests, what a healthy result looks like, and a candid answer to whether this constitutes a "use-case test suite."

The runnable counterpart lives at `tests/integrity_sweep.py`.

---

## A. Install & version sanity

| #   | Probe                                                             | What it asserts                                                                       |
|-----|-------------------------------------------------------------------|---------------------------------------------------------------------------------------|
| 1   | `recall --version`                                                | Binary on PATH, reports a version                                                     |
| 2   | `pipx list \| grep convo-recall`                                  | Package source (git URL), install method, declared version                            |
| 3   | `pip show convo-recall` (in venv)                                 | dist-info version matches `__version__` (caught the 0.2.0/0.3.0 mismatch)             |
| 4   | `python3 -c "import convo_recall; print(convo_recall.__version__)"` | Code-level version string                                                           |
| 5   | `recall --help`                                                   | Subcommand list intact                                                                |
| 6   | `recall doctor`                                                   | Self-reported health check (sidecar reachable, embed coverage, hook wiring per agent) |

---

## B. Database integrity

```python
import sqlite3, os
c = sqlite3.connect(os.path.expanduser('~/.local/share/convo-recall/conversations.db')).cursor()
c.execute("SELECT name FROM sqlite_master WHERE type='table'")  # schema present
c.execute("SELECT COUNT(*) FROM messages")                       # row count
c.execute("SELECT COUNT(*) FROM sessions")
c.execute("SELECT COUNT(*) FROM ingested_files")
c.execute("PRAGMA integrity_check")                              # corruption check
```

| #   | Probe                                                                                    | What it asserts                         |
|-----|------------------------------------------------------------------------------------------|-----------------------------------------|
| 7   | Tables exist (messages, sessions, projects, messages_fts, message_vecs*, ingested_files) | Schema not damaged                      |
| 8   | `recall stats` totals match raw counts                                                   | Stats command and DB agree              |
| 9   | `messages = user + assistant + tool_error`                                               | Role accounting balances                |
| 10  | `messages = sum(by_agent)`                                                               | Agent attribution accounts for all rows |
| 11  | `PRAGMA integrity_check`                                                                 | No SQLite corruption                    |

---

## C. Embedding subsystem

| #   | Probe                                          | What it asserts                                                                |
|-----|------------------------------------------------|--------------------------------------------------------------------------------|
| 12  | `ps aux \| grep "recall serve"`                | Sidecar running                                                                |
| 13  | `ls -l ~/.local/share/convo-recall/embed.sock` | Socket exists, correct perms (srw-------)                                      |
| 14  | HTTP POST to `/embed` over Unix socket         | Sidecar serves vectors, returns `{vector, dim, protocol}`                      |
| 15  | `dim` field uniform across all calls           | No mid-DB dimension change (caught 384→1024 jump)                              |
| 16  | `ingest._vec_ok(con)` via apsw                 | sqlite-vec extension loaded                                                    |
| 17  | `ingest._vec_count(con) == messages_count`     | Embedding coverage 100%                                                        |
| 18  | `recall doctor` reports same coverage          | Self-check agrees with raw count                                               |
| 19  | Sidecar PID stability across upgrades          | pipx upgrade doesn't orphan workers                                            |
| 20  | Sidecar CPU & memory (`ps aux`)                | Detect runaway model loops (caught 343% CPU spike during 0.3.1→0.3.4 backfill) |

---

## D. Search functionality

| #   | Probe                                            | What it asserts                                              |
|-----|--------------------------------------------------|--------------------------------------------------------------|
| 21  | `recall search "<query>" --project <p> --json`   | Project scoping works, JSON valid, `mode: hybrid`            |
| 22  | `recall search "<query>" --all-projects`         | Cross-project search                                         |
| 23  | Same query, vary `-n`                            | Result count parameter respected                             |
| 24  | Empty query                                      | Graceful handling, no crash                                  |
| 25  | Nonsense needle                                  | Returns empty results: `[]`, doesn't crash                   |
| 26  | Plant a unique needle, ingest, search            | End-to-end round-trip (auto-ingest → embed → index → search) |
| 27  | Result distribution by agent for the same query  | Detects ranking bias from prefix labels                      |

---

## E. Tail (cross-session aggregation — the 0.3.0/0.3.1 fix)

| #   | Probe                                                                       | What it asserts                                                          |
|-----|-----------------------------------------------------------------------------|--------------------------------------------------------------------------|
| 28  | `recall tail 100`                                                           | Auto-detects current project, returns up to 100 across multiple sessions |
| 29  | Header reads `<project> · N messages across M sessions · <oldest>→<newest>` | Cross-session aggregation working                                        |
| 30  | `recall tail 5 --all-projects`                                              | Cross-project version                                                    |
| 31  | `recall tail --json`                                                        | Machine-readable shape                                                   |
| 32  | `recall tail --project nonexistent`                                         | rc=1, "No sessions found", optional "Did you mean"                       |
| 33  | `recall tail 100 --session <id>`                                            | Explicit session override works                                          |
| 34  | `recall tail --expand 1,2`                                                  | Truncation override                                                      |

---

## F. Hooks (per-agent wiring)

| #   | Probe                                                                | What it asserts                                 |
|-----|----------------------------------------------------------------------|-------------------------------------------------|
| 35  | `~/.claude/settings.json` has `UserPromptSubmit` + `Stop` blocks     | Claude wired                                    |
| 36  | `~/.codex/hooks.json` has `UserPromptSubmit` + `Stop` blocks         | Codex wired                                     |
| 37  | `~/.gemini/settings.json` has `BeforeAgent` + `AfterAgent` blocks    | Gemini wired                                    |
| 38  | Hook scripts exist and are executable                                | conversation-memory.sh + conversation-ingest.sh |
| 39  | After one user turn, message count grows                             | Post-turn ingest hook fires                     |
| 40  | Pre-prompt context shows `## Prior context from convo-recall` block  | Pre-prompt hook injects search results          |
| 41  | Short prompts (<12 chars, "ok", "yes") get no injection              | Throttle works                                  |
| 42  | `CONVO_RECALL_HOOK_LOG=/tmp/x` enabled, then check log               | Hook payload logging works                      |

---

## G. Per-agent ingest correctness

| #   | Probe                                    | What it asserts                                                       |
|-----|------------------------------------------|-----------------------------------------------------------------------|
| 43  | ingested_files count by agent            | All three agents being scanned                                        |
| 44  | Sample messages per agent                | Content extraction working (no `[object Object]` etc.)                |
| 45  | Timestamp parseability per agent         | All in ISO-8601, no NULL timestamps                                   |
| 46  | Project resolution per agent             | Same logical cwd → same project_id (caught Gemini cwd-NULL dedup gap) |
| 47  | Sessions per (project, agent) cross-tab  | No agent-specific data drops                                          |

---

## H. tool_error extraction (the asymmetric piece)

| #   | Probe                                                                                      | What it asserts                                                            |
|-----|--------------------------------------------------------------------------------------------|----------------------------------------------------------------------------|
| 48  | Tool_error count by agent                                                                  | All three agents producing rows (regression: was claude-only before 0.3.4) |
| 49  | Codex prefix breakdown (`[exec_command_end exit=N]`, `[function_call_output]`, etc.)       | Typed extractors firing                                                    |
| 50  | Gemini prefix breakdown (`[gemini_error]`, `[gemini_warning]`, `[gemini tool ... status=error]`) | Typed extractors firing                                                  |
| 51  | Codex `[function_call_output]` rows containing `Process exited with code 0`                | False-positive count (regex truncation cliff)                              |
| 52  | Regex-pattern hit frequency in stored content                                              | Detects which patterns are doing the work                                  |
| 53  | Rows with no regex match                                                                   | Caught via `is_error` flag / explicit error type, not regex                |
| 54  | Content length distribution per agent                                                      | Detects truncation issues, anomalously short rows                          |

---

## I. Data quality / known issues

| #   | Probe                                          | What it asserts                                                     |
|-----|------------------------------------------------|---------------------------------------------------------------------|
| 55  | Stray projects (`/`, `projects`, `project`)    | Source files traced — confirmed e2e test runs, not install detritus |
| 56  | Duplicate display_names                        | Same project_name with different project_ids (Gemini/cwd-NULL bug)  |
| 57  | `e2e-watch-probe` row with 2030-01-01 timestamp | Sentinel for end-to-end smoke test                                  |
| 58  | Short-content rows (<20 chars) by role/agent   | Detect noise rows                                                   |
| 59  | Sentinel pollutes tail header range            | Cosmetic — header reads →2030-01-01                                 |

---

## J. Source-code review (read-only)

| #   | Probe                                                                | What it asserts                                             |
|-----|----------------------------------------------------------------------|-------------------------------------------------------------|
| 60  | `wc -l ingest.py` across versions                                    | Detect surface-level changes between versions               |
| 61  | grep for tool_error / extractor functions                            | Map source code to observed behavior                        |
| 62  | Read `_codex_fco_error`, `_gemini_record_error`, `_codex_event_msg_error` | Document per-agent extraction logic                    |
| 63  | `_ERROR_PATTERNS` regex                                              | Catalog detection signals                                   |
| 64  | Verify dist-info version matches `__init__.py.__version__`           | Catch packaging mismatch (caught it in 0.2.0→0.3.0 release) |

---

## Is this a "full use-case test"?

No, and it's important to be honest about that. What this sweep is:

✅ **A health check / integrity audit.** It verifies that the plumbing works — the binary runs, the DB is well-formed, the sidecar serves vectors, the hooks are wired, ingest produces rows, search returns results, tail aggregates correctly. Roughly equivalent to a smoke test plus invariant checks plus regression spot-checks.
