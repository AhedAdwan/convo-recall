"""Item 4 — public API surface of `convo_recall.install` after the
package extraction. The wizard should still expose `run`, `uninstall`,
`install_hooks`, `uninstall_hooks`. Plist helpers (`_ingest_plist`,
`_embed_plist`, `_launchctl_load`) have moved to `LaunchdScheduler`
and must NOT remain on the install module."""

import inspect

from convo_recall import install


def test_public_api_preserved():
    for name in ("run", "uninstall", "install_hooks", "uninstall_hooks"):
        assert callable(getattr(install, name)), f"missing public API: {name}"


def test_plist_helpers_moved_off_module():
    for name in ("_ingest_plist", "_embed_plist", "_launchctl_load"):
        assert not hasattr(install, name), (
            f"{name} should have moved to LaunchdScheduler, "
            f"not stayed on convo_recall.install"
        )


def test_launchd_scheduler_exposes_helpers_as_methods():
    from convo_recall.install.schedulers.launchd import LaunchdScheduler

    s = LaunchdScheduler()
    for name in ("_ingest_plist", "_embed_plist", "_launchctl_load"):
        assert callable(getattr(s, name)), f"LaunchdScheduler missing {name}"


def test_cli_wires_install_module_unchanged():
    """`cli.py` keeps using `from . import install as _install` and calling
    `_install.run(...)`. This regression-checks that the names `cli.py`
    relies on still exist on the install module."""
    from convo_recall import cli  # noqa: F401
    assert callable(install.run)
    assert "dry_run" in inspect.signature(install.run).parameters
    assert "non_interactive" in inspect.signature(install.run).parameters
