# Real-world feedback — claude-sandbox session, 2026-04-30

A Claude agent running inside `claude-sandbox` (separate from the dev session)
provided structured feedback after a real session using `recall`. Captured
verbatim below for archive purposes.

This feedback drove the F-1 through F-7 fix plan, all of which landed.

---

## Verbatim transcript

```
root@a017f1aea09e:/# cd /work/projects/app-claude/
root@a017f1aea09e:/work/projects/app-claude# claude
 ▐▛███▜▌   Claude Code v2.1.123
▝▜█████▛▘  Opus 4.7 (1M context) · Claude Max
  ▘▘ ▝▝    /work/projects/app-claude

❯ ok, what to do now?
…

❯ do you know anything about convo-recall

● I only know what the hook context tells me: there's a CLI called convo-recall
  (invoked as recall search "<query>") that searches this project's
  conversation history, with --all-projects to widen the scope. It's surfaced
  as a way to check prior discussions before searching the web or reinventing
  something. I don't have direct knowledge of how it's installed or
  implemented beyond that.

❯ yes [poke at the binary]

● Bash(recall --help …)
  → recall search "MoodMix sprint plan next steps" returned:
       No messages found for project='app-claude'.

  → recall search "MoodMix" --all-projects returned:
       [fts search]
       [app_claude] [assistant] 2026-04-29
       … (3 hits)

❯ so, is the recall working

● Yes — it's working. Quick verification:
  - recall --version and recall search --help both responded
  - Project-scoped search (recall search "..." with no flag) returned No
    messages found for project='app-claude' — that's because this repo's
    directory is app-claude but the ingested project slug is app_claude
    (underscore vs hyphen). So auto-detect isn't matching.
  - --all-projects worked perfectly.

  recall stats:
       Messages   : 145
       Embedded   : 0  (0%)
       Sessions   : 20
       Projects   : 7
       FTS        : porter tokenizer

❯ hmmm, what was any problem you faced, or feedback to the developer, why it
  did not trigger automatically with the hook?

● Honest feedback for the developer of convo-recall, based on this session:

  1. The hook doesn't actually trigger a search — it just nudges me to run
     one.
  The UserPromptSubmit hook injects a static reminder ("history is searchable
  via recall search ...") into every user turn. It doesn't run any search
  itself, doesn't pre-fetch relevant context, and doesn't pass the user's
  prompt into recall. So whether a search happens at all depends on me
  deciding it's worth it — which means it often won't, especially on short
  prompts like "yes" or "ok". A more useful hook would auto-run recall search
  "<user_prompt>" -n 3 and inject the top hits as context, the way some
  CLAUDE.md-style preludes do. Right now it's a reminder, not an integration.

  2. Project slug mismatch silently returns zero results.
  This repo lives at /work/projects/app-claude, but the ingested project slug
  is app_claude (underscore). Auto-detection from cwd produces app-claude, so
  recall search "..." returns No messages found for project='app-claude'
  even though there are 52 Claude messages from this exact repo in the DB.
  The hook reminder explicitly says "search all projects via --all-projects"
  only as an escalation — but for this repo, the default is silently broken.
  Fix options: normalize hyphens↔underscores when resolving the slug, or
  have search print a hint like "no results for slug X; did you mean Y?"
  when an obvious nearby slug exists. Right now an agent that trusts the
  default would conclude "topic never discussed" and move on.

  3. Embeddings are at 0%, so it's FTS-only.
  recall stats shows Embedded: 0 (0%) despite 145 messages. The install
  wizard wires up hooks and watchers but apparently didn't get the embedding
  sidecar running (or backfill never ran). Hybrid search is the headline
  feature — if a fresh install lands in FTS-only mode, that should at least
  surface in recall doctor or stats as a warning, e.g. "embedding sidecar
  not running — semantic search disabled, run recall serve and recall
  embed-backfill." Without that, users won't know they're getting a degraded
  experience.

  4. Minor UX things.
  - The hook reminder fires on every user turn, even for trivial replies
    ("yes", "go ahead"), which adds context noise. Some throttling — fire
    only on prompts above N tokens, or only when the prompt looks like a
    question — would reduce token bloat.
  - recall search has no --json output flag I noticed, so an agent has to
    scrape the human-formatted text. A machine-readable mode would make it
    much easier for agents (or other hooks) to chain it.
  - The redaction in output ([moodmix], [slug], [claude], [agent], [branch]
    all bracketed) is aggressive — at one point the search hit showed
    branch-per-[agent]: \[claude]/`, `gemini/`, `[codex]/* where gemini/*
    was un-redacted but claude/* and codex/* were. That inconsistency
    suggests the redactor's pattern list isn't symmetric across agent names.
    It also makes some hits hard to read — I had to infer that [moodmix]
    in a path was the literal project directory.

  TL;DR for the developer: the hook is a reminder, not an integration; the
  default project slug is broken on hyphenated dirs; embeddings aren't
  actually on after install; and the redactor is asymmetric. Fixing #2 alone
  would dramatically increase the odds an agent actually finds prior context
  on first try.
```

---

## How each finding was addressed

| # | Finding | Resolution |
|---|---|---|
| 1 | Hook is a reminder, not an integration | **F-3 — fixed**. `conversation-memory.sh` now runs `recall search --json` against the user's prompt for substantive prompts and injects top hits as context under "## Prior context from convo-recall". |
| 2 | Slug mismatch on hyphenated dirs | **F-1 — fixed**. `slug_from_cwd` collapses hyphens to underscores. Search prints "Did you mean: <slug>?" when a near-miss exists. README's "Project scope" section corrected. |
| 3 | Embeddings at 0% with no warning | **F-4 — fixed (discoverability path)**. Diagnosis: `[embeddings]` extra wasn't installed in the venv, wizard correctly skipped sidecar. Real defect was the silent 0% with no hint to the user. `recall stats` and `recall doctor` now surface embed-extra/sidecar/coverage status with the exact command to fix. |
| 4 | Hook fires on every turn | **F-6 — fixed**. Hook now skips the static reminder for prompts < 12 chars or matching the interjection regex `^(yes\|no\|ok\|okay\|sure\|hmm+\|...)\.?$`. Opt-out via `CONVO_RECALL_HOOK_AUTO_SEARCH=off`. |
| 4 | No `--json` output mode | **F-2 — fixed**. `recall search --json` emits a single JSON document with `query/project/agent/mode/n/results/[did_you_mean]`. |
| 4 | Redactor asymmetric (`[claude]`/`[codex]` bracketed but `gemini` not) | **F-5 — investigated, NOT a bug**. The bracketing is SQLite FTS5's `snippet()` highlighting matched query tokens. Agent's query was `"agent branches claude frontend codex ops"` (no "gemini") — only matched terms get bracketed. Documented in README; regression test `test_search_snippet_brackets_only_query_matches` locks the behavior. |

## Outcome

Plan delivered as 6 paired implement + test items. Final state:
- 159 unit tests + 2 hook shell tests pass on macOS
- 9-stage Linux sandbox harness passes end-to-end
- `tests/sandbox-results.ndjson` has 17 benchmark records
