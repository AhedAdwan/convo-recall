# TD-008 — `ingest.py` monolith split — sub-plan index

**Goal:** Split `src/convo_recall/ingest.py` (3,626 lines = 79% of the package's source) into focused modules so the public API is clean, test imports are scoped, and PRs stop colliding on one file. Pure file-move refactor — zero behavior delta.

**Source:** [`docs/TECH_DEBT.md`](../../../TECH_DEBT.md) → TD-008.

**Decomposition axis:** by concern, dependency-tree bottom-up. Each sub-plan extracts one module per PR; suite must stay green at every step.

**Precedent:** [`docs/plans/local/A-refactor-extraction.md`](../A-refactor-extraction.md) and B1–B4, C — the same shape was used successfully for the `install/` extraction in v0.3.0.

## Cross-module conventions

Rules that span more than one sub-plan, lifted out of individual risk sections so they're hard to miss.

### `_VEC_ENABLED` ownership

`_VEC_ENABLED` (per-connection sqlite-vec availability dict) lives in `db.py`. `embed.py` reads `db._VEC_ENABLED[con]` to decide whether vec ops are usable. Owner = `db.py`; reader = `embed.py`. The reverse — `_VEC_ENABLED` in `embed.py`, written by `db.open_db` — would create a `db → embed → db` cycle.

### Unassigned-symbol homes

Eleven module-level identifiers in current `ingest.py` aren't named in any sub-plan. Their canonical homes:

| Symbol | Line | Home | Lands in |
|---|---|---|---|
| `SUPPORTED_AGENTS` | 42 | `ingest/scan.py` | A7 |
| `PROJECTS_DIR` | 30 | `ingest/scan.py` | A7 |
| `GEMINI_TMP` | 32 | `ingest/scan.py` | A7 |
| `CODEX_SESSIONS` | 34 | `ingest/scan.py` | A7 |
| `_CONFIG_PATH` | 38 | `ingest/scan.py` (with `load_config`/`save_config`) | A7 |
| `_GEMINI_ALIAS_PATH` | 1553 | `ingest/gemini.py` (with `_load_gemini_aliases`) | A7 |
| `_Row` | 68 | `db.py` | A2 |
| `_row_factory` | 95 | `db.py` | A2 |
| `_AGENT_SOURCE_PATHS` | 1141 | `ingest/scan.py` | A7 |
| `_TEXT_BLOCK_TYPES` | 1043 | `ingest/writer.py` | A7 |
| `_extract_text` | 1046 | `ingest/writer.py` (re-home from `claude.py` — generic over text/input_text/output_text blocks; called by all three per-agent parsers via `_persist_message`) | A7 |

Note: this re-homes `_extract_text` and `_TEXT_BLOCK_TYPES` from `claude.py` (where A7's current text places them) into `writer.py`. This table is authoritative; A7's body will be reconciled when A7 runs.

### Docstring-freeze rule

The module-level docstring in `ingest.py` (covering `CONVO_RECALL_DB`, `CONVO_RECALL_SOCK`, `CONVO_RECALL_PROJECTS`, etc.) is asserted by `tests/test_ingest_docstring_truth.py`. The docstring stays unchanged in `ingest.py` through A1–A7 even when the constants it references move to other modules. A8 is the canonical move-and-update: it relocates the doc text to the new module homes and updates the test in the same PR.

## Pre-A1: baseline capture

Capture golden CLI snapshots from the v0.3.6 tree (`HEAD = 4e7f979`) into a fixture dir before A1 runs. Each subsequent sub-plan byte-diffs its output against the baseline as a behavior-preservation check.

```bash
mkdir -p docs/plans/local/td-008/baseline
recall search "ingest" --project convo-recall --limit 5 --json \
  > docs/plans/local/td-008/baseline/search.json
recall tail 30 --project convo-recall --json \
  > docs/plans/local/td-008/baseline/tail.json
recall stats > docs/plans/local/td-008/baseline/stats.txt
recall doctor > docs/plans/local/td-008/baseline/doctor.txt
```

The baseline directory is committed (small, deterministic). After each sub-plan lands, re-run the four commands and `diff` against the baseline. Quiesced state required for `stats` / `doctor` (sidecar idle, no in-flight ingest).

## Pre-A1: shim surface contract

Enumerate every legacy import of `convo_recall.ingest` so the back-compat shim's surface is explicit, not implicit.

```bash
grep -rhoE "(from convo_recall\.ingest import [^\n]+|import convo_recall\.ingest)" \
  tests/ src/ | sort -u > docs/plans/local/td-008/shim-surface.txt
```

The file is committed. A8 verifies the new shim re-exports every symbol named in `shim-surface.txt`. A1–A7 may freely add internal `from .X import …` lines but must not remove anything the shim is responsible for re-exporting.

## Sub-plans

| ID | File | Title | Depends on |
|---|---|---|---|
| A1 | [A1-identity.md](A1-identity.md) | Extract `identity.py` (project_id / display_name / legacy slug helpers) | none |
| A2 | [A2-db.md](A2-db.md) | Extract `db.py` (schema, migrations, open_db, connection helpers) | A1 |
| A3 | [A3-embed.md](A3-embed.md) | Extract `embed.py` (UDS client, vec helpers) | A2 |
| A4 | [A4-query.md](A4-query.md) | Extract `query.py` (search, tail, RRF, decay) | A2, A3 |
| A5 | [A5-backfill.md](A5-backfill.md) | Extract `backfill.py` (embed/tool_error/clean/redact/chunk backfills) | A2, A3 |
| A6 | [A6-admin.md](A6-admin.md) | Extract `admin.py` (stats, doctor, forget) | A2, A4 |
| A7 | [A7-ingest-subpackage.md](A7-ingest-subpackage.md) | Extract `ingest/` subpackage (claude/gemini/codex/writer/scan) | A1–A5 |
| A8 | [A8-shim-and-release.md](A8-shim-and-release.md) | `ingest.py` shim + cli/tests rewiring + v0.4.0 release | A7 |

## Execution order

```
A1 → A2 → A3 ─┬─→ A4 ─┐
              │       ├─→ A6 ─┐
              ├─→ A5 ─┤       ├─→ A7 → A8
              │       │       │
              └───────┴───────┘
```

A4, A5 can run in parallel after A3 lands. A6 needs A4. A7 collects from all of A1–A5. A8 finalizes.

## Acceptance gates per sub-plan

**Cross-cutting (every sub-plan A1–A7):** `recall search`, `recall tail`, `recall stats`, `recall doctor` produce byte-identical output to `docs/plans/local/td-008/baseline/`. Pytest suite green at every step.

| After | Gate |
|---|---|
| A1 | All tests green. `from convo_recall.ingest import _project_id` still works (re-export). |
| A2 | Cold-open of v0.3.x DB applies no new migrations. `tests/test_migration_project_id.py` green. |
| A3 | `embed("hi")` returns 1024-d list when sidecar up. `_vec_search` returns rows. |
| A4 | `recall search foo` and `recall tail` produce identical output to v0.3.6 baseline. |
| A5 | `recall tool-error-backfill` indexes 0 new rows on already-current DB. `recall backfill-redact --dry-run` shows zero matches expected. |
| A6 | `recall doctor`, `recall stats`, `recall forget --dry-run` produce identical output to v0.3.6. |
| A7 | Full pytest suite green. `recall ingest` produces 0 new rows on already-ingested JSONLs. |
| A8 | `pip install -e .` clean. Canonical imports don't warn; legacy `from convo_recall.ingest import …` warns once. v0.4.0 tag cut. |

## Total estimate

~3,600 LOC moved across 8 PRs + ~30 test imports rewired, ~1.5 days focused work. Output: v0.4.0 with one-release deprecation shim; v0.5.0 (later sprint) removes the shim.

## What stays in `ingest.py` after A8

The post-shim `ingest.py` is ~50 lines:
- Module docstring describing the deprecation.
- One `DeprecationWarning` emitted on import via `warnings.warn(...)`.
- `from .X import Y` re-exports for every legacy symbol that tests or external consumers reference.
- `_AGENT_INGEST` dispatch table and `ingest_file` / `ingest_gemini_file` / `ingest_codex_file` re-exports — the historical "front door" of the write path.
