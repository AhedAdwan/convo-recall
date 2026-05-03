"""
Dispatch / scan / watch for the convo-recall ingest path.

Provides:
  - _AGENT_INGEST / _AGENT_ITERATORS / _AGENT_SOURCE_PATHS — dispatch tables
    that map each agent name to its ingester / iterator / source-path getter.
  - detect_agents() — list `{name, path, file_count}` per agent.
  - load_config / save_config — `~/.local/share/convo-recall/config.json`.
  - _dispatch_ingest — shared per-agent loop; counts files, ticks the
    `ingest` progress phase, returns (total_messages, files_changed).
  - scan_one_agent — single-agent entry point.
  - scan_all — multi-agent entry point + self-heal embed pass for messages
    that landed while the embed sidecar was down. Contains the SQL pattern
    `LEFT JOIN message_vecs v ON v.rowid = m.rowid WHERE v.rowid IS NULL`
    which `tests/test_ingest.py` source-greps as a structural invariant.
  - watch_loop — polling watcher used inside the sandbox / on Linux.

Extracted from ingest.py in v0.4.0 (TD-008 / A7).

Reads `SUPPORTED_AGENTS`, `_CONFIG_PATH`, `EMBED_SOCK` from the package
init via lazy `from .. import ingest as _pkg`. Those constants stay in
`ingest/__init__.py` through v0.4.0; A8 finalizes the moves.
"""

import json
import os
import sys

import apsw
from datetime import datetime, timezone
from pathlib import Path

from ..db import _harden_perms, _vec_ok
from ..embed import _vec_insert, _wait_for_embed_socket, embed
from ..identity import _legacy_claude_slug, _legacy_gemini_slug
from .claude import _iter_claude_files, ingest_file
from .codex import _iter_codex_files, ingest_codex_file
from .gemini import _iter_gemini_files, ingest_gemini_file


# ── Dispatch tables ──────────────────────────────────────────────────────────

_AGENT_INGEST = {
    "claude": ingest_file,
    "gemini": ingest_gemini_file,
    "codex":  ingest_codex_file,
}

_AGENT_ITERATORS = {
    "claude": _iter_claude_files,
    "gemini": _iter_gemini_files,
    "codex":  _iter_codex_files,
}


def _agent_source_path(name: str) -> Path:
    """Resolve `name → source dir` via the package init (monkeypatch-aware)."""
    from .. import ingest as _pkg
    if name == "claude":
        return _pkg.PROJECTS_DIR
    if name == "gemini":
        return _pkg.GEMINI_TMP
    if name == "codex":
        return _pkg.CODEX_SESSIONS
    raise KeyError(name)


_AGENT_SOURCE_PATHS = {
    "claude": lambda: _agent_source_path("claude"),
    "gemini": lambda: _agent_source_path("gemini"),
    "codex":  lambda: _agent_source_path("codex"),
}


# ── Agent detection + config ─────────────────────────────────────────────────

def detect_agents() -> "list[dict]":
    """Return a list of {name, path, file_count} for each supported agent.

    Agents whose source dir doesn't exist report file_count=0 (they're 'absent'
    from this machine). Callers typically filter to file_count > 0 when
    showing a detection prompt.
    """
    from .. import ingest as _pkg
    result = []
    for name in _pkg.SUPPORTED_AGENTS:
        path = _AGENT_SOURCE_PATHS[name]()
        if not path.exists():
            result.append({"name": name, "path": str(path), "file_count": 0})
            continue
        count = sum(1 for _ in _AGENT_ITERATORS[name](path))
        result.append({"name": name, "path": str(path), "file_count": count})
    return result


def load_config() -> dict:
    """Load `~/.local/share/convo-recall/config.json` or return defaults.

    Also re-chmod the file to 0o600 if it was created with a wider mode
    (e.g. by a shell `echo > config.json` that bypassed `save_config`).
    """
    from .. import ingest as _pkg
    config_path = _pkg._CONFIG_PATH
    if not config_path.exists():
        return {"agents": ["claude"]}  # default — preserves pre-multi-agent behavior
    _harden_perms(config_path, 0o600)
    try:
        return json.loads(config_path.read_text())
    except (json.JSONDecodeError, OSError) as e:
        print(f"[warn] config read failed ({e}); using defaults", file=sys.stderr)
        return {"agents": ["claude"]}


def save_config(cfg: dict) -> None:
    """Persist config atomically with mode 0o600."""
    from .. import ingest as _pkg
    config_path = _pkg._CONFIG_PATH
    config_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = config_path.with_suffix(config_path.suffix + ".tmp")
    tmp.write_text(json.dumps(cfg, indent=2))
    os.chmod(tmp, 0o600)
    tmp.replace(config_path)


# ── Per-agent dispatch ───────────────────────────────────────────────────────

def _dispatch_ingest(con: apsw.Connection, agents: "list[str]", *,
                     embed_live: bool, verbose: bool) -> "tuple[int, int]":
    """Run the ingest pipeline for the named agents in order.

    Returns (total_messages_inserted, total_files_with_changes). Shared by
    `scan_one_agent` and `scan_all` so the per-agent dispatch logic lives
    in one place.

    Pre-pass counts total session files across all enabled agents and
    publishes that as the `ingest` phase total via the _progress tracker
    (no-op if no active run, e.g. the watcher loop). Each file processed
    ticks the counter so `recall stats` shows a live bar during ingest.
    """
    from .. import _progress

    # Build the work list once so we can both count and process from it.
    # File-path lists are tiny (a few KB even at 10K files) — well worth
    # the visibility win.
    work: "list[tuple[str, Path]]" = []
    for agent_name in agents:
        if agent_name not in _AGENT_INGEST:
            print(f"[warn] unknown agent: {agent_name}", file=sys.stderr)
            continue
        for jsonl_path in _AGENT_ITERATORS[agent_name]():
            work.append((agent_name, jsonl_path))

    _progress.set_phase_total("ingest", len(work))

    total = 0
    files = 0
    for processed, (agent_name, jsonl_path) in enumerate(work, start=1):
        ingester = _AGENT_INGEST[agent_name]
        kwargs = {"do_embed": embed_live}
        if agent_name == "claude":
            kwargs["agent"] = "claude"
        n = ingester(con, jsonl_path, **kwargs)
        if n > 0:
            files += 1
            total += n
            if verbose:
                slug = (_legacy_claude_slug(jsonl_path) if agent_name == "claude"
                        else _legacy_gemini_slug(jsonl_path) if agent_name == "gemini"
                        else jsonl_path.parent.name)
                print(f"  +{n:4d} msgs  [{agent_name}] {slug}/{jsonl_path.name[:8]}…")
        # Tick on every file so the bar advances at human-perceptible
        # cadence even when most files have no new messages (the common
        # case on a re-ingest of an already-populated DB).
        _progress.update_phase("ingest", processed)
    return total, files


def scan_one_agent(con: apsw.Connection, agent_name: str,
                   verbose: bool = False, do_embed: bool = True) -> int:
    """Scan and ingest only the named agent's source files. Returns total
    messages inserted. Used by `recall ingest --agent {name}` and by the
    per-agent launchd plists generated at install time."""
    from .. import ingest as _pkg
    if agent_name not in _AGENT_INGEST:
        print(f"[error] unknown agent: {agent_name}", file=sys.stderr)
        return 0
    embed_live = _pkg.EMBED_SOCK.exists() and do_embed
    total, files = _dispatch_ingest(con, [agent_name],
                                     embed_live=embed_live, verbose=verbose)
    if verbose or total > 0:
        print(f"Ingested {total} new [{agent_name}] message(s) from {files} file(s).")
    return total


def watch_loop(con: apsw.Connection, interval: int = 10,
               verbose: bool = False) -> None:
    """Polling watcher used inside the sandbox / on Linux (no launchd).

    Calls `scan_all` every `interval` seconds. Exits cleanly on SIGINT/SIGTERM.
    On macOS, prefer per-agent launchd plists generated by `recall install` —
    they are file-system event driven (no polling) and integrate with login
    sessions cleanly.
    """
    import signal, time
    stop = {"flag": False}
    def _handler(signum, frame):
        stop["flag"] = True
    signal.signal(signal.SIGINT, _handler)
    signal.signal(signal.SIGTERM, _handler)
    print(f"[watch] starting loop (interval={interval}s). Ctrl-C to stop.",
          flush=True)
    tick = 0
    while not stop["flag"]:
        tick += 1
        ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
        try:
            scan_all(con, verbose=verbose)
        except Exception as e:
            print(f"[watch] tick={tick} {ts} ERROR: {type(e).__name__}: {e}",
                  flush=True, file=sys.stderr)
        else:
            print(f"[watch] tick={tick} {ts} ok", flush=True)
        # Wait for `interval` seconds OR until stop flag set, whichever sooner
        for _ in range(interval):
            if stop["flag"]:
                break
            time.sleep(1)
    print("[watch] stopping.", flush=True)


def scan_all(con: apsw.Connection, verbose: bool = False,
             do_embed: bool = True) -> None:
    from .. import _progress
    from .. import ingest as _pkg

    # Close the race where the embed sidecar systemd unit was started
    # moments ago but hasn't bound its socket yet (~5s Linux, can be longer
    # for first-ever model download). Without this, embed_live=False here
    # → self-heal pass below silently skips → DB stays at 0% embedded.
    # On warm systems the socket already exists, so the wait is a no-op.
    if do_embed and _vec_ok(con):
        _wait_for_embed_socket(timeout_s=30.0, verbose=verbose)

    embed_live = _pkg.EMBED_SOCK.exists() and do_embed
    if not embed_live and do_embed:
        print("[warn] embed socket not found — running in FTS-only mode", file=sys.stderr)

    enabled_agents = load_config().get("agents") or ["claude"]

    # Same own-run pattern as embed_backfill: if a multi-phase chain is
    # already active (e.g. the wizard's _backfill-chain), we participate
    # in it and let the parent finish_run. Otherwise create a single-
    # phase run so standalone `recall ingest` shows a bar in stats.
    own_run = _progress.read_status() is None
    if own_run:
        _progress.start_run([("ingest", 0)])
    try:
        total, files = _dispatch_ingest(con, enabled_agents,
                                         embed_live=embed_live, verbose=verbose)
        _progress.finish_phase("ingest")
    finally:
        if own_run:
            _progress.finish_run()
    if verbose or total > 0:
        print(f"Ingested {total} new messages from {files} file(s).")

    # Self-healing embed pass: catch messages ingested while embed service was down.
    # Order DESC so the most recent (and most-queried) messages heal first
    # after a fresh install against an existing DB. Cap bumped to 2000 — at
    # ~200ms/embedding warm this fits well inside the 10s watch tick.
    if embed_live and _vec_ok(con):
        missing = con.execute("""
            SELECT m.rowid, m.content FROM messages m
            LEFT JOIN message_vecs v ON v.rowid = m.rowid
            WHERE v.rowid IS NULL
            ORDER BY m.rowid DESC
            LIMIT 2000
        """).fetchall()
        if missing:
            healed = 0
            for rowid, content in missing:
                vec = embed(content)
                if vec:
                    _vec_insert(con, rowid, vec)
                    healed += 1
            if verbose or healed > 0:
                print(f"Healed {healed} missing embedding(s).")
