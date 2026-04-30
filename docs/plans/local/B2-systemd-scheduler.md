## B2: SystemdUserScheduler — Linux native

**Status:** not started
**Dependencies:** A (Scheduler ABC must exist)

### Scope

Add `SystemdUserScheduler` — the production Linux equivalent of macOS launchd. Generates `.service` + `.path` unit pairs per agent (file-event driven, like launchd's `WatchPaths`), manages them via `systemctl --user`. Includes lingering setup (`loginctl enable-linger $USER`) so watchers survive logout. Validates generated unit syntax with `systemd-analyze verify` at install time.

### Key Components

- `install/schedulers/systemd.py`: `SystemdUserScheduler(Scheduler)`.
- `available()`: runs `systemctl --user --version` AND `systemctl --user is-system-running` succeeds with output containing `running` / `degraded` / `starting`. Returns False on `offline`, `command not found`, `subprocess.TimeoutExpired`. Probes `loginctl` availability too.
- Unit-file generators:
  - `_service_unit(agent, recall_bin, env_vars)` → `[Unit]` + `[Service]` (`Type=oneshot`, `ExecStart=...`, env exported) + `[Install]` (`WantedBy=default.target`).
  - `_path_unit(agent, watch_dir)` → `[Path]` (`PathChanged=...`, `PathModified=...`) + `[Install]`.
  - `_sidecar_service(recall_bin, sock_path)` → `[Service]` with `Restart=always`, `Type=simple`.
- `install_watcher(agent, ...)`: write both files to `~/.config/systemd/user/com.convo-recall.ingest.<agent>.{service,path}`, call `systemd-analyze verify <files>`, then `systemctl --user daemon-reload` + `systemctl --user enable --now com.convo-recall.ingest.<agent>.path`.
- `uninstall_watcher(agent)`: `systemctl --user disable --now <unit>`, unlink both unit files, `daemon-reload`.
- Same pattern for sidecar (single `.service` unit, no `.path`).
- `enable_linger()` helper — only called when wizard explicitly requests it.
- `consequence_yes()`: "A `.service` + `.path` unit pair is enabled per agent. Survives reboot if linger is enabled."
- `consequence_no()`: "Run `recall ingest` manually, or wire cron yourself."

### Rough File Inventory

- New: 1 file (`install/schedulers/systemd.py`)
- Modified: 1 file (`tests/test_schedulers.py` — add SystemdUserScheduler tests)

### Risks & Blockers

- **Detection accuracy**: a host where `systemctl --user --version` succeeds but the user instance is offline (no login session) currently produces "Failed to connect to bus" at runtime. `available()` must call `is-system-running` AND check exit code, not just `--version`.
- **`systemd-analyze verify` exit code**: returns 0 even with warnings; check stderr for any output before treating as pass. Fail loud — let the wizard surface the error so users see it before unit loading.
- **Lingering**: `loginctl enable-linger $USER` requires polkit/sudo on some distros. Wrap in subprocess + check exit; on failure, surface a warning that "watchers will die at logout — fix with `sudo loginctl enable-linger $USER`."
- **systemd version compatibility**: `Type=oneshot` and `Path` units have been stable since systemd 200+, so any Ubuntu/Fedora supported release works.
- **Path unit substitutions** (`%h`): make sure these resolve. They expand via systemd at load time, not at generate time, so we should NOT pre-resolve `Path.home()` — keep `%h` literal in the unit file.

### Done Criteria

- [ ] `SystemdUserScheduler.available()` correctly returns True only when both `systemctl --user --version` and `is-system-running` succeed
- [ ] Generated `.service` unit passes `systemd-analyze verify` (no warnings on stderr)
- [ ] Generated `.path` unit passes `systemd-analyze verify`
- [ ] `install_watcher()` writes 2 files, daemon-reloads, enables, starts the path unit
- [ ] `uninstall_watcher()` disables, removes both files, daemon-reloads
- [ ] Sandbox e2e: install a watcher; verify `systemctl --user list-units` shows it; uninstall; verify it's gone
- [ ] New unit tests pass (≥8 tests: detection True/False matrix, unit syntax pass/fail, lifecycle round-trip, linger handling)
- [ ] All 51 existing tests still pass

### Verification Artifacts

| File | Must Contain | Why |
|------|-------------|-----|
| `src/convo_recall/install/schedulers/systemd.py` | `class SystemdUserScheduler(Scheduler):` | Class exists |
| `src/convo_recall/install/schedulers/systemd.py` | `is-system-running` | Detection guard |
| `src/convo_recall/install/schedulers/systemd.py` | `systemd-analyze verify` | Pre-load syntax validation |
| `src/convo_recall/install/schedulers/systemd.py` | `enable-linger` | Logout-survival path |
| `tests/test_schedulers.py` | `def test_systemd` | Test coverage |
