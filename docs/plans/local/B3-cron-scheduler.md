## B3: CronScheduler — Linux without systemd

**Status:** not started
**Dependencies:** A (Scheduler ABC must exist)

### Scope

Add `CronScheduler` — fallback for Linux hosts that lack systemd-user (e.g., older distros, minimal containers, BSD-leaning setups). Uses the user crontab with `@reboot` lines that spawn `recall watch` and `recall serve` at boot. Tagged-line filtering on uninstall preserves the user's other crontab entries.

### Key Components

- `install/schedulers/cron.py`: `CronScheduler(Scheduler)`.
- `available()`: `crontab -l` returns exit code 0 (existing crontab) or 1 ("no crontab for user" — still usable). Returns False on command not found.
- Cron lines (with unique tags for round-trip identification):
  ```
  @reboot {recall} watch >> {log_dir}/watch.log 2>&1  # convo-recall:watch
  @reboot nohup {recall} serve --sock {sock} > {log_dir}/embed.log 2>&1 &  # convo-recall:embed
  ```
- `install_watcher()`: not per-agent — installs the single `@reboot recall watch` line on the FIRST call. Subsequent calls (for additional agents) are no-ops, with a Result message "already covered by polling watcher".
- `install_sidecar()`: appends the second tagged line.
- `uninstall_*()`: read crontab, filter out lines matching `# convo-recall:<purpose>`, reinstall via `crontab -`. Backup the original crontab to `<runtime_dir>/crontab.bak.<unix-ts>` before any modification.
- `consequence_yes()`: "Cron `@reboot` line spawns `recall watch` at boot. Polling-based; reaction time depends on `recall watch`'s 10s tick."
- `consequence_no()`: "Indexing won't run automatically; use `recall ingest` manually."

### Rough File Inventory

- New: 1 file (`install/schedulers/cron.py`)
- Modified: 1 file (`tests/test_schedulers.py` — add CronScheduler tests)

### Risks & Blockers

- **Tag collision**: must use a tag unique enough to never appear in user lines. Use ` # convo-recall:` (with leading space) so a substring search won't false-match comments. Test: a user line containing the string "convo-recall" in its content (e.g., `0 * * * * touch /tmp/convo-recall-test`) must NOT be filtered out — only lines literally ending with the tag are.
- **Empty crontab**: `crontab -l` returns exit 1 with "no crontab for user" on stderr. Treat as empty input, don't crash. After `crontab -` install, `crontab -l` will succeed.
- **`crontab -` removes everything not on stdin**. Always read existing crontab first, append our line, then write back the merged content.
- **No native cron on macOS by default in newer versions** — but CronScheduler is gated on `available()` checking `crontab -l` exit code, so it skips itself on hosts without cron.
- **`@reboot` doesn't fire if the user isn't logged in** on systemd-managed hosts — but those hosts would have picked SystemdUserScheduler first.

### Done Criteria

- [ ] `CronScheduler.available()` returns True when `crontab -l` exits 0 or 1
- [ ] `install_watcher()` (first call) appends `@reboot recall watch ... # convo-recall:watch` line; preserves all existing crontab lines
- [ ] `install_watcher()` (second call, different agent) is a no-op
- [ ] `install_sidecar()` appends the embed line
- [ ] `uninstall_watcher()` removes ONLY the `# convo-recall:watch` line; user's other lines remain
- [ ] User line containing the substring "convo-recall" but not the tag is preserved
- [ ] Backup file written before any modification
- [ ] New unit tests pass (≥6 tests: detection, install, idempotent re-install, uninstall preserves user lines, false-match negative test, backup creation)
- [ ] All 51 existing tests still pass

### Verification Artifacts

| File | Must Contain | Why |
|------|-------------|-----|
| `src/convo_recall/install/schedulers/cron.py` | `class CronScheduler(Scheduler):` | Class exists |
| `src/convo_recall/install/schedulers/cron.py` | `# convo-recall:` | Tag marker convention |
| `src/convo_recall/install/schedulers/cron.py` | `@reboot` | Cron line shape |
| `tests/test_schedulers.py` | `def test_cron` | Test coverage |
