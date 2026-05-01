"""F-24 — `_unwire_hook` must remove orphan-path entries.

Pre-fix `_unwire_hook` matched only `command == sig` where `sig` is the
CURRENT install path. If you reinstalled from a moved source dir, the
prior install's entry pointed at the old path and stayed forever — every
agent turn fired a broken hook script.

Fix: also match by suffix `convo_recall/hooks/conversation-memory.sh`,
which is a stable identifier across any install path (editable, pipx,
moved sources, etc.).
"""

import json
from pathlib import Path

import pytest

from convo_recall.install._hooks import (
    _is_convo_recall_hook,
    _unwire_hook,
)


# ── Pure-function level: matcher behavior ────────────────────────────────────


def test_is_convo_recall_hook_matches_current_path():
    sig = "/Users/foo/Projects/convo-recall/src/convo_recall/hooks/conversation-memory.sh"
    assert _is_convo_recall_hook(sig, sig) is True


def test_is_convo_recall_hook_matches_orphan_path():
    """The whole point of F-24: a different install path still gets matched."""
    current_sig = "/Users/foo/Projects/mylibs/convo-recall/src/convo_recall/hooks/conversation-memory.sh"
    orphan_cmd = "/Users/foo/Projects/libs/convo-recall/src/convo_recall/hooks/conversation-memory.sh"
    assert _is_convo_recall_hook(orphan_cmd, current_sig) is True


def test_is_convo_recall_hook_matches_pipx_install_path():
    """Wheel install via pipx puts the script under pipx's venv."""
    current_sig = "/Users/foo/.local/share/pipx/venvs/convo-recall/lib/python3.12/site-packages/convo_recall/hooks/conversation-memory.sh"
    # Switching from pipx to editable mid-test
    editable_orphan = "/Users/foo/Projects/convo-recall/src/convo_recall/hooks/conversation-memory.sh"
    assert _is_convo_recall_hook(editable_orphan, current_sig) is True


def test_is_convo_recall_hook_does_not_match_unrelated_command():
    """User's other hooks must survive."""
    sig = "/anywhere/convo_recall/hooks/conversation-memory.sh"
    for other in (
        "/usr/local/bin/my-own-hook.sh",
        "echo hello",
        "/Users/foo/.config/some-other-tool/hook.sh",
        None,
        "",
    ):
        assert _is_convo_recall_hook(other, sig) is False, other


def test_is_convo_recall_hook_handles_none_safely():
    """Settings entries can have `command: null` from broken edits — must not crash."""
    assert _is_convo_recall_hook(None, "/sig.sh") is False


# ── End-to-end: _unwire_hook removes both current AND orphan entries ─────────


@pytest.fixture
def claude_settings_with_orphan(tmp_path, monkeypatch):
    """Write a fake ~/.claude/settings.json with TWO convo-recall entries:
    one at the 'current' install path and one at an old orphan path."""
    fake_home = tmp_path
    monkeypatch.setattr(Path, "home", lambda: fake_home)

    settings_dir = fake_home / ".claude"
    settings_dir.mkdir(parents=True)
    settings_path = settings_dir / "settings.json"

    current_path = "/Users/foo/Projects/mylibs/convo-recall/src/convo_recall/hooks/conversation-memory.sh"
    orphan_path = "/Users/foo/Projects/libs/convo-recall/src/convo_recall/hooks/conversation-memory.sh"
    user_path = "/Users/foo/my-personal-hook.sh"

    settings_path.write_text(json.dumps({
        "hooks": {
            "UserPromptSubmit": [
                {"hooks": [{"type": "command", "command": current_path}]},
                {"hooks": [{"type": "command", "command": orphan_path}]},
                {"hooks": [{"type": "command", "command": user_path}]},
            ]
        }
    }, indent=2))

    return settings_path, current_path, orphan_path, user_path


def test_unwire_hook_removes_orphan_path_entry(claude_settings_with_orphan):
    settings_path, current_path, orphan_path, user_path = claude_settings_with_orphan

    changed, msg = _unwire_hook("claude", Path(current_path))
    assert changed, msg

    after = json.loads(settings_path.read_text())
    remaining = [
        h["command"]
        for group in after.get("hooks", {}).get("UserPromptSubmit", [])
        for h in group.get("hooks", [])
    ]

    # Both convo-recall entries gone
    assert current_path not in remaining, remaining
    assert orphan_path not in remaining, remaining
    # User's unrelated hook survives
    assert user_path in remaining, (
        f"F-24 regression: _unwire_hook removed an unrelated user hook: {remaining}"
    )


def test_unwire_hook_no_op_when_only_unrelated_hooks(tmp_path, monkeypatch):
    """If a CLI's settings contain ONLY hooks that aren't convo-recall, _unwire
    must leave the file untouched and return (False, no-op message)."""
    fake_home = tmp_path
    monkeypatch.setattr(Path, "home", lambda: fake_home)

    settings_dir = fake_home / ".claude"
    settings_dir.mkdir(parents=True)
    settings_path = settings_dir / "settings.json"

    user_path = "/Users/foo/my-personal-hook.sh"
    original = {
        "hooks": {
            "UserPromptSubmit": [
                {"hooks": [{"type": "command", "command": user_path}]},
            ]
        }
    }
    settings_path.write_text(json.dumps(original, indent=2))

    changed, msg = _unwire_hook("claude", Path("/anywhere/convo_recall/hooks/conversation-memory.sh"))
    assert not changed
    assert "nothing to remove" in msg

    after = json.loads(settings_path.read_text())
    assert after == original
