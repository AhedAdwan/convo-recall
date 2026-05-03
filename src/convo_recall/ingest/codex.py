"""
Codex per-agent ingester for convo-recall.

Provides:
  - ingest_codex_file(con, jsonl_path, do_embed) — ingest one rollout.
  - _iter_codex_files(codex_sessions=None) — walk CODEX_SESSIONS for rollouts.
  - _codex_event_msg_error / _codex_fco_error — extract failures from
    `event_msg` and `function_call_output` records.

Extracted from ingest.py in v0.4.0 (TD-008 / A7).

`_iter_codex_files` reads `CODEX_SESSIONS` from the package init via
lazy `from .. import ingest as _pkg`. CODEX_SESSIONS stays in
`ingest/__init__.py` through v0.4.0 (test-monkeypatched); A8 finalizes
the move.
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
    _legacy_project_id,
    _project_id,
)
from .claude import _is_error_result
from .writer import (
    _clean_content,
    _extract_text,
    _persist_message,
    _upsert_ingested_file,
    _upsert_session,
)


# ── Codex error extractors ───────────────────────────────────────────────────
#
# The agent CLIs emit failures in agent-specific shapes, not Anthropic's
# `tool_result.is_error` schema. Each helper below is a pure function that
# decides whether one record represents a harvestable failure and returns
# the error text (truncated) if so. Used by the in-place ingester and the
# tool_error_backfill walker.

def _codex_event_msg_error(rec: dict) -> "tuple[str, str] | None":
    """Extract (kind, text) from a Codex event_msg record if it represents
    a harvestable failure, else None. Recognized shapes (sandbox-confirmed):
      - exec_command_end with non-zero exit_code (shell failures)
      - patch_apply_end with success=False (failed file edits)
      - error (stream/rate-limit/CLI errors)
      - turn_aborted (user interrupt)
    Returned text is prefixed with a bracketed source tag for FTS targeting.
    """
    if rec.get("type") != "event_msg":
        return None
    pl = rec.get("payload", {})
    if not isinstance(pl, dict):
        return None
    pt = pl.get("type")
    if pt == "exec_command_end":
        ec = pl.get("exit_code")
        if ec is None or ec == 0:
            return None
        body = pl.get("aggregated_output") or ""
        if not body:
            body = (pl.get("stdout") or "") + "\n" + (pl.get("stderr") or "")
        return ("exec", f"[exec_command_end exit={ec}]\n{body[:500]}")
    if pt == "patch_apply_end":
        if pl.get("success", True):
            return None
        body = pl.get("stderr") or pl.get("stdout") or ""
        return ("patch", f"[patch_apply_end]\n{body[:500]}")
    if pt == "error":
        msg = pl.get("message") or ""
        info = pl.get("codex_error_info") or ""
        text = msg if not info else f"{msg} ({info})"
        return ("error", f"[codex_error]\n{text[:500]}")
    if pt == "turn_aborted":
        reason = pl.get("reason", "unknown")
        dur = pl.get("duration_ms")
        suffix = f" (after {dur}ms)" if dur is not None else ""
        return ("abort", f"[turn_aborted]\nTurn aborted: {reason}{suffix}")
    return None


def _codex_fco_error(rec: dict) -> "str | None":
    """Fallback extractor for Codex response_item.function_call_output records.
    Handles two output shapes:
      1. Older schema (~Sep 2025): output is JSON-string with metadata.exit_code.
      2. Newer schema (~2026): output is plain string; uses _is_error_result
         (catches "Process exited with code N").
    Returns truncated error text, else None.
    """
    if rec.get("type") != "response_item":
        return None
    pl = rec.get("payload", {})
    if not isinstance(pl, dict) or pl.get("type") != "function_call_output":
        return None
    out = pl.get("output", "")
    if not isinstance(out, str) or not out:
        return None
    try:
        obj = json.loads(out)
        if isinstance(obj, dict):
            ec = obj.get("metadata", {}).get("exit_code")
            if ec is not None and ec != 0:
                inner = obj.get("output", "")
                inner_s = inner if isinstance(inner, str) else str(inner)
                return f"[function_call_output exit={ec}]\n{inner_s[:500]}"
            return None  # Parsed cleanly, no error signal
    except (json.JSONDecodeError, ValueError):
        pass
    if _is_error_result(out):
        return f"[function_call_output]\n{out[:500]}"
    return None


# ── File iteration ───────────────────────────────────────────────────────────

def _iter_codex_files(codex_sessions: "Path | None" = None):
    if codex_sessions is None:
        from .. import ingest as _pkg
        codex_sessions = _pkg.CODEX_SESSIONS
    base = Path(codex_sessions)
    if not base.exists():
        return
    # Date-clustered: ~/.codex/sessions/{YYYY}/{MM}/{DD}/rollout-*.jsonl.
    # Skip ~/.codex/history.jsonl (lossy: rollout files are source of truth).
    yield from base.glob("*/*/*/rollout-*.jsonl")


# ── Ingestion ─────────────────────────────────────────────────────────────────

def ingest_codex_file(con: apsw.Connection, jsonl_path: Path,
                      do_embed: bool = True) -> int:
    """Ingest a single Codex rollout file.

    Source format:
      ~/.codex/sessions/{YYYY}/{MM}/{DD}/rollout-{ts}-{uuid}.jsonl

    Per record:
      - First record `type='session_meta'`: extract `payload.id` (session_id)
        and `payload.cwd` (project slug source).
      - `type='response_item'` with `payload.type='message'`:
          * `payload.role='user'` → user
          * `payload.role='assistant'` → assistant
          * `payload.role='developer'` skipped (system prompt)
          * `payload.content` is list of `{type:input_text|output_text, text}`
      - All other top-level types (`event_msg`, `turn_context`, function
        calls, reasoning blocks) are skipped — we only index human-readable
        user/assistant turns.

    `~/.codex/history.jsonl` is NOT touched here (rollouts are source of
    truth; the iter helper already excludes it by glob pattern).
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

    session_id = jsonl_path.stem  # fallback if session_meta missing
    project_id = _legacy_project_id("codex_unknown")
    display_name = "codex_unknown"
    cwd_real = None
    first_seen = None
    inserted = 0
    malformed = 0
    lines_read = 0

    def _set_project_from_cwd(cwd: str) -> None:
        nonlocal project_id, display_name, cwd_real
        project_id = _project_id(cwd)
        display_name = _display_name(cwd)
        cwd_real = os.path.realpath(cwd)

    with open(jsonl_path, "r", errors="replace") as f:
        for lineno, raw in enumerate(f):
            lines_read = lineno + 1
            if lineno < lines_already:
                # Re-scan first record on resume to pick up session_meta even
                # when ingestion previously stopped mid-file.
                if lineno == 0:
                    try:
                        rec = json.loads(raw)
                        if rec.get("type") == "session_meta":
                            payload = rec.get("payload", {})
                            session_id = payload.get("id", session_id)
                            cwd = payload.get("cwd")
                            if cwd:
                                _set_project_from_cwd(cwd)
                            first_seen = payload.get("timestamp") or rec.get("timestamp")
                    except (json.JSONDecodeError, ValueError):
                        pass
                continue
            try:
                rec = json.loads(raw)
            except (json.JSONDecodeError, ValueError):
                malformed += 1
                continue
            ttype = rec.get("type")
            timestamp = rec.get("timestamp")
            if ttype == "session_meta":
                payload = rec.get("payload", {})
                session_id = payload.get("id", session_id)
                cwd = payload.get("cwd")
                if cwd:
                    _set_project_from_cwd(cwd)
                first_seen = payload.get("timestamp") or rec.get("timestamp")
                continue

            # Tool-error harvesting (event_msg branch). Independent of the
            # message branch — codex emits failures as event_msg payloads,
            # not as `tool_result` blocks inside user messages.
            if ttype == "event_msg":
                hit = _codex_event_msg_error(rec)
                if hit is not None:
                    kind, tr_text = hit
                    payload_evt = rec.get("payload", {})
                    key = (payload_evt.get("call_id")
                           or payload_evt.get("turn_id")
                           or str(lineno))
                    tr_uuid = f"{session_id}:tr:codex:{kind}:{key}"
                    cleaned = _clean_content(tr_text)
                    if cleaned:
                        inserted += _persist_message(
                            con, "codex", project_id, session_id, tr_uuid,
                            "tool_error", cleaned, timestamp, do_embed,
                        )
                continue

            if ttype != "response_item":
                continue
            payload = rec.get("payload", {})
            payload_type = payload.get("type")

            # Tool-error fallback (function_call_output branch). Catches the
            # older Sep-2025 schema where exit_code lives inside output JSON,
            # plus modern plain-string outputs that match the error regex.
            if payload_type == "function_call_output":
                fco_text = _codex_fco_error(rec)
                if fco_text is not None:
                    call_id = payload.get("call_id") or str(lineno)
                    tr_uuid = f"{session_id}:tr:codex:fco:{call_id}"
                    cleaned = _clean_content(fco_text)
                    if cleaned:
                        inserted += _persist_message(
                            con, "codex", project_id, session_id, tr_uuid,
                            "tool_error", cleaned, timestamp, do_embed,
                        )
                continue

            if payload_type != "message":
                continue
            role_in = payload.get("role")
            if role_in == "user":
                role = "user"
            elif role_in == "assistant":
                role = "assistant"
            else:
                continue  # skip developer / system prompts
            text = _clean_content(_extract_text(payload.get("content", "")))
            if not text:
                continue
            uuid = payload.get("id") or f"{session_id}:{lineno}"
            inserted += _persist_message(con, "codex", project_id, session_id,
                                          uuid, role, text, timestamp, do_embed)

    _upsert_project(con, project_id, display_name, cwd_real)
    now = datetime.now(timezone.utc).isoformat()
    _upsert_session(con, "codex", project_id, session_id, None,
                    first_seen or now, now)
    _upsert_ingested_file(con, "codex", file_key, session_id, project_id,
                          lines_read, stat.st_mtime)
    if malformed:
        print(f"[warn] {malformed} malformed JSONL record(s) skipped in "
              f"{jsonl_path.name}", file=sys.stderr)
    return inserted
