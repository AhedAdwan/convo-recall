"""Hook wiring tests — `_hooks.py` extraction + ingest-kind parameterization."""

import inspect
import json
from pathlib import Path

import pytest


# ── B4 Item 2 — extraction sanity ───────────────────────────────────────────


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


# ── H02 — _hook_target with hook_kind parameter ─────────────────────────────


def test_hook_target_returns_stop_for_claude_ingest():
    from convo_recall.install._hooks import _hook_target
    path, event, label = _hook_target("claude", "ingest")
    assert event == "Stop"
    assert label == "claude"
    assert str(path).endswith(".claude/settings.json")


def test_hook_target_returns_stop_for_codex_ingest():
    from convo_recall.install._hooks import _hook_target
    _, event, label = _hook_target("codex", "ingest")
    assert event == "Stop"
    assert label == "codex"


def test_hook_target_returns_after_agent_for_gemini_ingest():
    from convo_recall.install._hooks import _hook_target
    _, event, label = _hook_target("gemini", "ingest")
    assert event == "AfterAgent"
    assert label == "gemini"


def test_hook_target_memory_kind_is_default_and_unchanged():
    from convo_recall.install._hooks import _hook_target
    _, event_default, _ = _hook_target("claude")
    _, event_explicit, _ = _hook_target("claude", "memory")
    assert event_default == event_explicit == "UserPromptSubmit"


def test_find_hook_script_locates_both_kinds():
    from convo_recall.install._hooks import _find_hook_script
    mem = _find_hook_script("memory")
    ing = _find_hook_script("ingest")
    assert mem.name == "conversation-memory.sh"
    assert ing.name == "conversation-ingest.sh"
    assert mem.is_file() and ing.is_file()


# ── H03 — _ensure_codex_hooks_feature_flag ──────────────────────────────────


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    return tmp_path


def test_ensure_codex_flag_fresh_write(fake_home):
    from convo_recall.install._hooks import _ensure_codex_hooks_feature_flag
    ok, msg = _ensure_codex_hooks_feature_flag()
    assert ok
    config = fake_home / ".codex" / "config.toml"
    assert config.exists()
    text = config.read_text()
    assert "[features]" in text
    assert "codex_hooks = true" in text


def test_ensure_codex_flag_existing_features_section(fake_home):
    from convo_recall.install._hooks import _ensure_codex_hooks_feature_flag
    config = fake_home / ".codex" / "config.toml"
    config.parent.mkdir(parents=True)
    config.write_text("[features]\nother_flag = false\n")
    ok, msg = _ensure_codex_hooks_feature_flag()
    assert ok
    text = config.read_text()
    assert "codex_hooks = true" in text
    assert "other_flag = false" in text


def test_ensure_codex_flag_already_enabled(fake_home):
    from convo_recall.install._hooks import _ensure_codex_hooks_feature_flag
    config = fake_home / ".codex" / "config.toml"
    config.parent.mkdir(parents=True)
    config.write_text("[features]\ncodex_hooks = true\n")
    ok, msg = _ensure_codex_hooks_feature_flag()
    assert ok
    assert "already enabled" in msg


def test_ensure_codex_flag_invalid_toml_skips(fake_home):
    from convo_recall.install._hooks import _ensure_codex_hooks_feature_flag
    config = fake_home / ".codex" / "config.toml"
    config.parent.mkdir(parents=True)
    config.write_text("this is not = valid [toml\nat all")
    ok, msg = _ensure_codex_hooks_feature_flag()
    assert not ok
    assert "invalid TOML" in msg or "skipping" in msg


# ── H05 / H09 — install_hooks / uninstall_hooks per-kind behavior ───────────


def test_install_hooks_kind_ingest_writes_stop_to_claude_settings(fake_home):
    from convo_recall.install._hooks import install_hooks
    n = install_hooks(agents=["claude"], non_interactive=True, kinds=("ingest",))
    assert n == 1
    settings = fake_home / ".claude" / "settings.json"
    data = json.loads(settings.read_text())
    stop_groups = data["hooks"]["Stop"]
    commands = [h["command"] for g in stop_groups for h in g["hooks"]]
    assert any(c.endswith("conversation-ingest.sh") for c in commands)


def test_install_hooks_kind_ingest_writes_after_agent_to_gemini_settings(fake_home):
    from convo_recall.install._hooks import install_hooks
    n = install_hooks(agents=["gemini"], non_interactive=True, kinds=("ingest",))
    assert n == 1
    settings = fake_home / ".gemini" / "settings.json"
    data = json.loads(settings.read_text())
    after_groups = data["hooks"]["AfterAgent"]
    commands = [h["command"] for g in after_groups for h in g["hooks"]]
    assert any(c.endswith("conversation-ingest.sh") for c in commands)


def test_install_hooks_kind_ingest_codex_writes_feature_flag(fake_home):
    from convo_recall.install._hooks import install_hooks
    install_hooks(agents=["codex"], non_interactive=True, kinds=("ingest",))
    config = fake_home / ".codex" / "config.toml"
    assert config.exists()
    assert "codex_hooks = true" in config.read_text()


def test_install_hooks_kind_ingest_codex_invalid_toml_skips(fake_home, capsys):
    from convo_recall.install._hooks import install_hooks
    config = fake_home / ".codex" / "config.toml"
    config.parent.mkdir(parents=True)
    config.write_text("garbage [not toml")
    install_hooks(agents=["codex"], non_interactive=True, kinds=("ingest",))
    out = capsys.readouterr().out
    assert "skipping" in out or "invalid TOML" in out
    # No hooks.json should be written when the flag couldn't be set.
    hooks_json = fake_home / ".codex" / "hooks.json"
    assert not hooks_json.exists()


def test_install_hooks_preserves_existing_other_hooks(fake_home):
    from convo_recall.install._hooks import install_hooks
    settings = fake_home / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps({
        "hooks": {
            "Stop": [{"hooks": [{"type": "command", "command": "/usr/bin/other-tool"}]}]
        }
    }))
    install_hooks(agents=["claude"], non_interactive=True, kinds=("ingest",))
    data = json.loads(settings.read_text())
    commands = [h["command"] for g in data["hooks"]["Stop"] for h in g["hooks"]]
    assert "/usr/bin/other-tool" in commands
    assert any(c.endswith("conversation-ingest.sh") for c in commands)


def test_uninstall_hooks_default_walks_both_kinds(fake_home):
    from convo_recall.install._hooks import install_hooks, uninstall_hooks
    install_hooks(agents=["claude"], non_interactive=True, kinds=("memory", "ingest"))
    settings = fake_home / ".claude" / "settings.json"
    data = json.loads(settings.read_text())
    assert "UserPromptSubmit" in data["hooks"]
    assert "Stop" in data["hooks"]
    uninstall_hooks(agents=["claude"])  # default kinds = both
    data = json.loads(settings.read_text())
    # Both events should be gone (no other hooks present).
    assert "UserPromptSubmit" not in (data.get("hooks") or {})
    assert "Stop" not in (data.get("hooks") or {})


def test_install_hooks_idempotent_per_kind(fake_home):
    from convo_recall.install._hooks import install_hooks
    n1 = install_hooks(agents=["claude"], non_interactive=True, kinds=("ingest",))
    n2 = install_hooks(agents=["claude"], non_interactive=True, kinds=("ingest",))
    assert n1 == 1
    assert n2 == 0  # second call: already wired, no-op
