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
    a benign Result, so dry_run tests never touch real launchd / systemd.

    Also stubs `_find_recall_bin` so tests don't fail when run in an env
    where the `recall` binary isn't on PATH (e.g. pytest run before
    `pipx install`)."""
    from convo_recall.install.schedulers.base import Result

    def fake_install_watcher(self, *a, **kw):
        return Result(ok=True, message="stubbed", path=None)

    def fake_install_sidecar(self, *a, **kw):
        return Result(ok=True, message="stubbed", path=None)

    for cls in (LaunchdScheduler, SystemdUserScheduler,
                CronScheduler, PollingScheduler):
        monkeypatch.setattr(cls, "install_watcher", fake_install_watcher)
        monkeypatch.setattr(cls, "install_sidecar", fake_install_sidecar)

    monkeypatch.setattr(_wizard, "_find_recall_bin", lambda: "/fake/bin/recall")


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


def test_wizard_apply_order_sidecar_before_backfill_chain(monkeypatch, tmp_path):
    """F-13 ordering invariant — adapted for the new background-backfill
    architecture. Pre-fix wizard ran embed-backfill BEFORE installing the
    sidecar so backfill always failed with 'Embed socket not found'. Now
    ingest+backfill are spawned as a single detached `_backfill-chain`
    Popen, but the same ordering invariant must hold: sidecar install
    must precede the Popen, AND watcher install must follow it (F-3:
    watchers come last, so the DB is already populated and there's no
    WAL-write race with the watcher's first scan)."""
    from convo_recall.install.schedulers.base import Result
    from convo_recall.install.schedulers.polling import PollingScheduler

    events: list[str] = []

    def _fake_install_sidecar(self, *a, **kw):
        events.append("install_sidecar")
        return Result(ok=True, message="stub", path=None)

    def _fake_install_watcher(self, *a, **kw):
        events.append(f"install_watcher({kw.get('agent', a[0] if a else '?')})")
        return Result(ok=True, message="stub", path=None)

    class _FakePopen:
        def __init__(self, args, **kwargs):
            cmd = args[1] if len(args) > 1 else "?"
            events.append(f"Popen({cmd})")
            self.pid = 11111
        def wait(self):
            return 0

    def _fake_run(args, *a, **kw):
        cmd = args[1] if len(args) > 1 else "?"
        events.append(f"run({cmd})")
        class _R:
            returncode = 0
        return _R()

    fake_subprocess = type("S", (), {
        "run": staticmethod(_fake_run),
        "Popen": _FakePopen,
        "DEVNULL": -3,
        "STDOUT": -2,
    })
    monkeypatch.setattr(_wizard, "subprocess", fake_subprocess)
    monkeypatch.setattr(PollingScheduler, "install_sidecar", _fake_install_sidecar)
    monkeypatch.setattr(PollingScheduler, "install_watcher", _fake_install_watcher)
    monkeypatch.setattr(_wizard, "_find_recall_bin", lambda: "/fake/bin/recall")
    monkeypatch.setattr(_wizard, "_check_embeddings_installed", lambda: True)
    monkeypatch.setattr(_wizard, "_ask", lambda *a, **kw: True)

    _wizard.run(non_interactive=True, dry_run=False, scheduler="polling",
                with_embeddings=True)

    def _idx(token):
        for i, e in enumerate(events):
            if token in e:
                return i
        return -1

    sidecar_at = _idx("install_sidecar")
    chain_at = _idx("_backfill-chain")  # the detached spawn
    watcher_at = _idx("install_watcher")

    assert sidecar_at >= 0, f"install_sidecar never called; events={events}"
    assert chain_at >= 0, f"_backfill-chain Popen never spawned; events={events}"
    assert watcher_at >= 0, f"install_watcher never called; events={events}"

    # F-13: sidecar BEFORE the backfill chain
    assert sidecar_at < chain_at, (
        f"F-13 regression: install_sidecar at {sidecar_at}, _backfill-chain "
        f"spawn at {chain_at}; sidecar must precede the chain.\n"
        f"events: {events}"
    )

    # F-3: watchers AFTER the chain spawn (chain triggers ingest into DB,
    # watcher install must wait until DB is reachable / not racing).
    assert chain_at < watcher_at, (
        f"F-3 regression: _backfill-chain spawn at {chain_at}, "
        f"install_watcher at {watcher_at}; watcher must come last.\n"
        f"events: {events}"
    )


def test_wizard_spawns_backfill_chain_detached(monkeypatch, tmp_path):
    """Pre-fix the wizard ran ingest + embed-backfill SYNCHRONOUSLY,
    blocking 10-30 min on a large corpus. New flow: spawn a detached
    `recall _backfill-chain` and exit. Assert the wizard calls Popen
    (not run) for the backfill, with start_new_session=True so the
    child survives wizard exit."""
    from convo_recall.install.schedulers.base import Result
    from convo_recall.install.schedulers.polling import PollingScheduler

    popen_calls: list[dict] = []
    run_calls: list[list] = []

    class _FakePopen:
        def __init__(self, args, **kwargs):
            popen_calls.append({"args": list(args), **kwargs})
            self.pid = 99999
        def wait(self):
            return 0

    def _fake_run(args, *a, **kw):
        run_calls.append(list(args))
        class _R:
            returncode = 0
        return _R()

    fake_subprocess = type("S", (), {
        "run": staticmethod(_fake_run),
        "Popen": _FakePopen,
        "DEVNULL": -3,
        "STDOUT": -2,
    })
    monkeypatch.setattr(_wizard, "subprocess", fake_subprocess)
    monkeypatch.setattr(PollingScheduler, "install_sidecar",
                        lambda self, *a, **kw: Result(ok=True, message="stub", path=None))
    monkeypatch.setattr(PollingScheduler, "install_watcher",
                        lambda self, *a, **kw: Result(ok=True, message="stub", path=None))
    monkeypatch.setattr(_wizard, "_check_embeddings_installed", lambda: True)
    monkeypatch.setattr(_wizard, "_ask", lambda *a, **kw: True)
    monkeypatch.setattr(_wizard, "_find_recall_bin", lambda: "/fake/bin/recall")

    _wizard.run(non_interactive=True, dry_run=False, scheduler="polling",
                with_embeddings=True)

    # The backfill chain MUST be spawned via Popen, not run().
    chain_popens = [c for c in popen_calls
                    if any("_backfill-chain" in str(a) for a in c["args"])]
    assert len(chain_popens) == 1, (
        f"expected exactly one detached Popen for _backfill-chain; "
        f"got {len(chain_popens)}: {popen_calls}"
    )
    # And it MUST be detached so the wizard can exit while it runs.
    assert chain_popens[0].get("start_new_session") is True, (
        f"backfill chain must be spawned with start_new_session=True so "
        f"it survives wizard exit; got: {chain_popens[0]}"
    )
    # Pre-fix asserted: regression guard — synchronous run([..., 'embed-backfill'])
    # must NOT happen anymore.
    bad = [c for c in run_calls if any("embed-backfill" in str(a) for a in c)]
    assert not bad, f"embed-backfill must not run synchronously; got: {bad}"


def test_wizard_announces_background_job_to_user(monkeypatch, tmp_path):
    """Wizard exits quickly after spawning the detached backfill chain.
    The user must see a clear "running in background" message + how to
    check progress, otherwise the install looks like it's done nothing."""
    from convo_recall.install.schedulers.base import Result
    from convo_recall.install.schedulers.polling import PollingScheduler

    class _FakePopen:
        def __init__(self, args, **kwargs):
            self.pid = 12345
        def wait(self):
            return 0

    def _fake_run(args, *a, **kw):
        class _R:
            returncode = 0
        return _R()

    fake_subprocess = type("S", (), {
        "run": staticmethod(_fake_run),
        "Popen": _FakePopen,
        "DEVNULL": -3,
        "STDOUT": -2,
    })
    monkeypatch.setattr(_wizard, "subprocess", fake_subprocess)
    monkeypatch.setattr(PollingScheduler, "install_sidecar",
                        lambda self, *a, **kw: Result(ok=True, message="stub", path=None))
    monkeypatch.setattr(PollingScheduler, "install_watcher",
                        lambda self, *a, **kw: Result(ok=True, message="stub", path=None))
    monkeypatch.setattr(_wizard, "_check_embeddings_installed", lambda: True)
    monkeypatch.setattr(_wizard, "_ask", lambda *a, **kw: True)
    monkeypatch.setattr(_wizard, "_find_recall_bin", lambda: "/fake/bin/recall")

    buf = io.StringIO()
    with redirect_stdout(buf):
        _wizard.run(non_interactive=True, dry_run=False, scheduler="polling",
                    with_embeddings=True)
    out = buf.getvalue()

    # User-facing announcement
    assert "background" in out.lower(), out
    # Discoverability: tells user how to inspect progress
    assert "recall stats" in out, out
    # PID shown so the user can find/kill the process if needed
    assert "12345" in out, out
