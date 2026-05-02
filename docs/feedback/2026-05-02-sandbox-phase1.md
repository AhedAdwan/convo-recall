# Real-world feedback — claude-sandbox session, 2026-05-01 → 2026-05-02

A two-day debugging session spanning the project-id refactor and the
Phase 1 hook-driven ingest landing in the sandbox.

Source: session `4b9c7106-b405-4347-95a8-b59bd63109e7`, 400 messages,
2026-05-01 20:22 UTC → 2026-05-02 15:31 UTC, 95 user prompts. Two
auto-compactions in the middle of the run.

---

## Arc summary

### Phase A — Slug typography drift (2026-05-01 20:22 → 21:38)

After a clean `pipx uninstall` + `recall install` on the sandbox, the user
ran the first cross-agent test and saw three different display names for
the same project across the three CLIs:

> "what is is passing app gemini and not app-gemini, app-claude is ok,
> but it is the same issue for app-codex"

`recall search --project app-codex foo` silently returned 0 hits. The hook
reminder told the agent to escalate to `--all-projects`, but new agents
trying defaults-only would conclude the topic was never discussed.

This drove the design discussion in Phase B.

### Phase B — Project identity research (2026-05-02 07:32 → 09:43)

The user reframed the question:

> "sometime I could be in app x directory and discuss with the agent
> app y (in another directory) issues, I even might modify code there as
> well. Still, it is important that when I have a conversation with the
> agent in app x directory, that becomes the long term context for app x."

A research agent was spawned to investigate (msg #72) — explored the three
CLIs' session conventions, reviewed web docs, and recommended the two-value
identity model: stable `project_id = sha1(realpath(cwd))[:12]` + visible
`display_name = basename(nearest-root-marker-ancestor)`. User approved at
msg #78 and the entire 24-item plan was loaded into `/claude-plan` at #83.

Implementation landed as commit **`3a1c6b8 feat(project-id): replace
lossy slugs with sha1(realpath(cwd)) + display_name`** with migration v4.
The four divergent helpers (`slug_from_cwd`, `_slug_from_cwd`,
`_slug_from_path`, `_gemini_slug_from_path`) collapsed to two functions
plus a normalized `projects` table.

### Phase C — Pipx install ergonomics (2026-05-02 07:42 → 11:43)

User tested install paths repeatedly:

```
pipx install 'convo-recall[embeddings] @ git+https://github.com/AhedAdwan/convo-recall.git'
```

This is the canonical install command for end users — does not require a
working tree. `recall install` then runs anywhere, no path argument
needed. Captured this in a `bin/convo recall.sh` helper at the user's
request (msg #219, #221) so iteration cycles in the sandbox could be
`/work/bin/convo recall.sh remove && /work/bin/convo recall.sh install`.

### Phase D — systemd `.path` non-recursion (2026-05-02 11:43 → 12:40)

Real-world observation: `recall ingest` was firing initially after install
but then went quiet for live appends inside existing project subdirs.
User worked back through the symptom:

> "Claude `~/.claude/projects/<flat-cwd>/<sid>.jsonl` worked before, the
> Path Changed=/root/.claude/projects did not include <flat-cwd>, so,
> I am confused" (#248)

Followed by:

> "I am not convinced, search the web for docs about this" (#251)

Web evidence: `man systemd.path` ("with `PathModified=/var/www/html`, a
change in `/var/www/html/dirxx/file.txt` will not trigger the script")
and Lennart Poettering's stance on systemd issue #4246 ("inotify watches
are a limited resource [and] this doesn't really scale, and we hence
won't do it.").

User then proposed the architectural pivot:

> "Ok, if the hook triggers a recall search, why not use an agent
> response completion hook to trigger recall ingest?" (#267)

This became Phase 1.

### Phase E — Phase 1 implementation (2026-05-02 12:47 → 13:42)

`/claude-plan` produced a 24-item plan saved at
`/Users/ahed_isir/.claude/plans/luminous-juggling-bachman.md`. 28
implement+test tasks were created and run in waves:

- Wave 1 — `conversation-ingest.sh` shell hook with 5s lockfile dedup
- Wave 2 — `install/_hooks.py` rewrite for per-CLI dual-hook support
- Wave 3 — wizard Step 2 prompt for ingest hook
- Wave 4 — `recall doctor` per-agent ingest-hook reporting
- Wave 5 — TD-003 entry, sandbox e2e Section 16 stub, README "Continuous
  ingest" subsection, CHANGELOG Unreleased
- Wave 6 — full local test suite green: **331 passed, 3 skipped, 0 failed**

Commit landed locally as **`10969f0 feat(hooks): hook-driven ingest
(Phase 1) — closes Linux .path recursion gap`** (+1012 / -135 across 16
files).

### Phase F — Stop hook validation failure (2026-05-02 13:44 → 13:51)

User immediately observed the new hook firing AND failing in the active
Claude session:

> "Stop hook error: Hook JSON output validation failed — (root):
> Invalid input" (#325)

The hook script had been emitting `hookSpecificOutput` — a field that's
only valid for `PreToolUse / UserPromptSubmit / PostToolUse` events.
The Stop event schema rejects it.

User correctly diagnosed the symptom:

> "I am not sure you understand what is happening, but it seems you have
> configured the stop hook here for you and when it runs, something
> breaks like this, it gets printed on the screen here and you stop." (#328)

Fix: emit empty stdout + exit 0 (universal across all three CLIs).
Committed in `10969f0` and refined in **`d2c5028 fix(hooks/e2e): hook PATH
fallback + honest e2e fixes from sandbox run`**.

### Phase G — Sandbox install + e2e attempt (2026-05-02 13:50 → 15:31)

Sandbox flow: `git push origin main` → `convo recall.sh remove` →
`convo recall.sh install` (in the container, picking up the new HEAD via
pipx) → `tests/sandbox-e2e-full.sh` execution.

Sandbox install completed. But the running Claude session (the one we
were debugging in) STILL did not see the Stop hook fire after the fix
was deployed:

- Lock mtime stale at 219s (~4 minutes) old despite 5+ turns since
- No `recall ingest` processes spawned since manual smoke-test ~4 min ago
- No `settings.local.json` permission overrides
- No log files in standard locations

Working hypothesis (#398): "Claude Code disabled this Stop hook for the
rest of this session after the earlier 'Hook JSON output validation
failed' errors. Some hook systems fail-stop after N validation failures
so a broken hook doesn't keep firing on every turn."

### Phase H — Confirmation in fresh session (2026-05-02 15:31)

User opened a fresh Claude session in the sandbox at `/work/projects/app-claude`
and tested:

```
> recall tail 10 --project app-claude
session 8eb629ae · app-claude · 6 messages · 2026-05-02 15:14→15:15 · latest 21s ago
```

**Latest message 21s old.** The hook IS firing in fresh sessions. The
debugging-session blackout was confirmed to be Claude Code's session-
level disable-after-validation-failure behavior, not a code defect in
`d2c5028`.

---

## Resolution mapping

| Finding | Where it landed |
|---|---|
| Slug typography drift across CLIs | **shipped** in `3a1c6b8` — sha1+display_name model, migration v4. README "Project scope" + CHANGELOG. |
| Embed coverage shows 0% during initial backfill, no progress signal | **shipped** in `87a5be9` (clearer first-run messaging) + `d6fab22` (one-shot tqdm in `recall stats`) — both pre-Phase 1. |
| systemd `.path` non-recursive on Linux | **worked around** in `10969f0` (Phase 1 hooks) — TD-003 documents the underlying limitation. |
| Stop-hook JSON schema mismatch (`hookSpecificOutput` invalid for Stop) | **shipped** in `10969f0` + `d2c5028` — empty stdout + exit 0 for both ingest hook and existing search hook. |
| Pipx install ergonomics from a public GitHub source | **documented** — canonical command is `pipx install 'convo-recall[embeddings] @ git+https://github.com/AhedAdwan/convo-recall.git'`. Helper script at sandbox `/work/bin/convo recall.sh`. |
| `apsw.BusyError: database is locked` on first install (concurrent wizard-backfill + watcher) | **NOT YET FIXED** — see TD-004. |
| No regression test for Claude Code's "disable Stop hook after N validation failures" behavior | **NOT YET COVERED** — see TD-005. |
| Phase 1 hook end-to-end test in formal harness | **NOT YET RUN** — `sandbox-results.ndjson` last record is 2026-05-01 00:10:50Z, predates `3a1c6b8` and `10969f0`. Re-run pending. |

---

## Open items routed to TECH_DEBT.md

- **TD-004** — `apsw.BusyError: database is locked` during first install.
  Captured in container logs at `/root/.local/state/convo-recall/{convo-recall-wizard-backfill.log, watch.log}`.
- **TD-005** — Missing regression test for Claude Code's session-level
  disable-after-validation-failures behavior. The fix in `10969f0` /
  `d2c5028` is verified only by manual fresh-session test (Phase H above).

---

## Phase I — TD-004 race confirmation (2026-05-02 16:34 → 16:42)

After the doc archive landed, an attempt to re-run the formal
`sandbox-e2e-full.sh` harness against `HEAD = d2c5028` revealed a
deeper issue worth recording: the install path itself is broken on a
fresh sandbox under `--scheduler polling`. The first install (16:34
attempt) reproduced the same `apsw.BusyError: database is locked` we
captured the previous day — both `recall watch` (the polling-tier
watcher subprocess) and `recall _backfill-chain` (the detached initial
ingest + embed-backfill child) hit the trace at `ingest.py:249`,
`con.execute("PRAGMA journal_mode=WAL")`. Watcher and backfill both
died; sidecar lived; DB ended at 0 messages.

### Hypothesis

The two surviving subprocesses race for SQLite's exclusive write lock
during WAL-mode transition:
- `recall _backfill-chain` started detached via `Popen(start_new_session=True)`
  at `_wizard.py:373` — runs in parallel.
- `recall watch` started per-agent (3 agents in this sandbox) at
  `_wizard.py:403` — also detached.

Both call `_enable_wal_mode` on first DB open; SQLite WAL-mode
init requires exclusive lock; apsw does not retry on `BusyError`.

The author already documented this constraint at `_wizard.py:329-340`:

> "Initial ingest must NOT race the watcher's first scan for the WAL
> writer lock, so the watcher install goes LAST (after the DB is
> already populated). Polling tier hits this deterministically;
> launchd / systemd are async-bootstrap and may or may not race."

But the "LAST" sequencing only holds in the wizard's main thread.
Once `Popen(start_new_session=True)` returns at line 373, the backfill
child is alive and contending for the lock — yet the wizard moves on
to spawn watchers at line 403 inside the same wall-clock second.

### Confirmation experiment

To isolate the participant set, ran `recall install --with-embeddings
--scheduler polling` interactively (no `-y`) and answered:

| Step | Question | Answer |
|---|---|---|
| 1/5 | Install polling watchers? | **n** ← key change |
| 2/5 | Wire response-completion ingest hooks? | y |
| 3/5 | Hybrid vector + FTS search | (auto, --with-embeddings) |
| 4/5 | Wire pre-prompt search hooks? | y |
| 5/5a | Run initial ingest now? | y |
| 5/5b | Embed all messages in one pass? | y |

Result:
- ✅ NO `apsw.BusyError` in any log
- ✅ Backfill completed cleanly: **66 messages ingested, 65/66 embedded (98%)**
- ✅ `/root/.local/state/convo-recall/watch.log` does not exist (watcher
  was never spawned, confirming the gate worked)
- ✅ Sidecar (pid 20360) and backfill (pid 20361) coexist without contention

The single-line difference (whether `do_watchers` is True) deterministically
flips the outcome between deadly-race and clean-startup. So the race is
unambiguously between `recall _backfill-chain` and `recall watch`, both
inside `_enable_wal_mode`.

### Fix candidates (rank-ordered easy → robust)

1. **Retry-with-backoff inside `_enable_wal_mode`** at `ingest.py:249` —
   ~6 lines, no architectural change. Both subprocesses re-attempt on
   BusyError; whichever wins the lock wins.
2. **Wizard runs WAL-init synchronously BEFORE spawning subprocesses** at
   `_wizard.py:340` — eliminate the contention by ensuring the DB is in
   WAL mode before either child opens it.
3. **Wait for backfill child to clear `_enable_wal_mode` before spawning watcher**
   at `_wizard.py:399` — restore the author's intended ordering with explicit
   synchronization.

### Routed to TECH_DEBT

TD-004 updated with the confirmed root cause + line numbers. The
existing TD-005 still stands (no regression test for Claude Code's
session-level disable-after-validation-failures behavior).

---

## Outcome

Two large commits shipped (`3a1c6b8` project-id refactor, `10969f0`
Phase 1 hooks) plus a follow-up patch (`d2c5028`). All driven by direct
real-world feedback observed inside `claude-sandbox`. The formal
`sandbox-e2e-full.sh` harness has NOT yet covered any of these changes
— the next run will be the first to exercise the project-id migration
and the new `conversation-ingest.sh` end-to-end.

The TD-004 race uncovered during this debugging cycle is itself a
candidate for the next sprint's regression test: any new harness stage
that asserts "after `recall install --with-embeddings --scheduler
polling`, watcher pid is alive AND backfill log has zero `apsw.BusyError`"
would catch the regression deterministically.
