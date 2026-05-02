"""Pre-prompt + ingest hook wiring — platform-agnostic, orthogonal to scheduler choice.

Edits each CLI's settings file (`~/.claude/settings.json`,
`~/.codex/hooks.json`, `~/.gemini/settings.json`) to insert hook blocks
pointing at the bundled `conversation-memory.sh` (search hook) and
`conversation-ingest.sh` (response-completion ingest hook).

Idempotent on re-wire (matches an existing hook by command path) and
preserves the user's other hooks on uninstall. The same helper functions
parameterize on `hook_kind: Literal["memory", "ingest"]` — block shape
only depends on the agent (Gemini differs from Claude/Codex), event name
depends on kind.
"""

import importlib.resources as _resources
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Literal, Sequence


HookKind = Literal["memory", "ingest"]


def _hook_target(agent: str, kind: HookKind = "memory") -> tuple[Path, str, str]:
    """Return (settings_path, event_name, agent_label) for (agent, kind).

    `kind="memory"` → search hook fires on UserPromptSubmit / BeforeAgent.
    `kind="ingest"` → ingest hook fires on Stop / AfterAgent.
    """
    if agent == "claude":
        path = Path.home() / ".claude" / "settings.json"
        event = "UserPromptSubmit" if kind == "memory" else "Stop"
        return path, event, "claude"
    if agent == "codex":
        path = Path.home() / ".codex" / "hooks.json"
        event = "UserPromptSubmit" if kind == "memory" else "Stop"
        return path, event, "codex"
    if agent == "gemini":
        path = Path.home() / ".gemini" / "settings.json"
        event = "BeforeAgent" if kind == "memory" else "AfterAgent"
        return path, event, "gemini"
    raise ValueError(f"unknown agent: {agent}")


def _hook_block(agent: str, hook_script: Path) -> dict:
    """Build the hook block to insert under settings.hooks[event].

    Block shape depends only on the agent (Gemini uses millisecond
    timeouts and a `name` field). Same shape works for memory and
    ingest hooks — only the event name differs.
    """
    if agent == "gemini":
        return {
            "matcher": "*",
            "hooks": [{
                "name": "convo-recall",
                "type": "command",
                "command": str(hook_script),
                "timeout": 5000,
            }],
        }
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


_HOOK_SCRIPT_NAMES: dict[HookKind, str] = {
    "memory": "conversation-memory.sh",
    "ingest": "conversation-ingest.sh",
}


def _find_hook_script(kind: HookKind = "memory") -> Path:
    """Locate the bundled hook script for the given kind. Tries the
    editable-install path first (works in dev), falls back to
    importlib.resources (works after pipx install)."""
    name = _HOOK_SCRIPT_NAMES[kind]
    here = Path(__file__).resolve().parent.parent / "hooks" / name
    if here.is_file():
        return here
    try:
        with _resources.path("convo_recall.hooks", name) as p:
            return Path(p).resolve()
    except (ModuleNotFoundError, FileNotFoundError):
        pass
    raise RuntimeError(
        f"Cannot locate {name}. Reinstall convo-recall and try again."
    )


def _backup_path(p: Path) -> Path:
    """Atomic-ish backup filename: <name>.bak.<unix-ts>."""
    return p.with_name(p.name + f".bak.{int(time.time())}")


def _ensure_codex_hooks_feature_flag() -> tuple[bool, str]:
    """Ensure ~/.codex/config.toml has [features] codex_hooks = true.

    Codex hooks are experimental and gated behind this flag. Auto-write
    when safe (file missing OR present and valid TOML where insertion
    yields valid TOML); skip-with-warning otherwise so we never corrupt
    user config.

    Returns (ok, message). ok=False means we couldn't safely write the
    flag — caller should skip Codex hook installation.
    """
    config_path = Path.home() / ".codex" / "config.toml"
    flag_line = "codex_hooks = true"
    section = "[features]"

    if not config_path.exists():
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(f"{section}\n{flag_line}\n")
        try:
            os.chmod(config_path, 0o600)
        except OSError:
            pass
        return True, f"  ✅ codex: wrote {config_path} with codex_hooks=true"

    try:
        text = config_path.read_text()
    except OSError as e:
        return False, f"  ⚠ codex: config.toml unreadable ({e}); skipping"

    if re.search(r"codex_hooks\s*=\s*true", text):
        return True, f"  · codex: codex_hooks already enabled in {config_path}"

    try:
        import tomllib
    except ImportError:
        return False, "  ⚠ codex: tomllib unavailable (Python <3.11); skipping"

    try:
        tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        return False, f"  ⚠ codex: config.toml is invalid TOML ({e}); skipping"

    if section in text:
        new_text = re.sub(
            re.escape(section) + r"\s*\n",
            f"{section}\n{flag_line}\n",
            text,
            count=1,
        )
    else:
        new_text = text.rstrip() + f"\n\n{section}\n{flag_line}\n"

    try:
        tomllib.loads(new_text)
    except tomllib.TOMLDecodeError as e:
        return False, f"  ⚠ codex: would corrupt config.toml ({e}); skipping"

    backup = _backup_path(config_path)
    backup.write_bytes(config_path.read_bytes())
    config_path.write_text(new_text)
    try:
        os.chmod(config_path, 0o600)
    except OSError:
        pass
    return True, (
        f"  ✅ codex: enabled codex_hooks in {config_path} "
        f"(backup: {backup.name})"
    )


def _wire_hook(agent: str, hook_script: Path,
               *, kind: HookKind = "memory",
               dry_run: bool = False) -> tuple[bool, str]:
    """Wire a convo-recall hook (memory or ingest) into one CLI's settings file.

    Returns (changed, message). Idempotent: if a hook block with the same
    command path already exists for the right event, no-op.
    """
    settings_path, event, label = _hook_target(agent, kind)
    existing: dict = {}
    if settings_path.exists():
        try:
            existing = json.loads(settings_path.read_text())
        except (json.JSONDecodeError, OSError) as e:
            return False, f"  ⚠  {label}: settings unreadable ({e}); skipping"

    new_block = _hook_block(agent, hook_script)
    sig = _hook_block_signature(agent, hook_script)

    hooks_root = existing.setdefault("hooks", {}) if not dry_run else (
        json.loads(json.dumps(existing.get("hooks", {})))
    )
    event_groups = hooks_root.setdefault(event, [])

    for group in event_groups:
        for entry in group.get("hooks", []):
            if entry.get("command") == sig:
                return False, f"  · {label}: {kind} hook already wired ({settings_path})"

    if dry_run:
        return True, (
            f"  + {label}: would add convo-recall {kind} hook to "
            f"{settings_path} → hooks.{event}"
        )

    event_groups.append(new_block)

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
    return True, f"  ✅ {label}: {kind} hook wired into {settings_path}{backup_msg}"


_ORPHAN_HOOK_SUFFIXES = (
    "convo_recall/hooks/conversation-memory.sh",
    "convo_recall/hooks/conversation-ingest.sh",
)


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
    return any(cmd.endswith(suffix) for suffix in _ORPHAN_HOOK_SUFFIXES)


def _unwire_hook(agent: str, hook_script: Path,
                 *, kind: HookKind = "memory") -> tuple[bool, str]:
    """Remove the convo-recall hook block from a CLI's settings — matched
    by current install path AND by suffix
    `convo_recall/hooks/conversation-{memory,ingest}.sh` so orphans from
    prior installs at other paths are cleaned too. Leaves user's other
    hooks untouched."""
    settings_path, event, label = _hook_target(agent, kind)
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
        return False, f"  · {label}: no convo-recall {kind} hook found; nothing to remove"
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
    return True, f"  ✅ {label}: {kind} hook removed from {settings_path} (backup: {backup.name})"


def install_hooks(agents: list[str] | None = None,
                  *, dry_run: bool = False,
                  non_interactive: bool = False,
                  kinds: Sequence[HookKind] = ("memory",)) -> int:
    """Standalone hook-wiring entry point. Used both by `recall install-hooks`
    and as one stage of the full `recall install` wizard.

    `kinds` selects which hook(s) to wire. Default is `("memory",)` for
    backward compatibility with existing call sites; pass `("ingest",)`
    or `("memory", "ingest")` for the new ingest hook.

    Returns the count of (CLI, kind) pairs actually changed. Skips agents
    with no detectable source dir unless `agents` is passed explicitly.
    """
    import convo_recall.ingest as _ingest
    from . import _ask  # late import: _ask lives in __init__.py

    if agents is None:
        detected = _ingest.detect_agents()
        agents = [d["name"] for d in detected if d["file_count"] > 0]
        if not agents:
            print("No agent source directories detected. Nothing to wire.")
            return 0

    if dry_run:
        print("[dry-run] showing what would change:\n")

    changed = 0
    for kind in kinds:
        try:
            hook_script = _find_hook_script(kind)
        except RuntimeError as e:
            print(f"ERROR: {e}", file=sys.stderr)
            continue
        print(f"{kind.title()} hook script: {hook_script}\n")

        for agent in agents:
            if agent not in ("claude", "codex", "gemini"):
                print(f"  ⚠  unknown agent {agent!r}, skipping")
                continue

            # Codex ingest needs the codex_hooks feature flag enabled.
            if kind == "ingest" and agent == "codex":
                if not dry_run:
                    ok, msg = _ensure_codex_hooks_feature_flag()
                    print(msg)
                    if not ok:
                        continue

            if not non_interactive and not dry_run:
                settings_path, event, label = _hook_target(agent, kind)
                verb = "search" if kind == "memory" else "ingest"
                consent = _ask(
                    f"Wire convo-recall {verb} hook for {label.title()} ({settings_path})?",
                    default=True,
                    if_yes=(
                        f"{label.title()} will see a 'search history first' hint on every prompt."
                        if kind == "memory"
                        else f"{label.title()} fires `recall ingest` after every agent turn."
                    ),
                    if_no=(
                        f"{label.title()} won't know convo-recall exists. "
                        f"Re-run `recall install-hooks --agent {label}` later to wire it."
                        if kind == "memory"
                        else f"{label.title()} won't auto-ingest on turn end. "
                             f"Re-run `recall install-hooks --kind ingest --agent {label}` later."
                    ),
                    non_interactive=False,
                )
                if not consent:
                    print(f"  · {label}: skipped by user")
                    continue
            did_change, msg = _wire_hook(agent, hook_script, kind=kind, dry_run=dry_run)
            print(msg)
            if did_change and not dry_run:
                changed += 1
    return changed


def uninstall_hooks(agents: list[str] | None = None,
                    *, kinds: Sequence[HookKind] = ("memory", "ingest")) -> int:
    """Remove convo-recall hook blocks from each CLI's settings file.

    By default walks BOTH memory and ingest hooks so a single uninstall
    cleans up after any prior install path.
    """
    if agents is None:
        agents = ["claude", "codex", "gemini"]
    removed = 0
    for kind in kinds:
        try:
            hook_script = _find_hook_script(kind)
        except RuntimeError as e:
            print(f"ERROR: {e}", file=sys.stderr)
            continue
        for agent in agents:
            if agent not in ("claude", "codex", "gemini"):
                continue
            did_remove, msg = _unwire_hook(agent, hook_script, kind=kind)
            print(msg)
            if did_remove:
                removed += 1
    return removed
