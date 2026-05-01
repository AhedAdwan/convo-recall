"""Pre-prompt hook wiring — platform-agnostic, orthogonal to scheduler choice.

Edits each CLI's settings file (`~/.claude/settings.json`,
`~/.codex/hooks.json`, `~/.gemini/settings.json`) to insert a hook
block pointing at the bundled `conversation-memory.sh`.

Idempotent on re-wire (matches an existing hook by command path) and
preserves the user's other hooks on uninstall.
"""

import importlib.resources as _resources
import json
import os
import sys
import time
from pathlib import Path


# Maps agent → (settings file path, hook event name, agent label).
def _hook_target(agent: str) -> tuple[Path, str, str]:
    """Return (settings_path, event_name, agent_label) for a given agent."""
    if agent == "claude":
        return Path.home() / ".claude" / "settings.json", "UserPromptSubmit", "claude"
    if agent == "codex":
        return Path.home() / ".codex" / "hooks.json", "UserPromptSubmit", "codex"
    if agent == "gemini":
        return Path.home() / ".gemini" / "settings.json", "BeforeAgent", "gemini"
    raise ValueError(f"unknown agent: {agent}")


def _hook_block(agent: str, hook_script: Path) -> dict:
    """Build the hook block to insert under settings.hooks[event]."""
    if agent == "gemini":
        # Gemini uses millisecond timeouts and requires a `name` field.
        return {
            "matcher": "*",
            "hooks": [{
                "name": "convo-recall",
                "type": "command",
                "command": str(hook_script),
                "timeout": 5000,
            }],
        }
    # Claude and Codex share the same shape; timeout is in seconds.
    return {
        "hooks": [{
            "type": "command",
            "command": str(hook_script),
            "timeout": 5,
        }],
    }


def _hook_block_signature(agent: str, hook_script: Path) -> str:
    """Stable identifier for an existing convo-recall hook entry — lets
    `install-hooks` skip already-wired CLIs and `uninstall-hooks` find
    only the convo-recall block among the user's other hooks."""
    return str(hook_script)


def _find_hook_script() -> Path:
    """Locate the bundled `conversation-memory.sh`. Tries the editable-install
    path first (works in dev), falls back to importlib.resources (works
    after pipx install)."""
    here = Path(__file__).resolve().parent.parent / "hooks" / "conversation-memory.sh"
    if here.is_file():
        return here
    # importlib.resources path for installed wheel
    try:
        with _resources.path("convo_recall.hooks", "conversation-memory.sh") as p:
            return Path(p).resolve()
    except (ModuleNotFoundError, FileNotFoundError):
        pass
    raise RuntimeError(
        "Cannot locate conversation-memory.sh. "
        "Reinstall convo-recall and try again."
    )


def _backup_path(p: Path) -> Path:
    """Atomic-ish backup filename: <name>.bak.<unix-ts>."""
    return p.with_name(p.name + f".bak.{int(time.time())}")


def _wire_hook(agent: str, hook_script: Path,
               *, dry_run: bool = False) -> tuple[bool, str]:
    """Wire the convo-recall pre-prompt hook into one CLI's settings file.

    Returns (changed, message). Idempotent: if a hook block with the same
    command path already exists for the right event, no-op.
    """
    settings_path, event, label = _hook_target(agent)
    existing: dict = {}
    if settings_path.exists():
        try:
            existing = json.loads(settings_path.read_text())
        except (json.JSONDecodeError, OSError) as e:
            return False, f"  ⚠  {label}: settings unreadable ({e}); skipping"

    new_block = _hook_block(agent, hook_script)
    sig = _hook_block_signature(agent, hook_script)

    hooks_root = existing.setdefault("hooks", {}) if not dry_run else (
        # Build a copy for diffing; never mutate `existing` in dry-run.
        json.loads(json.dumps(existing.get("hooks", {})))
    )
    event_groups = hooks_root.setdefault(event, [])

    # Idempotency: scan existing groups for a hook command that matches.
    for group in event_groups:
        for entry in group.get("hooks", []):
            if entry.get("command") == sig:
                return False, f"  · {label}: hook already wired ({settings_path})"

    if dry_run:
        return True, (
            f"  + {label}: would add convo-recall hook to "
            f"{settings_path} → hooks.{event}"
        )

    event_groups.append(new_block)

    # Atomic write with mode 0o600 (settings can include API keys).
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    if settings_path.exists():
        backup = _backup_path(settings_path)
        backup.write_bytes(settings_path.read_bytes())
        backup_msg = f" (backup: {backup.name})"
    else:
        backup_msg = " (new file)"
    tmp = settings_path.with_name(settings_path.name + ".tmp")
    tmp.write_text(json.dumps(existing, indent=2))
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    tmp.replace(settings_path)
    return True, f"  ✅ {label}: hook wired into {settings_path}{backup_msg}"


_ORPHAN_HOOK_SUFFIX = "convo_recall/hooks/conversation-memory.sh"


def _is_convo_recall_hook(cmd: str | None, current_sig: str) -> bool:
    """Match anything that is or was a convo-recall hook entry.

    `current_sig` matches the package's CURRENT install path. The suffix
    fallback catches orphan entries left over from a previous install
    at a different path — e.g. you ran `pipx install -e <pathA>` once,
    moved sources, and reinstalled from `<pathB>`. Without the suffix
    match, the prior entry survives uninstall_hooks and silently fires
    a broken script on every agent turn.
    """
    if not cmd:
        return False
    if cmd == current_sig:
        return True
    return cmd.endswith(_ORPHAN_HOOK_SUFFIX)


def _unwire_hook(agent: str, hook_script: Path) -> tuple[bool, str]:
    """Remove the convo-recall hook block from a CLI's settings — matched
    by current install path AND by suffix `convo_recall/hooks/conversation-memory.sh`
    so orphans from prior installs at other paths are cleaned too.
    Leaves user's other hooks untouched."""
    settings_path, event, label = _hook_target(agent)
    if not settings_path.exists():
        return False, f"  · {label}: no settings file; nothing to remove"
    try:
        existing = json.loads(settings_path.read_text())
    except (json.JSONDecodeError, OSError) as e:
        return False, f"  ⚠  {label}: settings unreadable ({e}); skipping"
    sig = _hook_block_signature(agent, hook_script)
    hooks_root = existing.get("hooks") or {}
    event_groups = hooks_root.get(event) or []
    new_groups = []
    removed = 0
    for group in event_groups:
        kept_hooks = [
            h for h in group.get("hooks", [])
            if not _is_convo_recall_hook(h.get("command"), sig)
        ]
        if not kept_hooks:
            removed += 1
            continue
        if len(kept_hooks) != len(group.get("hooks", [])):
            removed += 1
        if kept_hooks:
            new_group = dict(group)
            new_group["hooks"] = kept_hooks
            new_groups.append(new_group)
    if not removed:
        return False, f"  · {label}: no convo-recall hook found; nothing to remove"
    if new_groups:
        hooks_root[event] = new_groups
    else:
        hooks_root.pop(event, None)
    if not hooks_root:
        existing.pop("hooks", None)
    backup = _backup_path(settings_path)
    backup.write_bytes(settings_path.read_bytes())
    tmp = settings_path.with_name(settings_path.name + ".tmp")
    tmp.write_text(json.dumps(existing, indent=2))
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    tmp.replace(settings_path)
    return True, f"  ✅ {label}: hook removed from {settings_path} (backup: {backup.name})"


def install_hooks(agents: list[str] | None = None,
                  *, dry_run: bool = False,
                  non_interactive: bool = False) -> int:
    """Standalone hook-wiring entry point. Used both by `recall install-hooks`
    and as one stage of the full `recall install` wizard.

    Returns the count of CLIs actually changed. Skips agents with no
    detectable source dir unless `agents` is passed explicitly.
    """
    import convo_recall.ingest as _ingest
    from . import _ask  # late import: _ask lives in __init__.py

    try:
        hook_script = _find_hook_script()
    except RuntimeError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 0

    if agents is None:
        detected = _ingest.detect_agents()
        agents = [d["name"] for d in detected if d["file_count"] > 0]
        if not agents:
            print("No agent source directories detected. Nothing to wire.")
            return 0

    print(f"Pre-prompt hook script: {hook_script}\n")
    if dry_run:
        print("[dry-run] showing what would change:\n")

    changed = 0
    for agent in agents:
        if agent not in ("claude", "codex", "gemini"):
            print(f"  ⚠  unknown agent {agent!r}, skipping")
            continue
        if not non_interactive and not dry_run:
            settings_path, event, label = _hook_target(agent)
            consent = _ask(
                f"Wire convo-recall hook for {label.title()} ({settings_path})?",
                default=True,
                if_yes=f"{label.title()} will see a 'search history first' hint on every prompt.",
                if_no=f"{label.title()} won't know convo-recall exists. "
                      f"Re-run `recall install-hooks --agent {label}` later to wire it.",
                non_interactive=False,
            )
            if not consent:
                print(f"  · {label}: skipped by user")
                continue
        did_change, msg = _wire_hook(agent, hook_script, dry_run=dry_run)
        print(msg)
        if did_change and not dry_run:
            changed += 1
    return changed


def uninstall_hooks(agents: list[str] | None = None) -> int:
    """Remove convo-recall hook blocks from each CLI's settings file."""
    try:
        hook_script = _find_hook_script()
    except RuntimeError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 0
    if agents is None:
        agents = ["claude", "codex", "gemini"]
    removed = 0
    for agent in agents:
        if agent not in ("claude", "codex", "gemini"):
            continue
        did_remove, msg = _unwire_hook(agent, hook_script)
        print(msg)
        if did_remove:
            removed += 1
    return removed
