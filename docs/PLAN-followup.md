# convo-recall — v0.2.x follow-up plan

Tracking the findings from the 2026-04-30 three-agent code review (RAG /
code / security) that were *not* addressed by the cheap-fix sprint.

These need design work before implementation. Each item is sized into a
separate sprint so it can be picked up independently.

---

## P1 — Recall cliff (#4)

**Symptom.** `recall search foo --agent codex` (or any narrow agent/project
filter) returns 0 hits even when 20+ codex messages contain `foo`. Cause:
search() prefilters FTS and vec at top-100 *globally*, then intersects with
the filter's rowid set. When the filter set is a small fraction of the
corpus (codex is 414 / 61K = 0.7%), the intersection is empty for any
non-rare term.

**Verified RED test.** `tests/test_ingest.py::test_recall_cliff_with_skewed_agent_distribution`.

### Approach

Switch from "global prefilter then intersect" to "filter-aware retrieval":

1. **Compute filter set first.** (Already happens — `filter_rowids` is built
   at `ingest.py:1090`.)
2. **Choose retrieval strategy by filter cardinality.**
   - `len(filter_rowids) >= 5_000`: keep current top-100 prefilter; the
     intersection density is high enough.
   - `500 <= len(filter_rowids) < 5_000`: bump prefilter to
     `min(filter_rowids_size * 2, 1000)` so the intersection has room.
   - `len(filter_rowids) < 500`: brute-force exact cosine against just the
     filtered subset — sub-millisecond at this size, exact recall.
3. **FTS side**: pass the filter rowids into the FTS query as
   `messages_fts MATCH ? AND rowid IN (filter_set)` so FTS itself returns
   only filter-matching rows.

### Files

- `src/convo_recall/ingest.py:1080-1180` — `search()` prefilter logic.
- `src/convo_recall/ingest.py:339` — `_vec_search` may need a `restrict_rowids`
  argument.

### Acceptance

- `tests/test_ingest.py::test_recall_cliff_with_skewed_agent_distribution`
  flips green: `--agent codex` returns ≥ 10 hits when 20 ground-truth
  matches exist among 1000 claude rows.
- E2E latency stays < 500 ms warm.

---

## P0 — Secret redaction (#2)

**Symptom.** `_clean_content` strips ANSI/XML/box-drawing but lets API
keys, GitHub PATs, AWS access keys, and JWTs survive verbatim into the FTS
+ vector index. Combined with the now-fixed file-mode bug, peer users on a
multi-user box could (until the fix landed) `recall search 'sk-'` to harvest
keys.

**Verified RED test.** `tests/test_ingest.py::test_clean_content_redacts_obvious_secrets`.

### Approach

Add a `_redact_secrets(text: str) -> str` step inside `_clean_content`
that replaces well-known secret shapes with a stable placeholder.

Pattern set (start narrow; expand as new shapes show up):

| Pattern | Replacement |
|---|---|
| `sk-[A-Za-z0-9]{20,}` | `«REDACTED-OPENAI-KEY»` |
| `sk-ant-(?:api\d+-)?[A-Za-z0-9_-]{20,}` | `«REDACTED-ANTHROPIC-KEY»` |
| `ghp_[A-Za-z0-9]{30,}` / `gho_…` / `ghs_…` | `«REDACTED-GITHUB-TOKEN»` |
| `AKIA[0-9A-Z]{16}` | `«REDACTED-AWS-KEY»` |
| `eyJ[A-Za-z0-9_-]+?\.[A-Za-z0-9_-]+?\.[A-Za-z0-9_-]+` | `«REDACTED-JWT»` |
| Slack `xoxb-…` / `xoxp-…` | `«REDACTED-SLACK-TOKEN»` |

Configuration:
- Always-on by default (no opt-out flag — privacy-by-default).
- Add `CONVO_RECALL_REDACT=off` env var for users who **really** want raw
  content (e.g. testing, security research). Document in README.
- Add `recall doctor --scan-secrets` that reports how many existing rows
  match the patterns (so users discover what already leaked into their DB).
- Add `recall backfill-redact` that re-applies the redactor to all existing
  `messages.content` and rebuilds FTS.

### Files

- `src/convo_recall/ingest.py:101-127` — `_clean_content` and helpers.
- New: `src/convo_recall/redact.py` (keep patterns isolated and unit-testable).

### Acceptance

- `tests/test_ingest.py::test_clean_content_redacts_obvious_secrets` green.
- New `recall backfill-redact` command + test covering "DB had a secret →
  backfill → secret no longer findable via FTS or vector search."
- Document the redaction patterns in README so users know what is and
  isn't covered.

---

## P1 — Gemini slug doesn't align with Claude/Codex conventions (#7)

**Symptom.** `~/.gemini/tmp/{sha256-hash}/chats/session-*.jsonl` produces a
project_slug that's the SHA-256 dir name. Cross-agent `--project apps_noema`
silently misses all Gemini sessions because Gemini's slug isn't `apps_noema`,
it's `1c19fb10eb84...`.

### Approach

Two-layer resolution, in order:

1. **Read cwd from session header.** Some Gemini sessions include a `cwd` or
   `projectDir` field in the first record. If present, derive slug from it
   exactly like Codex does (`_codex_slug_from_cwd`).
2. **Fallback: alias map.** Add a `~/.local/share/convo-recall/gemini-aliases.json`
   that maps `{sha256-hash → human-slug}`. Auto-populated by `recall install`
   when it detects a Gemini dir whose hash maps to a known cwd via Gemini's
   own metadata. User can hand-edit.
3. **Last resort.** Use the SHA-256 hash unchanged but document it. Better
   than the current silent misalignment.

### Files

- `src/convo_recall/ingest.py:768` — `_gemini_slug_from_path`.
- `src/convo_recall/ingest.py:780+` — `ingest_gemini_file` header parsing.

### Acceptance

- New test fixture with realistic Gemini hash dirs + header containing cwd.
- Asserts produced slug matches the human-readable convention.
- E2E exercises a hashed-dir gemini session and verifies cross-agent
  `--project X` finds it.

---

## P2 — `recall forget` deletion API (#8)

**Symptom.** No way to delete a session/pattern/date-range. Only nuclear
`recall uninstall --purge-data`. If a user pastes a secret, only options
are nuclear DB wipe + 30-min re-embed, or hand-edit SQLite. Privacy
dead-end pre-OSS.

### Approach

New subcommand: `recall forget` with mutually-exclusive scope flags.

```
recall forget --session <session_id>          # all messages + sessions row + ingested_files row
recall forget --pattern '<regex>'             # all messages whose content matches; preview by default
recall forget --before YYYY-MM-DD             # all messages older than date
recall forget --project <slug>                # all messages from a project
recall forget --agent <name>                  # all messages from an agent
recall forget --uuid <message-uuid>           # single message
```

Defaults to `--dry-run` showing match count + first-3 excerpts; require
`--confirm` to actually delete.

After deletion: rebuild FTS (`INSERT INTO messages_fts(messages_fts) VALUES('rebuild')`),
and prune `message_vecs` rows whose rowid no longer exists in messages.

### Files

- `src/convo_recall/cli.py` — new subcommand.
- `src/convo_recall/ingest.py` — new `forget()` function.

### Acceptance

- Unit tests for each flag + the `--dry-run` / `--confirm` gate.
- E2E covers: insert → forget --pattern → search returns "No results"
  AND vector + FTS index both purged.
- Add `## Privacy` section to README documenting the API.

---

## P2 — Self-heal newest-first + larger cap (#9)

**Symptom.** Self-heal pass walks unembedded rows oldest-first, capped at
500/pass. After fresh `recall install` against a 60K-row pre-existing DB
imported from a backup, the most recent (and most-queried) messages heal
last — hybrid quality ramps over multiple hours.

### Approach

1. Change `ORDER BY m.rowid` → `ORDER BY m.rowid DESC` so most recent rows
   heal first.
2. Auto-run `embed_backfill()` once at the end of `recall install`'s
   "Initial ingest" phase (when `--with-embeddings` is set). Catches the
   "fresh-install on existing DB" case in one go instead of N self-heal
   passes.

### Files

- `src/convo_recall/ingest.py:1079` — self-heal SELECT.
- `src/convo_recall/install.py:225` — initial ingest hook.

### Acceptance

- Test asserts self-heal SELECT uses `DESC` ordering.
- Manual: copy a real DB into a fresh sandbox, `recall install`, observe
  100% embedding within minutes (not hours).

---

## P2 — `_vc` re-entrancy refactor (#10)

**Symptom.** Module-level `_vc` is mutable and represents two things at
once: the apsw connection AND the "vec is available" flag. Multiple
`open_db()` calls in one process clobber it. Risky for any caller that
holds two connections (e.g. a future test runner that opens N DBs).

### Approach

Replace global `_vc` with two pieces of per-connection state attached to the
returned `apsw.Connection`:

- `con._vec_enabled: bool` — set in `open_db` based on whether sqlite_vec
  loaded.
- `con._vec_capable: apsw.Connection | None` — alias to `con` if vec is
  enabled, else None. Internal helpers (`_vec_insert`, `_vec_search`,
  `_vec_count`) take the connection as their first arg.

Migration plan:
- Helpers accept an optional `con` arg with default = current global
  (backward compat).
- Tests + `scan_all` / `search` / backfills pass `con` explicitly.
- Eventually drop the global default.

### Files

- `src/convo_recall/ingest.py:48-50` — `_vc` declaration.
- `src/convo_recall/ingest.py:336-356` — `_vec_*` helpers.
- All call sites of `_vec_insert/_vec_search/_vec_count`.

### Acceptance

- Test: open two DBs in one process, ingest into each, verify both have
  separate vec state and don't clobber each other.

---

## Trivia / housekeeping

### `.bak` retained silently (#17)

Backup at `~/.claude/index/conversations.db.pre-v020.20260430-013233.bak`
(374 MB) was created during the v0.2.0 in-place migration. Add a CHANGELOG
note + a `recall doctor` check that surfaces stray `.bak` files older than
30 days for cleanup confirmation.

### Long-tail code-quality items (P2, group sprint)

These are seven smaller items the code review surfaced. Each is too small
to deserve its own sprint, but together they're a coherent "internal
cleanup" pass. Bundled here so the next sprint planner sees them.

| ID | Item | File / Line | Notes |
|----|------|-------------|-------|
| LQ-1 | `scan_one_agent` and `scan_all` share dispatch logic — extract a single `_dispatch_ingest(agents)` helper | `ingest.py` `scan_all` + `scan_one_agent` | Pure refactor; behavior identical. |
| LQ-2 | `con.changes()` after `INSERT OR IGNORE` is connection-scoped, not statement-scoped — concurrent ingest from two processes can read the wrong count | `ingest.py:632, 670, 822` | Replace with `RETURNING rowid` on the INSERT (apsw 3.46+ supports it) so the changes count and the rowid come from the same statement atomically. |
| LQ-3 | `try: rec = json.loads(raw); except (json.JSONDecodeError, ValueError): continue` in 3 sites silently drops malformed records | `ingest.py:572, 869, 1284` | Track a counter; surface non-zero counts in `recall ingest` summary line. Schema drift in upstream jsonl format becomes visible. |
| LQ-4 | No `schema_migrations` table — every `open_db()` re-runs `PRAGMA table_info` checks | `ingest.py:191, 239` | Add `_schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT)` and gate each migration on a version row. Faster cold-open + clearer audit trail. |
| LQ-5 | Sessions / ingested_files upsert duplicated 3x (claude / gemini / codex) | `ingest.py:692, 824, 925` | Extract `_upsert_session(con, agent, slug, sid, title, first_seen)` and `_upsert_ingested_file(...)`. Saves ~30 LOC and ensures the three parsers stay in sync. |
| LQ-6 | `_persist_message` split-brain — `ingest_file` (claude) keeps an inline INSERT; gemini and codex parsers use the `_persist_message` helper | `ingest.py:625, 715` | Refactor the claude path to also use `_persist_message`, then handle the tool_error edge case via a separate small helper. The current docstring at line 715 admits the split is temporary. |
| LQ-7 | README and CLAUDE.md doc drift — README says `~/.local/share/convo-recall/conversations.db`, the user's CLAUDE.md says `~/.claude/index/conversations.db` | `README.md`, `~/.claude/CLAUDE.md` (user-side) | Pick one canonical default, update both, and add a `recall doctor` check that warns on env override drift. |

### Tests we should add anyway

From the code review's "test gaps" list (none verified RED yet, but worth
adding as regression scaffolding):

- Concurrent ingest from two processes (file-locking and WAL behavior).
- Codex `session_meta` re-rescan on resume (the `lineno == 0` re-parse
  path in `ingest_codex_file`).
- Embed sidecar 500/timeout failure paths (caller falls back cleanly).
- FTS-rebuild crash mid-way (should be impossible after BEGIN IMMEDIATE
  fix, but assert it).
- Idempotency of `_migrate_add_agent_column` across multiple opens.

---

## Sequencing recommendation

1. **#2 secret redaction** — privacy is the most urgent gap pre-OSS.
2. **#4 recall cliff fix** — headline feature broken on real-world DBs.
3. **#7 Gemini slug** — invisible until users start running Gemini CLI.
4. **#8 `recall forget`** — needed for #2's recovery path on existing DBs.
5. **#9 self-heal order** — nice-to-have; only matters at install time.
6. **#10 `_vc` refactor** — internal cleanup; no user-visible benefit.

Each is roughly 1–3 hours of focused work. None depends on another.
