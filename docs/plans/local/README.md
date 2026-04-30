# Linux port — sub-plan index

**Goal:** make `recall install` work end-to-end on Linux (systemd-user / cron / polling) and macOS (launchd) with a single shared wizard UX, using a strategy-pattern scheduler architecture.

**Source:** [`docs/PLAN-linux-port.md`](../../PLAN-linux-port.md) — full design spec.

**Decomposition axis:** hybrid — by phase, with phase B sub-decomposed by scheduler so each loop iteration stays in the 100-250 LOC range.

## Sub-plans

| ID | File | Title | Depends on |
|---|---|---|---|
| A | [A-refactor-extraction.md](A-refactor-extraction.md) | Move `install.py` → `install/` package; extract LaunchdScheduler | none |
| B1 | [B1-polling-scheduler.md](B1-polling-scheduler.md) | Add `PollingScheduler` (universal fallback) | A |
| B2 | [B2-systemd-scheduler.md](B2-systemd-scheduler.md) | Add `SystemdUserScheduler` (Linux native) | A |
| B3 | [B3-cron-scheduler.md](B3-cron-scheduler.md) | Add `CronScheduler` (Linux fallback) | A |
| B4 | [B4-wizard-factory-rewire.md](B4-wizard-factory-rewire.md) | Drop `_require_macos()`; wire `detect_scheduler()`; refactor wizard | A, B1, B2, B3 |
| C | [C-validation-polish.md](C-validation-polish.md) | pexpect tests, sandbox e2e, CI matrix, pyproject + README | B4 |

## Execution order

```
A → B1 ─┐
        ├──→ B4 ──→ C
A → B2 ─┤
        │
A → B3 ─┘
```

Phase B's three scheduler sub-plans (B1, B2, B3) are file-independent — they can run in any order or in parallel. B4 depends on all three (factory + wizard wires them together). C depends on B4.

## Acceptance gates per phase

| After | Gate |
|---|---|
| A | `pytest tests/test_ingest.py` → 51/51 still passing. No behavior change. |
| B1 | New polling-scheduler unit tests pass. Sandbox `recall install --scheduler polling --dry-run` works. |
| B2 | `systemd-analyze verify` passes on generated units. Sandbox loads a real `.service`+`.path` pair. |
| B3 | Tagged-line filter preserves user crontab on uninstall. |
| B4 | All four tiers selectable via `--scheduler X`. Existing wizard tests still pass. |
| C | CI green on `macos-latest` + `ubuntu-latest`. README accurately describes new behavior. |

## Total estimate

~900 LOC + ~120 tests, ~6 hours focused work.
