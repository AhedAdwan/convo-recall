## B4: Wizard + factory rewire — make `recall install` truly cross-platform

**Status:** not started
**Dependencies:** A, B1, B2, B3 (all four schedulers must exist)

### Scope

Drop `_require_macos()` from `run()` and `uninstall()`. Implement `detect_scheduler()` factory that walks `[LaunchdScheduler, SystemdUserScheduler, CronScheduler, PollingScheduler]` in priority order and returns the first whose `available()` is True. Refactor the wizard (now in `_wizard.py`) to talk to a `Scheduler` instance instead of hardcoded launchd code. Add `--scheduler {auto,launchd,systemd,cron,polling}` override flag. Move hook wiring out of `__init__.py` into `_hooks.py` (no behavior change). Update `uninstall()` to walk `all_schedulers()` so a user who installed under one tier and switched OS gets clean teardown.

### Key Components

- `install/schedulers/__init__.py`: `detect_scheduler()`, `get_scheduler(name)`, `all_schedulers()`.
- `install/_hooks.py`: extract `_hook_target`, `_hook_block`, `_wire_hook`, `_unwire_hook`, `install_hooks`, `uninstall_hooks`, `_find_hook_script` from current `__init__.py` (no logic change).
- `install/_wizard.py`: extract the `run()` function. Replaces hardcoded plist generation with `sched.install_watcher(...)` etc. The wizard's Step 1 prompt uses `sched.consequence_yes()` / `sched.consequence_no()`.
- `install/__init__.py`: thin facade — re-exports `run`, `install_hooks`, `uninstall_hooks`, plus an updated `uninstall()` that loops over `all_schedulers()`.
- `cli.py`: add `--scheduler` argparse choice, plumb through to `install.run()`.
- New wizard question on systemd-only: "Keep watchers running when logged out? (enables `loginctl enable-linger`)".

### Rough File Inventory

- New: 2 files (`install/_hooks.py`, `install/_wizard.py`)
- Modified: 3 files (`install/__init__.py`, `install/schedulers/__init__.py`, `cli.py`)
- Possibly removed: 0 (leave nothing dangling)

### Risks & Blockers

- **Existing wizard tests** (`test_install_emits_one_plist_per_enabled_agent`, `test_install_hooks_*`) reference functions that are about to move. Re-anchor tests to import from new locations BEFORE moving, then move, then verify green.
- **`uninstall()` walking all_schedulers()** could try to `launchctl bootout` on Linux (where it's not in PATH). The Scheduler's `available()` gate makes that impossible — `all_schedulers()` only returns instances whose class would have been picked.
- **Detection priority**: macOS host with both launchd and (somehow) cron should pick launchd first — list order matters. The list `[LaunchdScheduler, SystemdUserScheduler, CronScheduler, PollingScheduler]` is correct because `PollingScheduler.available()` is always True (must be last).
- **Wizard for a host where cron isn't available either** — falls through to PollingScheduler. Good.
- **`_require_macos()` removal**: search for any straggling callers; fail loudly on Linux if any remain.

### Done Criteria

- [ ] `detect_scheduler()` returns the right class on each test platform
- [ ] `recall install --scheduler auto -y --dry-run` runs on macOS (auto-picks launchd)
- [ ] `recall install --scheduler auto -y --dry-run` runs on Linux (auto-picks systemd or polling)
- [ ] `recall install --scheduler polling -y --dry-run` works on every platform
- [ ] `--scheduler X` for an unsupported X exits with a helpful message
- [ ] Wizard's Step 1 prompt text adapts to the chosen scheduler (different `consequence_yes/no` strings)
- [ ] `recall uninstall` cleans up across all schedulers (no error if a tier wasn't installed)
- [ ] All 51 existing tests still pass after the move
- [ ] All 4 hook wiring tests still pass after `_hooks.py` extraction
- [ ] All polling/systemd/cron unit tests from B1-B3 still pass

### Verification Artifacts

| File | Must Contain | Why |
|------|-------------|-----|
| `src/convo_recall/install/schedulers/__init__.py` | `def detect_scheduler` | Factory exists |
| `src/convo_recall/install/_wizard.py` | `def run(` | Wizard moved |
| `src/convo_recall/install/_hooks.py` | `def install_hooks` | Hooks extracted |
| `src/convo_recall/install/__init__.py` | `from .schedulers import detect_scheduler, all_schedulers` | Public surface |
| `src/convo_recall/cli.py` | `--scheduler` | CLI flag wired |
