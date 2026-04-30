"""B4 Item 2 — verify hook wiring lives in `_hooks.py` and is still
reachable via the install facade."""

import inspect


def test_install_hooks_importable_from_module():
    from convo_recall.install._hooks import (
        _hook_block,
        _hook_target,
        _unwire_hook,
        _wire_hook,
        install_hooks,
        uninstall_hooks,
    )
    for fn in (install_hooks, uninstall_hooks, _wire_hook, _unwire_hook,
               _hook_target, _hook_block):
        assert callable(fn), f"{fn!r} must be callable"


def test_install_hooks_still_reachable_via_install_facade():
    from convo_recall.install import install_hooks, uninstall_hooks

    assert callable(install_hooks)
    assert callable(uninstall_hooks)
    src = inspect.getsourcefile(install_hooks) or ""
    assert src.endswith("_hooks.py"), (
        f"install_hooks should resolve to _hooks.py; got {src}"
    )
