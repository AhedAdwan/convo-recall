"""
Shared persistence layer + content-cleaning helpers for the ingest path.

Provides the symbols that every per-agent ingester (claude.py, gemini.py,
codex.py) calls into:
  - _expand_code_tokens / _clean_content — strip ANSI, XML wrappers, box-
    drawing chars, redact secrets, expand camelCase/snake_case for FTS.
  - _extract_text — pull human-readable text from a content payload
    (string, or list of {type, text} dict blocks; tool-use blocks excluded).
  - _upsert_session / _upsert_ingested_file — refresh per-file metadata.
  - _persist_message — the single INSERT-or-skip + embed point shared by
    all parsers and the tool_error path.

Extracted from ingest.py in v0.4.0 (TD-008 / A7).
"""

import os
import re
import sys

import apsw
from datetime import datetime, timezone

from .. import redact as _redact
from ..db import _vec_ok
from ..embed import embed, _vec_insert


# ── Content cleaning ──────────────────────────────────────────────────────────

_ANSI_RE        = re.compile(r'\x1b\[[0-9;]*[a-zA-Z]')
_CR_ERASE_RE    = re.compile(r'\r\x1b\[K')
_XML_PAIR_RE    = re.compile(
    r'<(?:command-name|local-command-stdout|local-command-caveat'
    r'|command-message|command-args)(?:\s[^>]*)?>.*?'
    r'</(?:command-name|local-command-stdout|local-command-caveat'
    r'|command-message|command-args)>',
    re.DOTALL,
)
_XML_SOLO_RE    = re.compile(
    r'</?(?:command-name|local-command-stdout|local-command-caveat'
    r'|command-message|command-args)(?:\s[^>]*)?>'
)
_BOX_BRAILLE_RE = re.compile(r'[╔╗╚╝║═─│┌┐└┘├┤┬┴┼━┃⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏]')
_BLANK_LINES_RE = re.compile(r'\n{3,}')


def _expand_code_tokens(text: str) -> str:
    text = re.sub(r'([a-z])([A-Z])', r'\1 \2', text)  # camelCase → camel Case
    text = re.sub(r'_([a-z])', r' \1', text)            # snake_case → snake case
    return text


def _clean_content(text: str) -> str:
    text = _CR_ERASE_RE.sub('', text)
    text = _ANSI_RE.sub('', text)
    text = _XML_PAIR_RE.sub('', text)
    text = _XML_SOLO_RE.sub('', text)
    text = _BOX_BRAILLE_RE.sub('', text)
    if os.environ.get("CONVO_RECALL_REDACT") != "off":
        text = _redact.redact_secrets(text)
    text = _BLANK_LINES_RE.sub('\n\n', text)
    text = _expand_code_tokens(text)
    return text.strip()


# ── Text extraction ──────────────────────────────────────────────────────────

_TEXT_BLOCK_TYPES = {"text", "input_text", "output_text", None}


def _extract_text(content) -> str:
    """Extract human-readable text from a content payload.

    Accepts (a) a plain string, or (b) a list of dict blocks. For dict blocks,
    pulls the `text` field when the `type` is text-like (text/input_text/
    output_text) OR when there is no `type` key at all (gemini's shape: a
    bare `[{"text": "..."}]`). Tool-use blocks and reasoning blocks have
    other type strings and are intentionally excluded.
    """
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for b in content:
            if not isinstance(b, dict):
                continue
            if b.get("type") in _TEXT_BLOCK_TYPES:
                t = b.get("text", "").strip()
                if t:
                    parts.append(t)
        return "\n".join(parts)
    return ""


# ── Persistence ──────────────────────────────────────────────────────────────

def _upsert_session(con: apsw.Connection, agent: str, project_id: str,
                    session_id: str, title: "str | None",
                    first_seen: str, now: str) -> None:
    """Insert or refresh a sessions row. Title is only set if provided."""
    con.execute(
        """INSERT INTO sessions (session_id, project_id, title, first_seen,
                                 last_updated, agent)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT(session_id) DO UPDATE SET
               title = COALESCE(excluded.title, sessions.title),
               last_updated = excluded.last_updated,
               agent = excluded.agent""",
        (session_id, project_id, title, first_seen, now, agent),
    )


def _upsert_ingested_file(con: apsw.Connection, agent: str, file_key: str,
                          session_id: str, project_id: str,
                          lines_read: int, mtime: float) -> None:
    """Insert or refresh an ingested_files row."""
    con.execute(
        """INSERT INTO ingested_files
               (file_path, session_id, project_id, lines_ingested,
                last_modified, agent)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT(file_path) DO UPDATE SET
               lines_ingested = excluded.lines_ingested,
               last_modified  = excluded.last_modified,
               agent          = excluded.agent""",
        (file_key, session_id, project_id, lines_read, mtime, agent),
    )


def _persist_message(con: apsw.Connection, agent: str, project_id: str,
                     session_id: str, uuid: str, role: str, text: str,
                     timestamp: "str | None", do_embed: bool,
                     model: "str | None" = None) -> int:
    """Insert one message row + (if vec is up) embedding. Returns rows changed
    (0 or 1). Shared by all per-agent parsers and the tool_error path."""
    try:
        # RETURNING is atomic: returns [(rowid,)] on insert, [] on conflict.
        # One round-trip instead of INSERT + SELECT after.
        ret = con.execute(
            """INSERT OR IGNORE INTO messages
               (uuid, session_id, project_id, role, content, timestamp, model, agent)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?) RETURNING rowid""",
            (uuid, session_id, project_id, role, text, timestamp, model, agent),
        ).fetchall()
        if not ret:
            return 0
        rowid = ret[0][0]
        if do_embed and _vec_ok(con):
            vec = embed(text)
            if vec:
                _vec_insert(con, rowid, vec)
        return 1
    except apsw.Error as _e:
        print(f"[warn] _persist_message failed for uuid={uuid!r}: "
              f"{type(_e).__name__}: {_e}", file=sys.stderr)
        return 0
