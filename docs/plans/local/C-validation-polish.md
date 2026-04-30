## C: Validation + polish — pexpect, sandbox e2e, CI matrix, docs

**Status:** not started
**Dependencies:** B4 (full wizard rewire complete)

### Scope

Lock the cross-platform port behind comprehensive tests and bring docs/CI into sync. Add a pexpect-driven wizard test that runs in CI on both `macos-latest` and `ubuntu-latest`. Add a sandbox-only e2e script that exercises the polling and systemd tiers end-to-end. Update GitHub Actions workflow for the OS matrix. Update pyproject classifier to drop "MacOS X" exclusivity. Rewrite README's Requirements / Install / Schedulers sections to reflect Linux support.

### Key Components

- `tests/test_install_wizard.py`: pexpect-driven test of the real `recall install` binary. Drives y/n flow, asserts each prompt's consequence text appears, asserts `--scheduler polling --dry-run` exits 0. Tests both happy path and "decline-watchers" branch. Marked `@pytest.mark.skipif(not shutil.which("recall"))` so missing-binary environments skip instead of failing.
- `tests/sandbox-linux-port-e2e.sh`: 6-section script analogous to existing `sandbox-e2e-full.sh`. Sections: 1) polling --dry-run, 2) polling -y full lifecycle (start, verify PID, kill, verify cleanup), 3) systemd --dry-run, 4) systemd -y full lifecycle (load unit, verify list-units, uninstall), 5) cron --dry-run, 6) auto-detection (no `--scheduler` flag) picks systemd in this sandbox.
- `.github/workflows/test.yml`: matrix `[macos-latest, ubuntu-latest]`. Existing pytest job runs on both.
- `pyproject.toml`: drop `Operating System :: MacOS :: MacOS X` classifier; add `Operating System :: POSIX :: Linux` and `Operating System :: OS Independent`. Add `pexpect` to `[project.optional-dependencies] dev`.
- `README.md`:
  - Rewrite "Requirements" — drop "macOS only" line; mention Linux + macOS
  - New "Schedulers" subsection explaining the auto-detection tier ladder
  - Update "Install" section so the example works on Linux too
  - Add note: on Linux, run `loginctl enable-linger $USER` if you want watchers to survive logout
- `CHANGELOG.md`: add v0.3.0 unreleased entry summarizing the port.

### Rough File Inventory

- New: 2 files (`tests/test_install_wizard.py`, `tests/sandbox-linux-port-e2e.sh`)
- Modified: 4 files (`.github/workflows/test.yml`, `pyproject.toml`, `README.md`, `CHANGELOG.md`)

### Risks & Blockers

- **pexpect on `macos-latest`**: bundled with Python on most macOS GH runners, but not always. Add `pexpect` to dev deps so `pip install -e .[dev]` includes it.
- **CI test time** roughly doubles with the OS matrix. Acceptable — CI was fast before (~30s per run).
- **Sandbox e2e is local-only** — not run in CI (claude-sandbox isn't reproducible there). Run manually after each phase.
- **README accuracy under maintenance** — once it claims Linux support, it has to keep being true. Add a CI badge or matrix-status note pointing at the workflow result.
- **pexpect timing flakiness** on a slow CI runner. Use generous timeouts (10s default per `expect`) and `--scheduler polling --dry-run` (no real subprocess spawn) to avoid platform-specific scheduling quirks.

### Done Criteria

- [ ] `tests/test_install_wizard.py` runs locally and in CI on both OSes
- [ ] At least 4 wizard test cases: full-yes flow, decline-watchers consequence, decline-hooks consequence, abort-at-final-confirm
- [ ] `tests/sandbox-linux-port-e2e.sh` runs in claude-sandbox; all 6 sections pass
- [ ] GitHub Actions workflow runs the existing pytest job on both `macos-latest` and `ubuntu-latest`; both green
- [ ] `pyproject.toml` lists Linux as a supported OS classifier; `pexpect` added to `[dev]` extras
- [ ] README's Requirements section mentions Linux; new Schedulers subsection documents the tier ladder
- [ ] CHANGELOG.md has a v0.3.0 unreleased entry covering: cross-platform install, scheduler abstraction, --scheduler flag, Linux schedulers (systemd/cron/polling), pexpect tests, CI matrix
- [ ] All previously-passing tests still pass

### Verification Artifacts

| File | Must Contain | Why |
|------|-------------|-----|
| `tests/test_install_wizard.py` | `import pexpect` | pexpect-based |
| `tests/test_install_wizard.py` | `def test_wizard` | Test cases exist |
| `tests/sandbox-linux-port-e2e.sh` | `--scheduler systemd` | Systemd path covered |
| `tests/sandbox-linux-port-e2e.sh` | `--scheduler polling` | Polling path covered |
| `.github/workflows/test.yml` | `ubuntu-latest` | Linux matrix entry |
| `pyproject.toml` | `Operating System :: POSIX :: Linux` | Classifier updated |
| `pyproject.toml` | `pexpect` | Dev dep added |
| `README.md` | `## Schedulers` or `### Schedulers` | New docs section |
| `CHANGELOG.md` | `## [0.3.0]` or `Unreleased.*Linux` | Release notes entry |
