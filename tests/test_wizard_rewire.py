"""B4 Item 3 — wizard talks to a Scheduler instance and adapts prompts."""

import io
import platform
import sys
from contextlib import redirect_stdout

import pytest

from convo_recall.install import _wizard
from convo_recall.install.schedulers import (
    CronScheduler,
    LaunchdScheduler,
    PollingScheduler,
    SystemdUserScheduler,
)


def _stub_scheduler_install_methods(monkeypatch):
    """Make every scheduler's install_* / uninstall_* a no-op that returns
    a benign Result, so dry_run tests never touch real launchd / systemd."""
    from convo_recall.install.schedulers.base import Result

    def fake_install_watcher(self, *a, **kw):
        return Result(ok=True, message="stubbed", path=None)

    def fake_install_sidecar(self, *a, **kw):
        return Result(ok=True, message="stubbed", path=None)

    for cls in (LaunchdScheduler, SystemdUserScheduler,
                CronScheduler, PollingScheduler):
        monkeypatch.setattr(cls, "install_watcher", fake_install_watcher)
        monkeypatch.setattr(cls, "install_sidecar", fake_install_sidecar)


def test_wizard_picks_polling_when_only_polling_available(monkeypatch):
    monkeypatch.setattr(LaunchdScheduler, "available", lambda self: False)
    monkeypatch.setattr(SystemdUserScheduler, "available", lambda self: False)
    monkeypatch.setattr(CronScheduler, "available", lambda self: False)
    _stub_scheduler_install_methods(monkeypatch)

    buf = io.StringIO()
    with redirect_stdout(buf):
        _wizard.run(non_interactive=True, dry_run=True, scheduler="auto")
    output = buf.getvalue()
    assert "polling (Popen fallback)" in output


def test_wizard_explicit_scheduler_override(monkeypatch):
    _stub_scheduler_install_methods(monkeypatch)
    buf = io.StringIO()
    with redirect_stdout(buf):
        _wizard.run(non_interactive=True, dry_run=True, scheduler="polling")
    output = buf.getvalue()
    assert "polling (Popen fallback)" in output
    assert "launchd (macOS)" not in output


def test_wizard_unknown_scheduler_raises_value_error(monkeypatch):
    with pytest.raises(ValueError, match="bogus"):
        _wizard.run(non_interactive=True, dry_run=True, scheduler="bogus")


def test_wizard_uses_scheduler_consequences_in_step1_prompt(monkeypatch):
    _stub_scheduler_install_methods(monkeypatch)
    buf = io.StringIO()
    with redirect_stdout(buf):
        _wizard.run(non_interactive=True, dry_run=True, scheduler="cron")
    output = buf.getvalue()
    assert "@reboot" in output
    # Launchd's consequence text MUST NOT appear when cron was selected.
    assert "indexed within ~10s" not in output


def test_wizard_systemd_path_asks_about_linger(monkeypatch):
    """Force-pick systemd via explicit name; capture every _ask question."""
    _stub_scheduler_install_methods(monkeypatch)

    asked: list[str] = []

    def recording_ask(question, *, default=True, if_yes=None, if_no=None,
                      non_interactive=False):
        asked.append(question)
        return True

    monkeypatch.setattr(_wizard, "_ask", recording_ask)
    monkeypatch.setattr(SystemdUserScheduler, "available", lambda self: True)

    buf = io.StringIO()
    with redirect_stdout(buf):
        _wizard.run(non_interactive=False, dry_run=True, scheduler="systemd")

    assert any(q.startswith("Keep watchers running when logged out") for q in asked), (
        f"linger question missing from {asked}"
    )


def test_wizard_macos_path_does_not_ask_about_linger(monkeypatch):
    _stub_scheduler_install_methods(monkeypatch)
    asked: list[str] = []

    def recording_ask(question, *, default=True, if_yes=None, if_no=None,
                      non_interactive=False):
        asked.append(question)
        return True

    monkeypatch.setattr(_wizard, "_ask", recording_ask)
    monkeypatch.setattr(LaunchdScheduler, "available", lambda self: True)

    buf = io.StringIO()
    with redirect_stdout(buf):
        _wizard.run(non_interactive=False, dry_run=True, scheduler="launchd")

    assert not any("linger" in q for q in asked), (
        f"linger question should not fire for launchd; got {asked}"
    )


# ── F-13: apply-phase order — sidecar → ingest → backfill → watchers ─────────


def test_wizard_apply_order_sidecar_before_backfill(monkeypatch, tmp_path):
    """The F-13 bug: pre-fix wizard ran embed-backfill BEFORE installing
    the sidecar, so backfill always failed with 'Embed socket not found'.
    Lock the correct order: sidecar install must happen before any
    subprocess.run('embed-backfill') call, AND watchers must install
    LAST (to preserve F-3's no-race-with-ingest property)."""
    from convo_recall.install.schedulers.base import Result
    from convo_recall.install.schedulers.polling import PollingScheduler

    # Track every install_sidecar / install_watcher call + every subprocess.run
    events: list[str] = []

    def _fake_install_sidecar(self, *a, **kw):
        events.append("install_sidecar")
        return Result(ok=True, message="stub", path=None)

    def _fake_install_watcher(self, *a, **kw):
        events.append(f"install_watcher({kw.get('agent', a[0] if a else '?')})")
        return Result(ok=True, message="stub", path=None)

    def _fake_subprocess_run(args, *a, **kw):
        events.append(f"subprocess.run({args[1]})")
        # Pretend the sidecar socket appears partway through, after
        # install_sidecar fired. Tests that pre-fix wizard didn't rely on
        # this; it raced the install. Post-fix, the wizard polls.
        from convo_recall.install import SOCK_PATH
        SOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
        SOCK_PATH.touch(exist_ok=True)
        class _R:
            returncode = 0
        return _R()

    monkeypatch.setattr(PollingScheduler, "install_sidecar", _fake_install_sidecar)
    monkeypatch.setattr(PollingScheduler, "install_watcher", _fake_install_watcher)
    monkeypatch.setattr(_wizard, "subprocess", type("S", (), {"run": staticmethod(_fake_subprocess_run)}))

    # CI installs only [dev], not [embeddings] — fake the extra as present so
    # the wizard takes the sidecar branch under test.
    monkeypatch.setattr(_wizard, "_check_embeddings_installed", lambda: True)

    # Make the socket initially absent. The wizard's poll loop should see
    # it appear after install_sidecar runs (via fake_install_sidecar
    # creating it through subprocess.run side-effect — except, we're not
    # actually wiring that... so test polling tolerance instead.)
    from convo_recall.install import SOCK_PATH
    if SOCK_PATH.exists():
        SOCK_PATH.unlink()

    # Make install_sidecar create the socket, simulating a fast warmup
    def _fake_install_sidecar_with_socket(self, *a, **kw):
        events.append("install_sidecar")
        SOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
        SOCK_PATH.touch(exist_ok=True)
        return Result(ok=True, message="stub", path=None)

    monkeypatch.setattr(PollingScheduler, "install_sidecar", _fake_install_sidecar_with_socket)

    # Stub _ask to accept all yeses (non-interactive flow)
    monkeypatch.setattr(_wizard, "_ask", lambda *a, **kw: True)

    try:
        _wizard.run(non_interactive=True, dry_run=False, scheduler="polling",
                    with_embeddings=True)
    finally:
        # Clean up the test socket if present
        if SOCK_PATH.exists():
            try: SOCK_PATH.unlink()
            except OSError: pass

    # Find positions of the key markers
    def _idx(prefix):
        for i, e in enumerate(events):
            if e.startswith(prefix):
                return i
        return -1

    sidecar_at = _idx("install_sidecar")
    ingest_at = _idx("subprocess.run(ingest)")
    backfill_at = _idx("subprocess.run(embed-backfill)")
    watcher_at = _idx("install_watcher")

    # All four should have happened
    assert sidecar_at >= 0, f"install_sidecar never called; events={events}"
    assert ingest_at >= 0, f"ingest never called; events={events}"
    assert backfill_at >= 0, f"embed-backfill never called; events={events}"
    assert watcher_at >= 0, f"install_watcher never called; events={events}"

    # F-13 invariant: sidecar BEFORE backfill (the bug we just fixed)
    assert sidecar_at < backfill_at, (
        f"F-13 regression: install_sidecar at index {sidecar_at} but "
        f"embed-backfill at {backfill_at}; sidecar must come first.\n"
        f"events: {events}"
    )

    # F-3 invariant: watcher LAST (after ingest, to avoid DB race)
    assert ingest_at < watcher_at, (
        f"F-3 regression: install_watcher at {watcher_at} but ingest at "
        f"{ingest_at}; watcher must come AFTER ingest.\nevents: {events}"
    )

    # Sanity: the full canonical order
    assert sidecar_at < ingest_at < backfill_at < watcher_at, (
        f"apply phase out of order. expected: sidecar < ingest < backfill < watcher\n"
        f"got: sidecar={sidecar_at} ingest={ingest_at} backfill={backfill_at} "
        f"watcher={watcher_at}\nevents: {events}"
    )


def test_wizard_skips_backfill_if_sidecar_doesnt_appear(monkeypatch, tmp_path):
    """If install_sidecar succeeds but the socket never appears within the
    30s timeout (e.g., model fails to load), backfill should be skipped
    cleanly with a guidance message rather than running against no
    sidecar. Same shape as F-10's read-only fallback — degrade gracefully."""
    from convo_recall.install.schedulers.base import Result
    from convo_recall.install.schedulers.polling import PollingScheduler
    from convo_recall.install import SOCK_PATH

    events: list[str] = []

    def _fake_install_sidecar(self, *a, **kw):
        events.append("install_sidecar")
        return Result(ok=True, message="stub", path=None)

    def _fake_install_watcher(self, *a, **kw):
        events.append("install_watcher")
        return Result(ok=True, message="stub", path=None)

    def _fake_subprocess_run(args, *a, **kw):
        events.append(f"subprocess.run({args[1]})")
        class _R:
            returncode = 0
        return _R()

    # Speed up the timeout: monkey-patch time.monotonic so 30s elapses fast
    import time as _time
    real_monotonic = _time.monotonic
    base = real_monotonic()
    fake_clock = [base]
    def _fake_monotonic():
        fake_clock[0] += 5  # advance 5s per call
        return fake_clock[0]
    monkeypatch.setattr(_time, "monotonic", _fake_monotonic)
    monkeypatch.setattr(_time, "sleep", lambda s: None)

    monkeypatch.setattr(PollingScheduler, "install_sidecar", _fake_install_sidecar)
    monkeypatch.setattr(PollingScheduler, "install_watcher", _fake_install_watcher)
    monkeypatch.setattr(_wizard, "subprocess", type("S", (), {"run": staticmethod(_fake_subprocess_run)}))
    monkeypatch.setattr(_wizard, "_ask", lambda *a, **kw: True)

    # CI installs only [dev], not [embeddings] — fake the extra as present so
    # the wizard takes the sidecar branch under test.
    monkeypatch.setattr(_wizard, "_check_embeddings_installed", lambda: True)

    # Ensure the socket is absent so the poll loop will time out
    if SOCK_PATH.exists():
        SOCK_PATH.unlink()

    buf = io.StringIO()
    with redirect_stdout(buf):
        _wizard.run(non_interactive=True, dry_run=False, scheduler="polling",
                    with_embeddings=True)
    out = buf.getvalue()

    # Backfill should NOT have been invoked (we waited 30s, socket never came)
    assert not any("subprocess.run(embed-backfill)" in e for e in events), (
        f"backfill should have been skipped; events: {events}"
    )
    assert "didn't come up within 30s" in out
