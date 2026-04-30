# convo-recall Linux port — full plan

A read-this-once-before-touching-anything document. Goal: cross-platform `recall install` that runs end-to-end on macOS (launchd) and Linux (systemd-user / cron / polling) with the same wizard UX. Strategy-pattern shape so each scheduler tier is isolated and individually testable.

---

## 1. Final directory layout

```
src/convo_recall/
├── __init__.py                            (unchanged)
├── ingest.py                              (unchanged)
├── redact.py                              (unchanged)
├── cli.py                                 (~5 LOC change: import path)
├── embed_service.py                       (unchanged)
├── hooks/
│   └── conversation-memory.sh             (unchanged)
└── install/                               ← NEW package, replaces install.py
    ├── __init__.py                        (public API)
    ├── _wizard.py                         (interactive flow)
    ├── _hooks.py                          (cross-CLI hook wiring)
    ├── _paths.py                          (XDG-aware path constants)
    └── schedulers/
        ├── __init__.py                    (detect_scheduler factory)
        ├── base.py                        (Scheduler ABC + Result dataclass)
        ├── launchd.py                     (macOS — extracted from current install.py)
        ├── systemd.py                     (Linux user-instance — NEW)
        ├── cron.py                        (Linux fallback — NEW)
        └── polling.py                     (universal fallback — NEW)

tests/
├── test_ingest.py                         (existing 51 tests — keep all passing)
├── test_install_wizard.py                 (NEW — pexpect-driven wizard test)
├── test_schedulers.py                     (NEW — per-scheduler unit tests)
├── sandbox-e2e-full.sh                    (existing — keep passing)
├── sandbox-hooks-e2e.sh                   (existing — keep passing)
└── sandbox-linux-port-e2e.sh              (NEW — Linux scheduler tiers e2e)
```

---

## 2. File-by-file contents

### `install/_paths.py` (~30 LOC, no logic, just constants)

```python
import os, platform
from pathlib import Path

def is_macos() -> bool: return platform.system() == "Darwin"
def is_linux() -> bool: return platform.system() == "Linux"

def scheduler_unit_dir() -> Path:
    """Where scheduler unit files live."""
    if is_macos():
        return Path.home() / "Library" / "LaunchAgents"
    # XDG: ~/.config/systemd/user/
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "systemd" / "user"

def log_dir() -> Path:
    """Where service logs go."""
    if is_macos():
        return Path.home() / "Library" / "Logs"
    # XDG: ~/.local/state/convo-recall/logs/
    return Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")) / "convo-recall" / "logs"

def runtime_dir() -> Path:
    """Where PID files / sockets go."""
    if is_macos():
        return Path.home() / "Library" / "Caches" / "convo-recall"
    # XDG: $XDG_RUNTIME_DIR (typically /run/user/$UID); fall back to /tmp
    rd = os.environ.get("XDG_RUNTIME_DIR")
    return Path(rd) / "convo-recall" if rd else Path("/tmp") / f"convo-recall-{os.getuid()}"
```

### `install/schedulers/base.py` (~90 LOC, contract definition)

```python
from abc import ABC, abstractmethod
from dataclasses import dataclass

@dataclass
class Result:
    """Outcome of an install/uninstall operation."""
    ok: bool
    message: str        # human-readable status, ✅ or ⚠ prefixed
    label: str | None = None   # service identifier (plist label, unit name)

class Scheduler(ABC):
    """Abstract base for per-platform service schedulers.

    Subclasses implement: launchd, systemd-user, cron @reboot+polling, polling-only.
    `detect_scheduler()` walks them in priority order and returns the first
    `.available()` instance.
    """

    name: str                  # short identifier ("launchd", "systemd", ...)
    persistent: bool           # survives reboot?
    file_event_driven: bool    # watches for file changes (vs polling)?

    @classmethod
    @abstractmethod
    def available(cls) -> bool:
        """Can this scheduler actually be used on the current host?"""

    @abstractmethod
    def install_watcher(self, agent: str, recall_bin: str,
                         watch_dir: Path, db_path: Path,
                         sock_path: Path, config_path: Path) -> Result:
        """Install + start a per-agent ingest watcher."""

    @abstractmethod
    def uninstall_watcher(self, agent: str) -> Result:
        """Stop + remove a per-agent ingest watcher."""

    @abstractmethod
    def install_sidecar(self, recall_bin: str, sock_path: Path) -> Result:
        """Install + start the embed sidecar."""

    @abstractmethod
    def uninstall_sidecar(self) -> Result:
        """Stop + remove the embed sidecar."""

    @abstractmethod
    def describe(self) -> str:
        """One-sentence description for the wizard. e.g.
        'launchd (macOS native, file-event driven, survives reboot)'"""

    @abstractmethod
    def consequence_yes(self) -> str:
        """Wizard '↪ if YES:' callout — one short sentence."""

    @abstractmethod
    def consequence_no(self) -> str:
        """Wizard '↪ if NO:' callout — what won't work + how to do it manually."""
```

### `install/schedulers/launchd.py` (~200 LOC, extracted from current install.py)

Wraps the existing `_ingest_plist`, `_embed_plist`, `_launchctl_load` helpers as a `Scheduler` subclass.

- `available()` — returns `is_macos()`.
- `install_watcher()` — generates plist, writes to `~/Library/LaunchAgents/`, calls `launchctl bootstrap gui/<uid> <plist>`.
- `uninstall_watcher()` — `launchctl bootout` + `unlink()`.
- `install_sidecar()` / `uninstall_sidecar()` — same pattern with the embed plist label.

**No behavior change vs current code.** Just refactored into a class.

### `install/schedulers/systemd.py` (~250 LOC, NEW)

For Linux desktops with systemd PID 1 + user-instance available.

- `available()` — runs `systemctl --user --version` AND `systemctl --user is-system-running` succeeds with output containing `running` / `degraded`. Returns False on `offline`, `command not found`, timeout, etc.
- Generates a `.service` + `.path` unit pair per agent:

```ini
# ~/.config/systemd/user/com.convo-recall.ingest.claude.service
[Unit]
Description=convo-recall ingest watcher (claude)
After=network.target

[Service]
Type=oneshot
ExecStart=%h/.local/bin/recall ingest --agent claude
Environment="CONVO_RECALL_DB=%h/.local/share/convo-recall/conversations.db"
Environment="CONVO_RECALL_SOCK=%h/.local/share/convo-recall/embed.sock"

[Install]
WantedBy=default.target

# ~/.config/systemd/user/com.convo-recall.ingest.claude.path
[Unit]
Description=Trigger convo-recall ingest when claude sessions change

[Path]
PathChanged=%h/.claude/projects
PathModified=%h/.claude/projects

[Install]
WantedBy=default.target
```

- `install_watcher()` — write both files, `systemctl --user daemon-reload`, `systemctl --user enable --now com.convo-recall.ingest.<agent>.path`.
- Sidecar is a simple `.service` with `KeepAlive=true` equivalent (`Restart=always`).
- `uninstall_watcher()` — `systemctl --user disable --now <unit>`, `rm` both files, `daemon-reload`.
- Linger handling — call `loginctl enable-linger $USER` only if the wizard explicitly asked (it's a separate wizard question on Linux: "should the watcher survive logout?").
- Validates generated unit syntax via `systemd-analyze verify` before loading (catches typos at install time).

### `install/schedulers/cron.py` (~150 LOC, NEW)

Fallback for Linux without systemd.

- `available()` — `crontab -l` returns exit code 0 or 1 (1 = "no crontab yet" — still usable).
- `install_watcher()` — there's no per-agent cron job; cron only triggers `recall watch &` once at `@reboot`. So `install_watcher()` for the first agent installs a single `@reboot recall watch >> ~/.local/state/convo-recall/logs/watch.log 2>&1` line; subsequent agent installs are no-ops (logged "already covered by polling watcher").
- `install_sidecar()` — adds `@reboot nohup recall serve --sock <path> &` line.
- `uninstall_*()` — read crontab, filter out only convo-recall lines (matched by tagged comment marker `# convo-recall`), reinstall.
- Tagging: every line we install ends with ` # convo-recall:<purpose>` so we can find/remove only our own lines without disturbing the user's.

### `install/schedulers/polling.py` (~120 LOC, NEW)

Universal fallback — sandbox/CI/headless containers.

- `available()` — always True.
- `install_watcher()` — spawn `recall watch &` via `subprocess.Popen` with `start_new_session=True`. Write PID to `<runtime_dir>/watch.pid`. The same single watcher serves all agents — subsequent calls are no-ops.
- `install_sidecar()` — same pattern with `recall serve`. Write PID to `<runtime_dir>/embed.pid`.
- `uninstall_*()` — read PID file, `os.kill(pid, SIGTERM)` with 5s grace, then `SIGKILL`. Remove PID file.
- Documents in `consequence_yes`: "won't survive reboot/logout — re-run install on restart."

### `install/schedulers/__init__.py` (~40 LOC)

```python
from .base import Scheduler, Result
from .launchd import LaunchdScheduler
from .systemd import SystemdUserScheduler
from .cron import CronScheduler
from .polling import PollingScheduler

_SCHEDULERS = [LaunchdScheduler, SystemdUserScheduler, CronScheduler, PollingScheduler]

def detect_scheduler() -> Scheduler:
    """Walk schedulers in priority order; return the first available."""
    for cls in _SCHEDULERS:
        if cls.available():
            return cls()
    raise RuntimeError("No scheduler available — should never happen, polling is the fallback")

def all_schedulers() -> list[Scheduler]:
    """For uninstall: return every scheduler that's available so we can clean up across tiers."""
    return [cls() for cls in _SCHEDULERS if cls.available()]

def get_scheduler(name: str) -> Scheduler:
    """Explicit override via `--scheduler X` flag on the CLI."""
    for cls in _SCHEDULERS:
        if cls().name == name:
            return cls()
    raise ValueError(f"Unknown scheduler: {name}")
```

### `install/_hooks.py` (~120 LOC, lifted verbatim from current install.py)

All the existing functions: `_hook_target`, `_hook_block`, `_wire_hook`, `_unwire_hook`, `install_hooks`, `uninstall_hooks`, `_find_hook_script`. Just moved out of install.py — no changes.

### `install/_wizard.py` (~250 LOC)

The 4-stage interactive wizard, refactored to talk to a `Scheduler` instance instead of hardcoded launchd. Includes the `_ask` helper.

Structure:
```python
def run(dry_run, with_embeddings, non_interactive, scheduler_override=None):
    print("convo-recall setup wizard")

    # Detection phase
    sched = get_scheduler(scheduler_override) if scheduler_override else detect_scheduler()
    detected = detect_agents()
    enabled = [d for d in detected if d.file_count > 0]
    embeddings_avail = check_embeddings_installed()

    print(f"Detected scheduler: {sched.describe()}")
    print(f"Detected agents: {enabled}")

    # Step 1: watchers
    do_watchers = _ask(
        "Install watchers...",
        if_yes=sched.consequence_yes(),
        if_no=sched.consequence_no(),
    )

    # Step 2: sidecar (skip if no embeddings extra)
    do_sidecar = _ask("Install embed sidecar...") if embeddings_avail else False

    # Step 3: hooks (cross-platform; uses _hooks.install_hooks)
    do_hooks = _ask("Wire pre-prompt hooks...")

    # Step 4: initial ingest
    do_ingest = _ask("Run initial ingest now...")
    do_backfill = _ask("Embed all existing rows...") if do_sidecar and do_ingest else False

    # Linger question (systemd-only)
    if sched.name == "systemd":
        do_linger = _ask("Keep watchers running when logged out...")

    # Summary + confirm gate
    # Apply via sched.install_watcher() / sched.install_sidecar()
```

### `install/__init__.py` (~80 LOC, public API)

```python
from .schedulers import detect_scheduler, get_scheduler, all_schedulers
from . import _wizard, _hooks

# Public API — what cli.py imports
def run(dry_run=False, with_embeddings=False, non_interactive=False, scheduler=None):
    return _wizard.run(dry_run, with_embeddings, non_interactive, scheduler)

def install_hooks(agents=None, dry_run=False, non_interactive=False):
    return _hooks.install_hooks(agents, dry_run=dry_run, non_interactive=non_interactive)

def uninstall_hooks(agents=None):
    return _hooks.uninstall_hooks(agents)

def uninstall(purge_data=False):
    """Tear down across ALL schedulers (in case user reinstalled with a different tier)."""
    for sched in all_schedulers():
        for agent in ("claude", "gemini", "codex"):
            sched.uninstall_watcher(agent)
        sched.uninstall_sidecar()
    if purge_data:
        # remove ~/.local/share/convo-recall/
        ...
```

### `cli.py` changes (~5 LOC)

```python
# Before: from . import __version__, ingest, install as _install
# After:  from . import __version__, ingest
#         from . import install as _install  # now a package, not a module

# Add `--scheduler` flag to install:
p_install.add_argument("--scheduler", choices=["auto", "launchd", "systemd", "cron", "polling"], default="auto")
```

Everything else in cli.py stays the same.

---

## 3. Test plan

### Existing tests — must keep all 51 passing

The hook tests, redaction tests, recall-cliff test, etc. don't touch install. **No refactor to `tests/test_ingest.py` needed**, except possibly adjusting imports if I move `install.run` calls.

The two install tests (`test_install_emits_one_plist_per_enabled_agent`, `test_install_plist_targets_correct_watch_dir`) will need updating because the launchd implementation moved into `LaunchdScheduler`. They become tests of `LaunchdScheduler` specifically.

### New unit tests — `tests/test_schedulers.py` (~200 LOC)

Per-scheduler unit tests:
- `LaunchdScheduler` — generates valid plist XML, plistlib-parseable; mock `launchctl` to verify command shape.
- `SystemdUserScheduler.available()` — mocked subprocess to test detection edge cases (offline, command not found, etc.).
- `SystemdUserScheduler.install_watcher()` — generates valid `.service` + `.path` unit; `systemd-analyze verify` validates syntax.
- `CronScheduler` — generates one `@reboot` line per service; uninstall preserves user's other crontab lines (matched by `# convo-recall` tag).
- `PollingScheduler` — `subprocess.Popen` mock + PID file roundtrip + SIGTERM cleanup.

### Wizard pexpect test — `tests/test_install_wizard.py` (~120 LOC)

Drives the real CLI binary through the y/n flow:

```python
def test_wizard_full_yes_flow():
    child = pexpect.spawn("recall install --scheduler polling --dry-run")
    child.expect("Install watchers")
    child.sendline("y")
    child.expect("Install the embed sidecar")
    child.sendline("y")
    child.expect("Wire pre-prompt hooks")
    child.sendline("y")
    child.expect("Run initial ingest now")
    child.sendline("n")
    child.expect("Apply these settings now")
    child.sendline("y")
    child.expect(pexpect.EOF)
    assert child.exitstatus == 0

def test_wizard_decline_watchers_shows_manual_path():
    child = pexpect.spawn("recall install --scheduler polling --dry-run")
    child.expect("Install watchers")
    child.sendline("n")
    # consequence text should appear
    child.expect("Run.*recall ingest.*manually")
    ...
```

Marked with `@pytest.mark.skipif(not shutil.which("recall"))` so it skips on environments where the wheel isn't installed.

### Sandbox e2e — `tests/sandbox-linux-port-e2e.sh` (~80 LOC)

Section 1: `--scheduler polling --dry-run` runs cleanly
Section 2: `--scheduler polling -y` actually starts a watcher; verify PID file exists, kill it, verify cleanup
Section 3: `--scheduler systemd --dry-run` runs cleanly (now that sandbox has systemd PID 1)
Section 4: `--scheduler systemd -y` actually loads a unit; verify `systemctl --user list-units` shows it; uninstall; verify it's gone
Section 5: `--scheduler cron --dry-run` runs cleanly
Section 6: detection logic — `recall install --dry-run -y` (no override) auto-picks systemd in this sandbox

### CI matrix — `.github/workflows/test.yml` (~20 LOC)

```yaml
strategy:
  matrix:
    os: [macos-latest, ubuntu-latest]
runs-on: ${{ matrix.os }}
```

Run pytest on both. The pexpect wizard test runs on both. Sandbox e2e isn't run in CI (it's a local-only validation).

---

## 4. Sequence of work

| Step | What | Files touched | Tests passing after |
|---|---|---|---|
| **A1** | Create `install/` package with current code unchanged. Move `install.py` content into `install/__init__.py`. Update `cli.py` import. Run tests — must still be 51/51 green. | `install/__init__.py`, `cli.py` | 51/51 |
| **A2** | Add `_paths.py`. Replace hardcoded `LAUNCHAGENTS`/`LOG_DIR` with `_paths.scheduler_unit_dir()` etc. Tests still 51/51. | `install/_paths.py`, `install/__init__.py` | 51/51 |
| **A3** | Add `schedulers/base.py`. Extract launchd code into `schedulers/launchd.py` as `LaunchdScheduler`. Update wizard to use `LaunchdScheduler()` directly (not yet via factory). Tests 51/51. | `install/schedulers/{base,launchd}.py` | 51/51 |
| **B1** | Add `schedulers/__init__.py` with `detect_scheduler()` factory that returns `LaunchdScheduler` on macOS, raises elsewhere. Drop `_require_macos()`. Tests 51/51. | `install/schedulers/__init__.py`, `install/__init__.py` | 51/51 |
| **B2** | Add `PollingScheduler`. Wire into factory. Test that `PollingScheduler().install_watcher()` spawns `recall watch &` and PID-file roundtrip works. | `install/schedulers/polling.py`, `tests/test_schedulers.py` | 51 + new |
| **B3** | Add `SystemdUserScheduler`. Generators produce parseable units (`systemd-analyze verify`). Detection logic correctly returns False when offline. | `install/schedulers/systemd.py`, `tests/test_schedulers.py` | 53 + new |
| **B4** | Add `CronScheduler`. Generators produce valid cron lines. Tagged-line filtering preserves user lines on uninstall. | `install/schedulers/cron.py`, `tests/test_schedulers.py` | 55 + new |
| **B5** | Move hook wiring from `install/__init__.py` → `install/_hooks.py`. No behavior change. | `install/_hooks.py`, `install/__init__.py` | 57 + |
| **B6** | Move wizard from `install/__init__.py` → `install/_wizard.py`. Add `--scheduler` override flag. Wizard now calls `detect_scheduler()` and uses scheduler methods. | `install/_wizard.py`, `install/__init__.py`, `cli.py` | all green |
| **B7** | Update `install/__init__.py` `uninstall()` to walk `all_schedulers()` (covers cross-tier cleanup if user reinstalled differently). | `install/__init__.py` | all green |
| **C1** | Add `tests/test_install_wizard.py` (pexpect). Mark CI-only. | `tests/test_install_wizard.py` | + 5-10 wizard tests |
| **C2** | Add `tests/sandbox-linux-port-e2e.sh`. Run in claude-sandbox; assert all tiers work as designed. | new shell script | sandbox e2e green |
| **C3** | Update `.github/workflows/test.yml` for matrix `[macos-latest, ubuntu-latest]`. Verify both green in CI. | workflow yml | CI green on both |
| **C4** | Update `pyproject.toml` classifier (drop "MacOS X", add "POSIX :: Linux", "OS Independent"). Update `README.md` Requirements + Install + Schedulers sections. | `pyproject.toml`, `README.md` | docs match reality |
| **C5** | Add `pexpect` to `[dev]` dependencies. | `pyproject.toml` | clean install |

Each step ends with full test run; if anything goes red, fix before continuing.

---

## 5. Validation gates

After each phase, before moving on:

| Gate | Pass criteria |
|---|---|
| After A1-A3 | `pytest tests/test_ingest.py` → 51/51. Editable-install `recall --version` still works. |
| After B1 | `pytest` → 51/51. `recall install --dry-run -y` runs on macOS without crashing (still macOS-only via factory). |
| After B2 | `pytest` → 51 + new polling tests. In sandbox: `recall install --scheduler polling --dry-run` runs. |
| After B3 | sandbox: `recall install --scheduler systemd -y` actually loads a real unit (now that sandbox has systemd PID 1). `systemctl --user list-units` shows it. |
| After B4 | crontab integration test passes. `crontab -l` shows the convo-recall line tagged. Uninstall removes only that line. |
| After B5-B7 | Full pytest + sandbox-e2e-full + sandbox-hooks-e2e + sandbox-linux-port-e2e all green. |
| After C1-C5 | CI runs green on `macos-latest` and `ubuntu-latest`. README accurately describes the new behavior. |

---

## 6. Risk analysis

| Risk | Likelihood | Mitigation |
|---|---|---|
| Existing 51 tests break during refactor | medium | Step A is pure file moves — no logic changes. Run pytest after every move. |
| Hidden import-cycle bugs after package split | medium | Keep `install/__init__.py` re-exporting all current public API names. CLI never knows it's a package. |
| systemd unit file syntax wrong, fails at runtime | medium | `systemd-analyze verify` runs as part of unit tests AND at install time. Catches typos before users see them. |
| Cron line filtering accidentally drops user lines | low | Use a unique tag marker per line (`# convo-recall:ingest`, `# convo-recall:embed`) and match exactly. Backup crontab before modifying. |
| pexpect test flaky on slow CI | medium | Generous timeouts. `--scheduler polling --dry-run` (no actual subprocess spawning) for the wizard test. |
| Linger handling on systemd: user logs out, watcher dies | low | Wizard explicitly asks; defaults to enabling linger via `loginctl enable-linger`. Check exit code; warn if it fails. |
| Sandbox e2e flaky if container state varies | medium | Each section bootstraps from a clean state (uninstall first, then install). |
| Path constants on Linux differ (XDG vs hardcoded) | low | All path lookups go through `_paths.py`. Single source of truth. |
| Hatch wheel doesn't include hooks/ subdir | low (already verified) | Add a smoke test: `pipx install --force <wheel>` then verify `convo_recall.hooks` resources exist. |
| GitHub Actions ubuntu runners have no systemd-user instance | high | The CI workflow does not attempt full systemd integration. It runs the unit tests (which use mocks/syntax validators). Real systemd integration is validated in claude-sandbox locally. |

---

## 7. Rollback plan

If any phase fails irrecoverably, the recovery is bounded:

| Phase | Rollback |
|---|---|
| A1-A3 | Revert by `git checkout HEAD~ src/convo_recall/install*` — all changes in one commit per phase, easy to undo |
| B1-B4 | Each new scheduler is its own file. Comment out the registration in `schedulers/__init__.py` to disable. Wizard falls back to next-tier. |
| B5-B6 | Same — file moves, easily reverted |
| C1-C5 | Optional polish; not blocking the port itself |

Concretely: each phase is a separate git commit. If phase X breaks, `git revert` the commit, fix, retry.

---

## 8. Estimated effort

| Phase | LOC | Time |
|---|---|---|
| A1-A3 (extract launchd, no behavior change) | 0 net (files moved) | 30 min |
| B1 (drop `_require_macos`, factory) | +50 | 15 min |
| B2 (PollingScheduler) | +120 + 30 tests | 45 min |
| B3 (SystemdUserScheduler) | +250 + 50 tests | 90 min |
| B4 (CronScheduler) | +150 + 40 tests | 60 min |
| B5-B7 (move hooks, wizard, uninstall) | +50 net (mostly file moves) | 45 min |
| C1 (pexpect wizard test) | +120 | 60 min |
| C2 (sandbox-linux-port-e2e.sh) | +80 | 30 min |
| C3 (CI matrix) | +20 | 15 min |
| C4 (pyproject + README) | +60 | 30 min |
| C5 (pexpect dep) | +1 | 5 min |
| **Total** | **~900 LOC + ~120 tests** | **~6 hours focused** |

---

## 9. Out of scope (deferred to v0.3.x)

- **Migration from v0.2 macOS launchd installs.** Existing users of v0.2 will see new `com.convo-recall.*` plists from v0.3 alongside their old ones. `recall uninstall` will clean both up. No proactive migration.
- **Logging unification.** macOS logs to `StandardOutPath`; Linux uses `journalctl`. README documents this as "see your scheduler's native logs." No abstraction.
- **Windows support.** Would need a fifth scheduler (`WindowsTaskScheduler` via `schtasks` or `cron-like` via WSL). Plumbing is in place to add it later.
- **Data migration from private indexer.** Separate ~50 LOC script (`recall import-from-claude-index`) — not part of this port.
- **Removing the legacy `_vc` shim.** Still in `ingest.py` for backward compatibility; can be deleted in v1.0 once all tests stop monkeypatching it.

---

## 10. What success looks like

After this port lands, the user-facing experience is:

```bash
# Linux user
$ pipx install convo-recall[embeddings]
$ recall install
convo-recall setup wizard
Detected scheduler: systemd-user (Linux native, file-event driven, survives logout if linger enabled)
Detected agents: claude (4217 files), codex (11 files)
[embeddings] extra installed: yes

? Install systemd user units so new sessions index automatically?
   ↪ if YES: A .service + .path unit pair is enabled per agent. Survives reboot if linger enabled.
   ↪ if NO:  Run `recall ingest` manually, or wire cron yourself.
   [Y/n] y

# (continues identically to macOS UX from here on)
```

```bash
# macOS user (no UX change)
$ pipx install convo-recall[embeddings]
$ recall install
convo-recall setup wizard
Detected scheduler: launchd (macOS native, file-event driven, survives reboot)
Detected agents: claude (4217 files), codex (11 files)
[embeddings] extra installed: yes

? Install launchd watchers so new sessions index automatically?
   ↪ if YES: convo-recall watches each session dir; new content indexed within ~10s.
   ↪ if NO:  Indexing won't happen automatically. Run `recall ingest` manually.
   [Y/n] y
```

```bash
# Sandbox / container user
$ recall install
convo-recall setup wizard
Detected scheduler: polling (no systemd, no cron — using nohup recall watch)
[...same prompts; consequence text says "won't survive container restart"...]
```

Same wizard UX, same prompts, same consequence-callout pattern, same hook wiring. The OS detection and scheduler details are entirely behind the curtain.
