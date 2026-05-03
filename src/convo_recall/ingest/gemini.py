"""
Gemini per-agent ingester for convo-recall.

Provides:
  - ingest_gemini_file(con, jsonl_path, do_embed) — ingest one Gemini chat.
  - _iter_gemini_files(gemini_tmp=None) — walk GEMINI_TMP for sessions.
  - _load_gemini_aliases() — read the optional `{hash → real cwd}` map.
  - _gemini_record_error / _gemini_tool_call_error — extract failures from
    top-level `error`/`warning` records and from gemini's `toolCalls[]`.

Extracted from ingest.py in v0.4.0 (TD-008 / A7).

`_iter_gemini_files` and `_load_gemini_aliases` read `GEMINI_TMP` and
`_GEMINI_ALIAS_PATH` from the package init via lazy `from .. import
ingest as _pkg`. Both constants stay in `ingest/__init__.py` through
v0.4.0 (test-monkeypatched in `tests/test_ingest.py`); A8 finalizes
the moves.
"""

import json
import os
import sys

import apsw
from datetime import datetime, timezone
from pathlib import Path

from ..db import _upsert_project
from ..identity import (
    _display_name,
    _gemini_hash_project_id,
    _project_id,
)
from .writer import (
    _clean_content,
    _extract_text,
    _persist_message,
    _upsert_ingested_file,
    _upsert_session,
)


# ── Gemini error extractors ──────────────────────────────────────────────────

def _gemini_record_error(rec: dict) -> "tuple[str, str] | None":
    """Extract (kind, text) from a top-level Gemini message record if its
    type is 'error' or 'warning'. Content is always a plain string per the
    ConversationRecord schema.
    """
    rtype = rec.get("type")
    if rtype not in ("error", "warning"):
        return None
    content = rec.get("content", "")
    if not isinstance(content, str) or not content:
        return None
    kind = "cli_error" if rtype == "error" else "cli_warning"
    return (kind, f"[gemini_{rtype}]\n{content[:500]}")


def _gemini_tool_call_error(tc: dict) -> "str | None":
    """Extract error text from a Gemini ToolCallRecord if its status indicates
    failure ('error' or 'cancelled'). The error string typically lives at
    tc.result[].functionResponse.response.error.
    """
    if not isinstance(tc, dict):
        return None
    status = tc.get("status")
    if status not in ("error", "cancelled"):
        return None
    name = tc.get("name", "<?>")
    err_text = ""
    result = tc.get("result")
    if isinstance(result, list):
        for r in result:
            if not isinstance(r, dict):
                continue
            fr = r.get("functionResponse", {})
            if isinstance(fr, dict):
                resp = fr.get("response", {})
                if isinstance(resp, dict):
                    err = resp.get("error")
                    if isinstance(err, str) and err:
                        err_text = err
                        break
    if not err_text:
        err_text = json.dumps(result)[:500] if result is not None else f"toolCall {status}"
    return f"[gemini_tool {name} status={status}]\n{err_text[:500]}"


# ── File iteration + alias map ───────────────────────────────────────────────

def _iter_gemini_files(gemini_tmp: "Path | None" = None):
    if gemini_tmp is None:
        from .. import ingest as _pkg
        gemini_tmp = _pkg.GEMINI_TMP
    base = Path(gemini_tmp)
    if not base.exists():
        return
    yield from base.glob("*/chats/session-*.jsonl")


def _load_gemini_aliases() -> "dict[str, str]":
    """Read the optional `{sha-hash → human-slug}` map.

    The file is hand-editable. Returns an empty dict when missing or
    malformed; redactions/upgrades shouldn't crash on a stale file.
    """
    from .. import ingest as _pkg
    alias_path = _pkg._GEMINI_ALIAS_PATH
    if not alias_path.exists():
        return {}
    try:
        return json.loads(alias_path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


# ── Ingestion ─────────────────────────────────────────────────────────────────

def ingest_gemini_file(con: apsw.Connection, jsonl_path: Path,
                       do_embed: bool = True) -> int:
    """Ingest a single Gemini chat session file.

    Source format (one record per line):
      - First record: header `{sessionId, projectHash, startTime, kind}` —
        used to seed session metadata.
      - User messages: `{id, timestamp, type: "user", content: [{text}]}`
      - Gemini messages: `{id, timestamp, type: "gemini", content: [{text}]}`
      - `{$set: ...}` records: metadata patches, skipped.
      - `{type: "info", ...}` records: tool/system info, skipped.

    Tool-call records are skipped (we only index human-readable text).
    """
    stat = jsonl_path.stat()
    file_key = str(jsonl_path)

    row = con.execute(
        "SELECT lines_ingested, last_modified FROM ingested_files WHERE file_path = ?",
        (file_key,),
    ).fetchone()
    if row and row["last_modified"] == stat.st_mtime:
        return 0
    lines_already = row["lines_ingested"] if row else 0

    # Three-layer project_id resolution (in priority order):
    #   1. cwd from session header → _project_id(cwd)
    #   2. ~/.gemini/projects.json reverse-lookup of hash_dir → real cwd
    #   3. SHA-hash dir name → synthetic gemini-hash:<hash> id
    hash_dir = jsonl_path.parent.parent.name
    project_id: "str | None" = None
    display_name: "str | None" = None
    cwd_real: "str | None" = None

    aliases = _load_gemini_aliases()
    aliased_cwd = aliases.get(hash_dir)
    if aliased_cwd:
        project_id = _project_id(aliased_cwd)
        display_name = _display_name(aliased_cwd)
        cwd_real = os.path.realpath(aliased_cwd)

    session_id = jsonl_path.stem  # fallback if no header
    first_seen = None
    inserted = 0
    malformed = 0
    lines_read = 0

    with open(jsonl_path, "r", errors="replace") as f:
        for lineno, raw in enumerate(f):
            lines_read = lineno + 1
            if lineno < lines_already:
                continue
            try:
                rec = json.loads(raw)
            except (json.JSONDecodeError, ValueError):
                malformed += 1
                continue
            # Header record (no `type`) — extract sessionId/startTime/cwd
            if "$set" in rec:
                continue
            if "sessionId" in rec and "type" not in rec:
                session_id = rec.get("sessionId", session_id)
                first_seen = rec.get("startTime") or first_seen
                cwd = rec.get("cwd") or rec.get("projectDir")
                if cwd:
                    project_id = _project_id(cwd)
                    display_name = _display_name(cwd)
                    cwd_real = os.path.realpath(cwd)
                continue
            rtype = rec.get("type")
            if rtype not in ("user", "gemini", "error", "warning"):
                continue
            # Defer message inserts until we've decided project_id (after header).
            if project_id is None:
                project_id = _gemini_hash_project_id(hash_dir)
                display_name = hash_dir
                cwd_real = None
            timestamp = rec.get("timestamp")

            # Top-level CLI error/warning records → tool_error.
            if rtype in ("error", "warning"):
                hit = _gemini_record_error(rec)
                if hit is not None:
                    kind, tr_text = hit
                    rec_id = rec.get("id") or str(lineno)
                    tr_uuid = f"{session_id}:tr:gemini:{kind}:{rec_id}"
                    cleaned = _clean_content(tr_text)
                    if cleaned:
                        inserted += _persist_message(
                            con, "gemini", project_id, session_id, tr_uuid,
                            "tool_error", cleaned, timestamp, do_embed,
                        )
                continue

            role = "user" if rtype == "user" else "assistant"
            text = _clean_content(_extract_text(rec.get("content", "")))
            uuid = rec.get("id") or f"{session_id}:{lineno}"

            if text:
                inserted += _persist_message(con, "gemini", project_id, session_id,
                                              uuid, role, text, timestamp, do_embed)

            # toolCalls[] error/cancelled harvest. Runs independently of
            # `text` so messages that are pure tool wrappers don't drop the
            # error signal (TD-006-style invariant).
            if rtype == "gemini":
                tool_calls = rec.get("toolCalls") or []
                if isinstance(tool_calls, list):
                    for tc in tool_calls:
                        tr_text = _gemini_tool_call_error(tc)
                        if tr_text is None:
                            continue
                        tc_id = tc.get("id") or f"{lineno}-{id(tc)}"
                        tr_uuid = f"{session_id}:tr:gemini:tool:{tc_id}"
                        cleaned = _clean_content(tr_text)
                        if cleaned:
                            inserted += _persist_message(
                                con, "gemini", project_id, session_id, tr_uuid,
                                "tool_error", cleaned, timestamp, do_embed,
                            )

    # If no records produced project_id (empty file or skipped-only), fall back.
    if project_id is None:
        project_id = _gemini_hash_project_id(hash_dir)
        display_name = hash_dir
        cwd_real = None

    _upsert_project(con, project_id, display_name, cwd_real)
    now = datetime.now(timezone.utc).isoformat()
    _upsert_session(con, "gemini", project_id, session_id, None,
                    first_seen or now, now)
    _upsert_ingested_file(con, "gemini", file_key, session_id, project_id,
                          lines_read, stat.st_mtime)
    if malformed:
        print(f"[warn] {malformed} malformed JSONL record(s) skipped in "
              f"{jsonl_path.name}", file=sys.stderr)
    return inserted
