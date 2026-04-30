## B1: PollingScheduler — universal fallback

**Status:** not started
**Dependencies:** A (Scheduler ABC must exist)

### Scope

Add `PollingScheduler` — the always-available fallback that spawns `recall watch &` (and `recall serve &` for the sidecar) as long-running background processes via `subprocess.Popen` with PID files for lifecycle management. Used by Linux containers without systemd or cron, and as a last-resort manual-mode option for any host. **Does not survive reboot** — wizard's consequence_no() makes this explicit.

### Key Components

- `install/schedulers/polling.py`: `PollingScheduler(Scheduler)`.
- `available()` returns `True` unconditionally (always last in the priority list).
- `install_watcher(agent, ...)`: spawns `recall watch` via `Popen(start_new_session=True)`, redirects stdout/stderr to `<log_dir>/watch.log`, writes PID to `<runtime_dir>/watch.pid`. Same single watcher serves all agents — second/third call is a no-op (returns "already covered" Result).
- `install_sidecar(...)`: same pattern with `recall serve --sock <path>`, PID file `<runtime_dir>/embed.pid`.
- `uninstall_watcher() / uninstall_sidecar()`: read PID file, `os.kill(pid, SIGTERM)` with 5s grace, then `SIGKILL` if still alive, then unlink PID file.
- `consequence_yes()`: "Backgrounded via Popen; won't survive reboot/logout — re-run install on restart."
- `consequence_no()`: "Run `recall ingest` manually after each session."

### Rough File Inventory

- New: 1 file (`install/schedulers/polling.py`)
- Modified: 1 file (`tests/test_schedulers.py` — new file with PollingScheduler tests)

### Risks & Blockers

- **`Popen(start_new_session=True)` orphans the child** — required so the child survives the parent shell. Verify the parent shell can exit and the watcher keeps running.
- **PID file race**: if two `install` runs happen in parallel, two watchers spawn. Mitigate with a stat-then-write check that asserts no live PID at the existing PID file.
- **Stale PID files** (process died, file remains). Detection: read PID, check `os.kill(pid, 0)` — if it raises `ProcessLookupError`, PID is stale, overwrite. Tests must cover this.
- **`SIGKILL` after grace period** is destructive but bounded (only kills processes we ourselves spawned).

### Done Criteria

- [ ] `PollingScheduler.available()` returns True on every platform
- [ ] `install_watcher()` spawns a process and writes a valid PID file
- [ ] Second `install_watcher()` call is a no-op
- [ ] `uninstall_watcher()` kills the process within 5 seconds and unlinks the PID file
- [ ] Stale PID file (dead process) is detected and overwritten gracefully
- [ ] Same lifecycle works for `install_sidecar` / `uninstall_sidecar`
- [ ] New unit tests pass (≥6 tests covering: spawn, idempotent re-install, SIGTERM cleanup, SIGKILL escalation, stale PID handling, log-file redirection)
- [ ] All 51 existing tests still pass

### Verification Artifacts

| File | Must Contain | Why |
|------|-------------|-----|
| `src/convo_recall/install/schedulers/polling.py` | `class PollingScheduler(Scheduler):` | Class exists |
| `src/convo_recall/install/schedulers/polling.py` | `start_new_session=True` | Detached spawn |
| `src/convo_recall/install/schedulers/polling.py` | `os.kill(` | Lifecycle handling |
| `tests/test_schedulers.py` | `def test_polling_scheduler` | Test coverage |
