## A: Refactor — extract install.py into install/ package

**Status:** not started
**Dependencies:** none

### Scope

Pure file-move refactor. Move single-file `src/convo_recall/install.py` into a sub-package `src/convo_recall/install/`. Add `_paths.py` for XDG-aware path constants. Extract the existing launchd code into `schedulers/launchd.py` as `LaunchdScheduler`, conforming to a new `Scheduler` ABC in `schedulers/base.py`. **No behavior change** — wizard still hard-references `LaunchdScheduler()` at this stage. All 51 existing unit tests must stay green.

### Key Components

- `install/__init__.py`: re-exports current public API (`run`, `uninstall`, `install_hooks`, `uninstall_hooks`) so `cli.py` keeps working unchanged.
- `install/_paths.py`: `is_macos()`, `is_linux()`, `scheduler_unit_dir()`, `log_dir()`, `runtime_dir()` — XDG on Linux, `~/Library/...` on macOS.
- `install/schedulers/base.py`: `Scheduler` ABC + `Result` dataclass. Defines `available()`, `install_watcher()`, `uninstall_watcher()`, `install_sidecar()`, `uninstall_sidecar()`, `describe()`, `consequence_yes()`, `consequence_no()`.
- `install/schedulers/launchd.py`: `LaunchdScheduler(Scheduler)` wrapping the existing `_ingest_plist`, `_embed_plist`, `_launchctl_load` helpers.

### Rough File Inventory

- New: 4 files (`install/__init__.py`, `install/_paths.py`, `install/schedulers/base.py`, `install/schedulers/launchd.py`)
- New: 1 dir (`install/schedulers/`)
- Modified: 1 file (`cli.py`, only if any imports break — should not)
- Removed: 1 file (`install.py` deleted; its content lives in `install/__init__.py` initially, then thinned out as code moves to subfiles)

### Risks & Blockers

- **Hatch wheel must include all `install/` and `install/schedulers/` files.** Hatch auto-includes everything under `packages = ["src/convo_recall"]`, so this should work, but add a smoke test that imports `convo_recall.install.schedulers.launchd` from a built wheel.
- **Import cycles** — keep the dependency graph one-way: `_paths` ← `schedulers/base` ← `schedulers/launchd`. `__init__.py` imports both but neither imports `__init__`.
- **Two install tests in `tests/test_ingest.py`** (`test_install_emits_one_plist_per_enabled_agent`, `test_install_plist_targets_correct_watch_dir`) reference `_install._ingest_plist` and similar helpers. Update them to point at `LaunchdScheduler` instance methods.

### Done Criteria

- [ ] `src/convo_recall/install/` package exists with the 4 new files
- [ ] `src/convo_recall/install.py` no longer exists (replaced by `install/__init__.py`)
- [ ] `LaunchdScheduler.available()` returns `True` only on macOS
- [ ] `pytest tests/test_ingest.py` → 51/51 pass
- [ ] `recall --version` runs (editable install integrity check)
- [ ] `recall install --dry-run -y` runs on macOS without crashing (still macOS-gated)
- [ ] No new public API methods exposed on `convo_recall.install` — only the 4 existing functions

### Verification Artifacts

| File | Must Contain | Why |
|------|-------------|-----|
| `src/convo_recall/install/__init__.py` | `def run(` and `def install_hooks(` | Public API preserved |
| `src/convo_recall/install/schedulers/base.py` | `class Scheduler(ABC):` | ABC contract defined |
| `src/convo_recall/install/schedulers/launchd.py` | `class LaunchdScheduler(Scheduler):` | Subclass exists |
| `src/convo_recall/install/_paths.py` | `def scheduler_unit_dir():` and `def log_dir():` | XDG-aware paths exposed |
