"""
Claude per-agent ingester for convo-recall.

Provides:
  - ingest_file(con, jsonl_path, do_embed, agent) — ingest one Claude JSONL.
  - _iter_claude_files(projects_dir=None) — walk PROJECTS_DIR for sessions.
  - _session_id_from_path — extract session_id from file path (handles the
    `subagents/` nested-dir convention).
  - _is_error_result / _extract_tool_result_text — helpers for the
    tool_result-block harvesting hot path (TD-006-protected).
  - _ERROR_PATTERNS — error-text regex.

Extracted from ingest.py in v0.4.0 (TD-008 / A7).

`_iter_claude_files` reads `PROJECTS_DIR` from the package init via lazy
`from .. import ingest as _pkg` so test fixtures patching
`ingest.PROJECTS_DIR` flow through to this codepath. PROJECTS_DIR stays
defined in `ingest/__init__.py` through v0.4.0; A8 finalizes the move.
"""

import json
import os
import re
import sys

import apsw
from datetime import datetime, timezone
from pathlib import Path

from ..db import _upsert_project
from ..identity import (
    _display_name,
    _legacy_claude_slug,
    _legacy_project_id,
    _project_id,
)
from .writer import (
    _clean_content,
    _extract_text,
    _persist_message,
    _upsert_ingested_file,
    _upsert_session,
)


# ── Error detection ───────────────────────────────────────────────────────────

_ERROR_PATTERNS = re.compile(
    r'(Error:|TypeError|ECONNREFUSED|Traceback|FAILED|AssertionError|'
    r'npm ERR!|cargo error|\bat\s+\w.*:\d+|Exit code [1-9])',
    re.I,
)


def _is_error_result(content: str) -> bool:
    return bool(_ERROR_PATTERNS.search(content))


def _extract_tool_result_text(block: dict) -> str:
    c = block.get("content", "")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return "\n".join(b.get("text", "") for b in c
                         if isinstance(b, dict) and b.get("type") == "text")
    return ""


# ── Path helpers ──────────────────────────────────────────────────────────────

def _session_id_from_path(jsonl_path: Path) -> str:
    if jsonl_path.parent.name == "subagents":
        return jsonl_path.parent.parent.name
    return jsonl_path.stem


def _iter_claude_files(projects_dir: "Path | None" = None):
    if projects_dir is None:
        # Lazy package read so tests patching `ingest.PROJECTS_DIR` reach here.
        from .. import ingest as _pkg
        projects_dir = _pkg.PROJECTS_DIR
    base = Path(projects_dir)
    if not base.exists():
        return
    for project_dir in base.iterdir():
        if not project_dir.is_dir():
            continue
        for pattern in ("*.jsonl", "*/subagents/*.jsonl"):
            yield from project_dir.glob(pattern)


# ── Ingestion ─────────────────────────────────────────────────────────────────

def ingest_file(con: apsw.Connection, jsonl_path: Path,
                do_embed: bool = True, agent: str = "claude") -> int:
    stat = jsonl_path.stat()
    file_key = str(jsonl_path)

    row = con.execute(
        "SELECT lines_ingested, last_modified FROM ingested_files WHERE file_path = ?",
        (file_key,),
    ).fetchone()

    if row and row["last_modified"] == stat.st_mtime:
        return 0

    lines_already = row["lines_ingested"] if row else 0
    session_id = _session_id_from_path(jsonl_path)

    # Pre-scan for cwd: Claude records carry a cwd field on user/attachment
    # rows. First-found wins. Falls back to the lossy slug encoding.
    recovered_cwd: "str | None" = None
    try:
        with open(jsonl_path, "r", errors="replace") as f:
            for i, line in enumerate(f):
                if i > 200:
                    break
                try:
                    d = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if isinstance(d, dict) and d.get("cwd"):
                    recovered_cwd = d["cwd"]
                    break
    except OSError:
        pass

    if recovered_cwd:
        project_id = _project_id(recovered_cwd)
        display_name = _display_name(recovered_cwd)
        cwd_real = os.path.realpath(recovered_cwd)
    else:
        legacy = _legacy_claude_slug(jsonl_path)
        project_id = _legacy_project_id(legacy)
        display_name = legacy
        cwd_real = None
    _upsert_project(con, project_id, display_name, cwd_real)

    inserted = 0
    malformed = 0
    title = None
    lines_read = 0

    with open(jsonl_path, "r", errors="replace") as f:
        for lineno, raw in enumerate(f):
            lines_read = lineno + 1
            if lineno < lines_already:
                if lineno < 5:
                    try:
                        rec = json.loads(raw)
                        if rec.get("type") == "custom-title":
                            title = rec.get("customTitle")
                    except (json.JSONDecodeError, ValueError):
                        pass
                continue

            try:
                rec = json.loads(raw)
            except (json.JSONDecodeError, ValueError):
                malformed += 1
                continue

            rtype = rec.get("type")
            if rtype == "custom-title":
                title = rec.get("customTitle")
                continue
            if rtype not in ("user", "assistant"):
                continue
            if rec.get("isMeta"):
                continue

            msg = rec.get("message", {})
            role = msg.get("role", rtype)
            raw_text = _extract_text(msg.get("content", ""))
            text = _clean_content(raw_text)

            uuid = rec.get("uuid", f"{session_id}:{lineno}")
            timestamp = rec.get("timestamp")
            model = msg.get("model") if role == "assistant" else None

            if text:
                inserted += _persist_message(
                    con, agent, project_id, session_id, uuid, role, text,
                    timestamp, do_embed, model=model,
                )

            # Index tool_result error blocks within user messages. This runs
            # independently of `text` — modern Claude Code emits user records
            # whose content is ONLY a tool_result block (no accompanying text),
            # so an early-out on empty `text` would silently drop every
            # tool error. See TD-006.
            if rtype == "user":
                content_blocks = msg.get("content", [])
                if isinstance(content_blocks, list):
                    for block in content_blocks:
                        if not isinstance(block, dict):
                            continue
                        if block.get("type") != "tool_result":
                            continue
                        raw_tr = _extract_tool_result_text(block)
                        if not raw_tr:
                            continue
                        if not (block.get("is_error", False) or _is_error_result(raw_tr)):
                            continue
                        tool_use_id = block.get("tool_use_id", f"tr{lineno}")
                        tr_uuid = f"{session_id}:tr:{tool_use_id}"
                        tr_text = _clean_content(raw_tr[:500])
                        if not tr_text:
                            continue
                        inserted += _persist_message(
                            con, agent, project_id, session_id, tr_uuid,
                            "tool_error", tr_text, timestamp, do_embed,
                        )

    now = datetime.now(timezone.utc).isoformat()
    _upsert_session(con, agent, project_id, session_id, title, now, now)
    _upsert_ingested_file(con, agent, file_key, session_id, project_id,
                           lines_read, stat.st_mtime)
    if malformed:
        print(f"[warn] {malformed} malformed JSONL record(s) skipped in "
              f"{jsonl_path.name}", file=sys.stderr)
    return inserted
