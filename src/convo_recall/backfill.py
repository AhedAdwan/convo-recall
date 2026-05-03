"""
Backfill operations for convo-recall: embed-backfill, tool_error_backfill,
backfill_clean, backfill_redact, chunk_backfill.

These are batch-mode operations invoked by `recall <subcommand>` CLI entry
points; they do NOT run on the per-message ingest hot path.

Provides:
  - embed_backfill(con) — re-embed any rows with missing message_vecs.
  - tool_error_backfill(con) — full-scan every agent's session JSONLs and
    insert tool_error rows the in-place ingest may have missed (e.g. pre-
    fix sessions, foreign DBs); idempotent via INSERT OR IGNORE on uuid PK.
  - backfill_clean(con, confirm) — re-run _clean_content over historical
    rows (ANSI / box-drawing / XML-pair stripping).
  - backfill_redact(con, confirm) — re-run secret-redaction patterns over
    historical messages.content + rebuild FTS.
  - chunk_backfill(con, confirm) — re-embed long messages through the new
    sidecar chunker (semantics shifted post-TD-007 reframe; this no longer
    populates a chunk_vecs table — see CHANGELOG v0.3.3).
  - _backfill_insert_tool_error(...) — shared insert helper used by all
    three per-agent walkers.
  - _backfill_claude_tool_errors / _backfill_codex_tool_errors /
    _backfill_gemini_tool_errors — per-agent JSONL walkers.
  - _confirm_destructive(label, n_changed, samples, confirm) — shared
    user-confirm prompt for in-place mutations.

Extracted from ingest.py in v0.4.0 (TD-008). Back-compat re-exports keep
`from convo_recall.ingest import embed_backfill, ...` working through one
release.

Temporary cross-module deps (resolved in A7):
The walkers and `backfill_clean` need write-path helpers that live in
ingest.py through A6 (`_clean_content`, `_iter_*_files`, the per-agent
error-extractors `_codex_event_msg_error` / `_codex_fco_error` /
`_gemini_record_error` / `_gemini_tool_call_error` /
`_extract_tool_result_text` / `_is_error_result`, the path helpers
`_session_id_from_path` / `_load_gemini_aliases`, and the constants
PROJECTS_DIR / GEMINI_TMP / CODEX_SESSIONS / _GEMINI_ALIAS_PATH).

These are accessed lazily via `from . import ingest as _ing; _ing.X` so:
  (a) a load-time cycle is avoided (ingest.py imports backfill near its
      top while backfill needs symbols defined later in ingest),
  (b) test fixtures monkeypatching `ingest.PROJECTS_DIR` etc. flow into
      the production code path, and
  (c) when A7 moves these symbols into `ingest/{writer,scan,claude,
      codex,gemini}.py`, the `_ing.X` accesses can be replaced with
      direct imports of the new homes — one mechanical pass.
"""

import json
import os
import sys

import apsw

from . import redact as _redact
from .db import _upsert_project
from .embed import _wait_for_embed_socket
from .identity import (
    _display_name,
    _gemini_hash_project_id,
    _legacy_claude_slug,
    _legacy_project_id,
    _project_id,
)
# Direct imports of write-path helpers that have a stable home post-A7 and
# aren't test-monkeypatched on `ingest`. The remaining `_ing.X` accesses
# inside function bodies (for `_vec_ok`, `embed`, `_vec_insert`, `EMBED_SOCK`,
# `PROJECTS_DIR`, `_load_gemini_aliases`) ARE monkeypatched in tests and
# must keep flowing through the ingest namespace.
from .ingest.writer import _clean_content
from .ingest.claude import (
    _extract_tool_result_text,
    _is_error_result,
    _session_id_from_path,
)
from .ingest.codex import (
    _codex_event_msg_error,
    _codex_fco_error,
    _iter_codex_files,
)
from .ingest.gemini import (
    _gemini_record_error,
    _gemini_tool_call_error,
    _iter_gemini_files,
)


def embed_backfill(con: apsw.Connection) -> None:
    from . import _progress
    from . import ingest as _ing

    if not _ing._vec_ok(con):
        print("sqlite-vec not loaded", file=sys.stderr)
        return
    # Same race fix as scan_all: the wizard's chain calls embed_backfill
    # right after spawning the sidecar, before the socket is bound.
    # Wait up to 30s for the socket to appear before declaring failure.
    if not _wait_for_embed_socket(timeout_s=30.0, verbose=True):
        print("Embed socket not found (waited 30s)", file=sys.stderr)
        return
    existing = {r[0] for r in con.execute("SELECT rowid FROM message_vecs").fetchall()}
    rows = con.execute("SELECT rowid, content FROM messages").fetchall()
    pending = [r for r in rows if r["rowid"] not in existing]
    total = len(pending)
    print(f"Embedding {total:,} messages…")

    # If we're called inside a multi-phase chain (e.g. _backfill-chain),
    # the parent has already created the progress run with both phases
    # pre-declared. Otherwise (standalone `recall embed-backfill`),
    # create our own single-phase run so the user still sees a bar in
    # `recall stats`.
    own_run = _progress.read_status() is None
    if own_run:
        _progress.start_run([("embed-backfill", total)])
    else:
        _progress.set_phase_total("embed-backfill", total)

    done = 0
    try:
        for r in pending:
            vec = _ing.embed(r["content"])
            if vec:
                _ing._vec_insert(con, r["rowid"], vec)
                done += 1
            # Update the file every 100 rows — keeps the progress display
            # fresh without thrashing the disk on per-row writes.
            if done % 100 == 0 and done > 0:
                _progress.update_phase("embed-backfill", done)
            if done % 500 == 0 and done > 0:
                print(f"  {done}/{total}…")
        _progress.finish_phase("embed-backfill")
    finally:
        if own_run:
            _progress.finish_run()
    print(f"Done. {done:,} embeddings written.")


def _confirm_destructive(label: str, n_changed: int,
                         samples: "list[tuple[str, str]]",
                         confirm: bool) -> bool:
    """Shared preview + confirmation gate for backfill mutations.

    Prints a danger banner, the row count, up to 3 before/after diffs, and:
    - non-TTY without confirm → refuse, return False
    - TTY without confirm → prompt 'YES', return True only on exact match
    - confirm=True → skip prompt, return True

    `samples` is a list of (before, after) string pairs to show the user.
    """
    print()
    print("🔥" * 35)
    print(f"☠️  DANGER — `{label}` will REWRITE {n_changed:,} rows IN PLACE  ☠️")
    print("🔥" * 35)
    print()
    print("This is an in-place mutation of message content. The original text")
    print("is replaced with the new text. There is NO undo and NO automatic")
    print("backup. If the cleaning/redaction logic has a bug, every changed")
    print("row carries the bug.")
    print()
    if samples:
        print(f"📊 Sample of changes (first {len(samples)} of {n_changed:,}):")
        for i, (before, after) in enumerate(samples, 1):
            b = before if len(before) <= 100 else before[:100] + "…"
            a = after  if len(after)  <= 100 else after[:100]  + "…"
            print(f"  ── #{i} ──")
            print(f"    BEFORE: {b!r}")
            print(f"    AFTER : {a!r}")
        print()
    if n_changed == 0:
        print("✅ Nothing to do — every row is already up to date.")
        return False

    if confirm:
        print("💥 --confirm passed — proceeding without prompt.\n")
        return True
    if not sys.stdin.isatty():
        print("⚠️  DRY-RUN — non-interactive shell.")
        print(f"Re-run with --confirm to apply:")
        print(f"    recall {label} --confirm")
        return False
    print("⚠️  ⚠️  ⚠️   ARE YOU SURE?   ⚠️  ⚠️  ⚠️\n")
    response = input(
        "Type 'YES' (uppercase) to apply the mutation, anything else to cancel: "
    ).strip()
    if response != "YES":
        print("\n✅ Aborted. No rows changed.")
        return False
    print()
    return True


def backfill_clean(con: apsw.Connection, confirm: bool = False) -> None:
    """Re-run content cleaning on every message.

    Defaults to DRY-RUN (preview only). Pass confirm=True (or --confirm on
    the CLI) to actually mutate rows. Pattern matches `recall forget`.
    """
    from . import ingest as _ing  # _clean_content lives in ingest until A7

    rows = con.execute("SELECT rowid, content FROM messages").fetchall()
    pending: "list[tuple[int, str, str]]" = []  # (rowid, old, new)
    for r in rows:
        new = _ing._clean_content(r["content"])
        if new != r["content"]:
            pending.append((r["rowid"], r["content"], new))

    samples = [(old, new) for _, old, new in pending[:3]]
    if not _confirm_destructive("backfill-clean", len(pending), samples, confirm):
        return

    for rowid, _old, new in pending:
        con.execute("UPDATE messages SET content = ? WHERE rowid = ?",
                    (new, rowid))
    print(f"Cleaned {len(pending):,} messages. Rebuilding FTS…")
    con.execute("INSERT INTO messages_fts(messages_fts) VALUES('rebuild')")
    print("Done.")


def backfill_redact(con: apsw.Connection, confirm: bool = False) -> None:
    """Re-apply secret redaction to all existing rows + rebuild FTS.

    Use this after upgrading to a version with secret redaction, or after
    `recall doctor --scan-secrets` reports findings on legacy rows that
    were ingested before redaction was enabled.

    Defaults to DRY-RUN (preview only). Pass confirm=True (or --confirm on
    the CLI) to actually mutate rows.
    """
    rows = con.execute("SELECT rowid, content FROM messages").fetchall()
    pending: "list[tuple[int, str, str]]" = []
    for r in rows:
        new = _redact.redact_secrets(r["content"])
        if new != r["content"]:
            pending.append((r["rowid"], r["content"], new))

    samples = [(old, new) for _, old, new in pending[:3]]
    if not _confirm_destructive("backfill-redact", len(pending), samples, confirm):
        return

    for rowid, _old, new in pending:
        con.execute("UPDATE messages SET content = ? WHERE rowid = ?",
                    (new, rowid))
    print(f"Redacted {len(pending):,} messages. Rebuilding FTS…")
    con.execute("INSERT INTO messages_fts(messages_fts) VALUES('rebuild')")
    print("Done.")


def chunk_backfill(con: apsw.Connection, confirm: bool = False) -> None:
    """Re-embed long messages whose vectors may pre-date server-side chunking.
    Chunking now happens inside the sidecar — one HTTP call per message.

    Defaults to DRY-RUN. Less catastrophic than backfill-clean/redact
    (only embeddings change, message text stays intact) but still consumes
    GPU/CPU and time, so we gate it behind a confirm.
    """
    from . import ingest as _ing  # EMBED_SOCK lives in ingest through v0.4.0

    _BACKFILL_MIN_CHARS = 1800  # ≈ 450 tokens; shorter texts always fit in model window
    if not _ing._vec_ok(con) or not _ing.EMBED_SOCK.exists():
        print("Embed service not available", file=sys.stderr)
        return
    rows = con.execute(
        "SELECT rowid, content FROM messages WHERE LENGTH(content) > ?",
        (_BACKFILL_MIN_CHARS,),
    ).fetchall()
    total = len(rows)

    print()
    print("─" * 70)
    print(f"📊 chunk-backfill: {total:,} long message(s) (>{_BACKFILL_MIN_CHARS} chars) "
          f"would be re-embedded.")
    print("─" * 70)
    print("This re-runs the embedding model — message TEXT is not touched, only")
    print("the stored vectors are replaced. Lower risk than backfill-clean/redact,")
    print("but still consumes GPU/CPU and re-downloads chunks via the sidecar.")
    print()

    if total == 0:
        print("✅ Nothing to do.")
        return

    if not confirm:
        if not sys.stdin.isatty():
            print("⚠️  DRY-RUN — non-interactive shell.")
            print("Re-run with --confirm to apply: recall chunk-backfill --confirm")
            return
        response = input(
            f"Type 'YES' (uppercase) to re-embed {total:,} messages, "
            "anything else to cancel: "
        ).strip()
        if response != "YES":
            print("\n✅ Aborted.")
            return
        print()

    print(f"Re-embedding {total:,} long messages via sidecar chunking…")
    done = 0
    for r in rows:
        vec = _ing.embed(r["content"])
        if vec:
            _ing._vec_insert(con, r["rowid"], vec)
            done += 1
        if done % 100 == 0 and done > 0:
            print(f"  {done}/{total}…")
    print(f"Done. {done:,} re-embedded.")


def _backfill_insert_tool_error(con: apsw.Connection, agent: str,
                                 project_id: str, session_id: str,
                                 uuid: str, text: str,
                                 timestamp: "str | None") -> int:
    """Insert one tool_error row + embed. Returns 1 if inserted, 0 otherwise.
    Catches apsw.Error and surfaces a [warn] line so backfill keeps walking."""
    from . import ingest as _ing
    try:
        ret = con.execute(
            """INSERT OR IGNORE INTO messages
               (uuid, session_id, project_id, role, content, timestamp, model, agent)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?) RETURNING rowid""",
            (uuid, session_id, project_id, "tool_error", text, timestamp,
             None, agent),
        ).fetchall()
    except apsw.Error as _e:
        print(f"[warn] tool_error_backfill insert failed: "
              f"{type(_e).__name__}: {_e}", file=sys.stderr)
        return 0
    if not ret:
        return 0
    if _ing._vec_ok(con):
        vec = _ing.embed(text)
        if vec:
            try:
                _ing._vec_insert(con, ret[0][0], vec)
            except apsw.Error as _e:
                print(f"[warn] tool_error_backfill vec insert failed: "
                      f"{type(_e).__name__}: {_e}", file=sys.stderr)
    return 1


def _backfill_claude_tool_errors(con: apsw.Connection) -> int:
    """Walk Claude project JSONLs and harvest tool_result.is_error blocks.
    Mirrors the in-place ingest loop's logic at ingest_file but full-scans
    every file (no lines_already guard)."""
    from . import ingest as _ing  # PROJECTS_DIR stays in package init through v0.4.0
    PROJECTS_DIR = _ing.PROJECTS_DIR

    indexed = 0
    if not PROJECTS_DIR.exists():
        return 0
    for project_dir in PROJECTS_DIR.iterdir():
        if not project_dir.is_dir():
            continue
        for pattern in ("*.jsonl", "*/subagents/*.jsonl"):
            for jsonl_path in project_dir.glob(pattern):
                session_id = _session_id_from_path(jsonl_path)
                recovered_cwd: "str | None" = None
                try:
                    with open(jsonl_path, "r", errors="replace") as fh:
                        for i, line in enumerate(fh):
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
                    continue
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
                try:
                    with open(jsonl_path, "r", errors="replace") as f:
                        for lineno, raw in enumerate(f):
                            try:
                                rec = json.loads(raw)
                            except (json.JSONDecodeError, ValueError):
                                continue
                            if rec.get("type") != "user":
                                continue
                            msg = rec.get("message", {})
                            content_blocks = msg.get("content", [])
                            if not isinstance(content_blocks, list):
                                continue
                            timestamp = rec.get("timestamp")
                            for block in content_blocks:
                                if not isinstance(block, dict):
                                    continue
                                if block.get("type") != "tool_result":
                                    continue
                                raw_tr = _extract_tool_result_text(block)
                                if not raw_tr:
                                    continue
                                if not (block.get("is_error", False)
                                        or _is_error_result(raw_tr)):
                                    continue
                                tool_use_id = block.get("tool_use_id", f"tr{lineno}")
                                tr_uuid = f"{session_id}:tr:{tool_use_id}"
                                tr_text = _clean_content(raw_tr[:500])
                                if not tr_text:
                                    continue
                                indexed += _backfill_insert_tool_error(
                                    con, "claude", project_id, session_id,
                                    tr_uuid, tr_text, timestamp,
                                )
                except OSError:
                    pass
    return indexed


def _backfill_codex_tool_errors(con: apsw.Connection) -> int:
    """Walk Codex rollout JSONLs and harvest event_msg failures + FCO
    fallback. Project_id derived from session_meta.payload.cwd."""
    indexed = 0
    for jsonl_path in _iter_codex_files():
        session_id = jsonl_path.stem
        project_id = _legacy_project_id("codex_unknown")
        display_name: str = "codex_unknown"
        cwd_real: "str | None" = None
        try:
            with open(jsonl_path, "r", errors="replace") as fh:
                for i, line in enumerate(fh):
                    if i > 50:  # session_meta is always first
                        break
                    try:
                        d = json.loads(line)
                    except (json.JSONDecodeError, ValueError):
                        continue
                    if d.get("type") != "session_meta":
                        continue
                    payload = d.get("payload", {})
                    session_id = payload.get("id", session_id)
                    cwd = payload.get("cwd")
                    if cwd:
                        project_id = _project_id(cwd)
                        display_name = _display_name(cwd)
                        cwd_real = os.path.realpath(cwd)
                    break
        except OSError:
            continue
        _upsert_project(con, project_id, display_name, cwd_real)
        try:
            with open(jsonl_path, "r", errors="replace") as f:
                for lineno, raw in enumerate(f):
                    try:
                        rec = json.loads(raw)
                    except (json.JSONDecodeError, ValueError):
                        continue
                    ttype = rec.get("type")
                    timestamp = rec.get("timestamp")
                    if ttype == "event_msg":
                        hit = _codex_event_msg_error(rec)
                        if hit is None:
                            continue
                        kind, tr_text = hit
                        pl = rec.get("payload", {})
                        key = (pl.get("call_id") or pl.get("turn_id")
                               or str(lineno))
                        tr_uuid = f"{session_id}:tr:codex:{kind}:{key}"
                        cleaned = _clean_content(tr_text)
                        if not cleaned:
                            continue
                        indexed += _backfill_insert_tool_error(
                            con, "codex", project_id, session_id,
                            tr_uuid, cleaned, timestamp,
                        )
                    elif ttype == "response_item":
                        pl = rec.get("payload", {})
                        if pl.get("type") != "function_call_output":
                            continue
                        fco_text = _codex_fco_error(rec)
                        if fco_text is None:
                            continue
                        call_id = pl.get("call_id") or str(lineno)
                        tr_uuid = f"{session_id}:tr:codex:fco:{call_id}"
                        cleaned = _clean_content(fco_text)
                        if not cleaned:
                            continue
                        indexed += _backfill_insert_tool_error(
                            con, "codex", project_id, session_id,
                            tr_uuid, cleaned, timestamp,
                        )
        except OSError:
            pass
    return indexed


def _backfill_gemini_tool_errors(con: apsw.Connection) -> int:
    """Walk Gemini session JSONLs and harvest top-level error/warning
    records + toolCalls[] with status in (error, cancelled). Project_id
    derived from header cwd → alias map → hash-dir fallback."""
    # _load_gemini_aliases is monkeypatched in tests (test_ingest_project_id.py,
    # test_migration_project_id.py) — keep flowing through the ingest namespace.
    from . import ingest as _ing
    indexed = 0
    aliases = _ing._load_gemini_aliases()
    for jsonl_path in _iter_gemini_files():
        session_id = jsonl_path.stem
        hash_dir = jsonl_path.parent.parent.name
        project_id: "str | None" = None
        display_name: "str | None" = None
        cwd_real: "str | None" = None
        aliased_cwd = aliases.get(hash_dir)
        if aliased_cwd:
            project_id = _project_id(aliased_cwd)
            display_name = _display_name(aliased_cwd)
            cwd_real = os.path.realpath(aliased_cwd)
        # Read header for cwd / sessionId override
        try:
            with open(jsonl_path, "r", errors="replace") as fh:
                first = fh.readline()
                try:
                    head = json.loads(first)
                    if isinstance(head, dict):
                        if "sessionId" in head and "type" not in head:
                            session_id = head.get("sessionId", session_id)
                            cwd = head.get("cwd") or head.get("projectDir")
                            if cwd and project_id is None:
                                project_id = _project_id(cwd)
                                display_name = _display_name(cwd)
                                cwd_real = os.path.realpath(cwd)
                except (json.JSONDecodeError, ValueError):
                    pass
        except OSError:
            continue
        if project_id is None:
            project_id = _gemini_hash_project_id(hash_dir)
            display_name = hash_dir
            cwd_real = None
        _upsert_project(con, project_id, display_name or hash_dir, cwd_real)
        try:
            with open(jsonl_path, "r", errors="replace") as f:
                for lineno, raw in enumerate(f):
                    try:
                        rec = json.loads(raw)
                    except (json.JSONDecodeError, ValueError):
                        continue
                    if not isinstance(rec, dict):
                        continue
                    if "$set" in rec or ("sessionId" in rec and "type" not in rec):
                        continue
                    timestamp = rec.get("timestamp")
                    rtype = rec.get("type")
                    if rtype in ("error", "warning"):
                        hit = _gemini_record_error(rec)
                        if hit is None:
                            continue
                        kind, tr_text = hit
                        rec_id = rec.get("id") or str(lineno)
                        tr_uuid = f"{session_id}:tr:gemini:{kind}:{rec_id}"
                        cleaned = _clean_content(tr_text)
                        if not cleaned:
                            continue
                        indexed += _backfill_insert_tool_error(
                            con, "gemini", project_id, session_id,
                            tr_uuid, cleaned, timestamp,
                        )
                    elif rtype == "gemini":
                        for tc in rec.get("toolCalls") or []:
                            tr_text = _gemini_tool_call_error(tc)
                            if tr_text is None:
                                continue
                            tc_id = tc.get("id") or f"{lineno}-{id(tc)}"
                            tr_uuid = f"{session_id}:tr:gemini:tool:{tc_id}"
                            cleaned = _clean_content(tr_text)
                            if not cleaned:
                                continue
                            indexed += _backfill_insert_tool_error(
                                con, "gemini", project_id, session_id,
                                tr_uuid, cleaned, timestamp,
                            )
        except OSError:
            pass
    return indexed


def tool_error_backfill(con: apsw.Connection) -> None:
    """Walk every agent's session files and insert any tool_error rows
    that the in-place ingest missed (e.g. pre-fix sessions, foreign DBs).
    Always full-scans (no lines_already / mtime guard). Idempotent via
    INSERT OR IGNORE on the uuid PK."""
    n_claude = _backfill_claude_tool_errors(con)
    n_codex = _backfill_codex_tool_errors(con)
    n_gemini = _backfill_gemini_tool_errors(con)
    total = n_claude + n_codex + n_gemini
    print(f"Indexed {total:,} tool_result error(s) "
          f"(claude={n_claude:,}, codex={n_codex:,}, gemini={n_gemini:,}).")
