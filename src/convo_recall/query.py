"""
Read-path for convo-recall: search, tail, RRF fusion, and decay.

Provides:
  - search(con, query, ...) → hybrid FTS + vec search with RRF + optional
    time-decay reranking; emits human-formatted or JSON output.
  - tail(con, n, ...) → last N messages (single-session or cross-session
    timeline) with metadata header and ASCII / Unicode formatting.
  - _decay(timestamp, half_life_days) → exponential time-decay weight.
  - _safe_fts_query(query) → escape FTS5 special chars; cap at MAX_QUERY_LEN.
  - _resolve_project_ids(con, project) → display_name → project_id list with
    exact-first / LIKE-fallback resolution and a stderr warning on multi-hit.
  - _resolve_tail_session(con, project, agent) → most-recently-updated session
    matching the filters.
  - _fetch_context(con, session_id, ts, n) → before/after surrounding messages
    around a hit.
  - _tail_* formatting helpers (timestamp parsers, wrapping, glyph table).
  - MAX_QUERY_LEN, RRF_K, DECAY_HALF_LIFE_DAYS — read-path tuning constants.

Extracted from ingest.py in v0.4.0 (TD-008). Back-compat re-exports keep
`from convo_recall.ingest import search, tail, ...` working through one
release.

Test-monkeypatch contract: search() reads EMBED_SOCK through the ingest
namespace at call time (`from . import ingest as _ing; _ing.EMBED_SOCK`)
so test fixtures that point EMBED_SOCK at a non-existent path to force
the FTS-only path still flow through to this codepath. EMBED_SOCK itself
stays defined in ingest.py through v0.4.0 for the docstring-truth test;
A8 finalizes the move.
"""

import math
import sys
from datetime import datetime, timezone

from .db import _vec_ok
from .embed import embed, _vec_search


MAX_QUERY_LEN = 2048
RRF_K = 60
DECAY_HALF_LIFE_DAYS = 90


# ── Temporal decay ────────────────────────────────────────────────────────────

def _decay(timestamp: "str | None", half_life_days: int = DECAY_HALF_LIFE_DAYS) -> float:
    if not timestamp:
        return 1.0
    try:
        ts = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        age_days = (datetime.now(timezone.utc) - ts).days
        return math.pow(0.5, age_days / half_life_days)
    except Exception:
        return 1.0


# ── Search-time helpers ──────────────────────────────────────────────────────

def _fetch_context(con, session_id: str,
                   timestamp: "str | None", n: int) -> "tuple[list, list]":
    if not timestamp or n <= 0:
        return [], []
    before = con.execute(
        """SELECT role, SUBSTR(content, 1, 150) AS excerpt FROM messages
           WHERE session_id = ? AND timestamp < ?
           ORDER BY timestamp DESC LIMIT ?""",
        (session_id, timestamp, n),
    ).fetchall()
    after = con.execute(
        """SELECT role, SUBSTR(content, 1, 150) AS excerpt FROM messages
           WHERE session_id = ? AND timestamp > ?
           ORDER BY timestamp ASC LIMIT ?""",
        (session_id, timestamp, n),
    ).fetchall()
    return list(reversed(before)), after


def _safe_fts_query(query: str) -> str:
    """Convert a free-form user query into a safe FTS5 MATCH expression.

    FTS5 treats `-`, `.`, `:`, `(`, `*`, `AND`, `OR`, `NOT`, `NEAR` as
    operators / column refs. Passing a raw user string into
    `messages_fts MATCH ?` crashes on common inputs (`app-gemini` →
    "no such column: gemini"; `.*` → "syntax error near '.'").

    Strategy: split on whitespace, wrap each token in double quotes
    (FTS5's phrase syntax — special chars inside are literal), join
    with spaces. Multiple quoted tokens are implicit-AND'ed by FTS5,
    matching the prior behavior for normal multi-word queries.

    Edge cases:
      - empty input → returns a quoted empty string, which FTS5 reads
        as a no-match (caller prints "No results.").
      - embedded double quotes are doubled (FTS5's quote-escape
        convention).
      - tokens that consist entirely of FTS5-special chars (e.g. `.*`)
        end up as empty phrases, which FTS5 also no-matches cleanly.
    """
    if not query.strip():
        return '""'
    parts = []
    for token in query.split():
        # Strip leading/trailing FTS5 specials so a token like `.*` doesn't
        # produce an empty phrase that FTS5 treats as a syntax error in
        # some contexts. Internal punctuation is preserved (the tokenizer
        # handles word-boundary splitting inside the phrase).
        cleaned = token.strip('.*:()')
        if not cleaned:
            continue
        # Double-up any embedded double quotes per FTS5's escape convention.
        escaped = cleaned.replace('"', '""')
        parts.append(f'"{escaped}"')
    if not parts:
        return '""'
    return " ".join(parts)


_DEFAULT_TAIL_N = 30
_TAIL_WIDTH = 220                # per-message char budget before truncation
_TAIL_BODY_COLS = 76             # body wrap column (right-side body width)
_TAIL_ROLES = ("user", "assistant")
_TAIL_USER_LABEL = "YOU"         # display label for the user's own role

_TAIL_GLYPHS = {
    # `pipe` is shown next to agent rows; `pipe_user` (heavier) marks YOUR rows
    # so your own messages pop visually without color.
    "unicode": {"pipe": "│", "pipe_user": "┃", "dot": "·", "ellipsis": "…", "rule": "─"},
    "ascii":   {"pipe": "|", "pipe_user": "#", "dot": "-", "ellipsis": "...", "rule": "-"},
}


def _resolve_project_ids(con, project: str,
                          exact_only: bool = False) -> "tuple[list[str], list[str]]":
    """Resolve a display_name → list of project_ids.

    Strategy:
      1. Exact match on display_name (case-insensitive, NOCASE).
      2. If 0 hits AND not exact_only, fall back to LIKE %project%.
         Print a stderr warning when LIKE matched >1 project.

    Returns (project_ids, matched_display_names). Both empty when no match.
    """
    rows = con.execute(
        "SELECT project_id, display_name FROM projects "
        "WHERE display_name = ? COLLATE NOCASE",
        (project,),
    ).fetchall()
    if rows:
        return ([r["project_id"] for r in rows],
                [r["display_name"] for r in rows])
    if exact_only:
        return ([], [])
    rows = con.execute(
        "SELECT project_id, display_name FROM projects "
        "WHERE display_name LIKE ? COLLATE NOCASE",
        (f"%{project}%",),
    ).fetchall()
    if not rows:
        return ([], [])
    if len(rows) > 1:
        names = ", ".join(r["display_name"] for r in rows)
        print(f"[warn] '{project}' matched {len(rows)} projects: {names}",
              file=sys.stderr)
    return ([r["project_id"] for r in rows],
            [r["display_name"] for r in rows])


def _resolve_tail_session(con, project: "str | None",
                          agent: "str | None") -> "tuple[str, str] | None":
    """Pick the latest session matching project/agent filters.

    Returns (session_id, project_id) or None if no session matches.
    """
    where = []
    params: list = []
    if project:
        pids, _ = _resolve_project_ids(con, project)
        if not pids:
            return None
        placeholders = ",".join("?" * len(pids))
        where.append(f"project_id IN ({placeholders})")
        params.extend(pids)
    if agent:
        where.append("agent = ?")
        params.append(agent)
    sql = "SELECT session_id, project_id FROM sessions"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY last_updated DESC LIMIT 1"
    row = con.execute(sql, params).fetchone()
    if row is None:
        return None
    return (row[0], row[1])


# ── Tail formatting helpers ──────────────────────────────────────────────────

def _tail_parse_ts(ts: "str | None") -> "datetime | None":
    if not ts:
        return None
    try:
        s = ts.replace("Z", "+00:00")
        # Strip sub-second precision past microsecond cap if present.
        return datetime.fromisoformat(s)
    except (ValueError, AttributeError):
        return None


def _tail_format_ago(ts: "str | None",
                     now: "datetime | None" = None) -> str:
    """Return 'Xs ago' / 'Xm ago' / 'Xh ago' / 'Xd ago' / 'Xw ago'.

    `now` is injectable for deterministic tests; defaults to current UTC.
    Returns '' for unparseable timestamps and 'now' for sub-second elapsed.
    """
    dt = _tail_parse_ts(ts)
    if dt is None:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    if now is None:
        now = datetime.now(timezone.utc)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    secs = int((now - dt).total_seconds())
    if secs <= 0:
        return "now"
    if secs < 60:
        return f"{secs}s ago"
    if secs < 3600:
        return f"{secs // 60}m ago"
    if secs < 86400:
        return f"{secs // 3600}h ago"
    if secs < 604800:
        return f"{secs // 86400}d ago"
    return f"{secs // 604800}w ago"


def _tail_clock(ts: "str | None") -> str:
    """Format `ts` as HH:MM:SS, or 'unknown' if unparseable."""
    dt = _tail_parse_ts(ts)
    if dt is not None:
        return dt.strftime("%H:%M:%S")
    return (ts or "")[11:19] or "unknown"


def _tail_session_range(rows: list) -> str:
    """First-msg date + 'HH:MM→HH:MM' (or '→<date> HH:MM' across days)."""
    if not rows:
        return ""
    first = _tail_parse_ts(rows[0][1])
    last = _tail_parse_ts(rows[-1][1])
    if first is None or last is None:
        return ""
    if first.date() == last.date():
        return f"{first.date().isoformat()} {first.strftime('%H:%M')}"\
               f"→{last.strftime('%H:%M')}"
    return f"{first.date().isoformat()} {first.strftime('%H:%M')}"\
           f"→{last.date().isoformat()} {last.strftime('%H:%M')}"


def _tail_wrap(text: str, cols: int) -> "list[str]":
    """Word-wrap, preserving paragraph breaks. Empty list never returned."""
    import textwrap
    out: "list[str]" = []
    for para in text.split("\n"):
        para = para.rstrip()
        if not para:
            out.append("")
            continue
        wrapped = textwrap.wrap(
            para, width=cols,
            break_long_words=False,   # don't split URLs/identifiers
            break_on_hyphens=False,
        )
        out.extend(wrapped or [""])
    return out or [""]


# ── tail() ───────────────────────────────────────────────────────────────────

def tail(con, n: int = _DEFAULT_TAIL_N,
         session: "str | None" = None,
         project: "str | None" = None,
         agent: "str | None" = None,
         roles: "tuple[str, ...] | None" = None,
         width: int = _TAIL_WIDTH,
         expand: "set[int] | None" = None,
         ascii_only: bool = False,
         cols: int = _TAIL_BODY_COLS,
         json_: bool = False) -> int:
    """Print the last N messages in chronological order.

    Two modes:
      - `session` given → pull the last N messages from that one session.
      - `session` omitted → pull the last N messages by timestamp across
        every session matching `project` and `agent` (or globally when
        `project` is None). Session boundaries are rendered as inline
        rules so the reader can see when the conversation jumped sessions.

    Output is oldest-first so the latest message appears at the bottom.
    `expand` is a set of 1-based turn numbers to render in full (no
    truncation). `ascii_only` swaps Unicode glyphs for ASCII fallbacks.

    Returns: 0 on success, 1 if no messages match.
    """
    roles = tuple(roles) if roles else _TAIL_ROLES
    expand = expand or set()
    if n <= 0:
        n = _DEFAULT_TAIL_N

    resolved_project = project

    # ── single-session path ────────────────────────────────────────────────
    if session is not None:
        if resolved_project is None:
            # Recover display_name from the sessions+projects join.
            row = con.execute(
                "SELECT p.display_name FROM sessions s "
                "LEFT JOIN projects p ON p.project_id = s.project_id "
                "WHERE s.session_id = ?",
                (session,),
            ).fetchone()
            if row is not None and row["display_name"] is not None:
                resolved_project = row["display_name"]

        placeholders = ",".join(["?"] * len(roles))
        rows = con.execute(
            f"SELECT role, timestamp, content, agent, session_id "
            f"FROM messages "
            f"WHERE session_id = ? AND role IN ({placeholders}) "
            f"ORDER BY timestamp DESC LIMIT ?",
            [session, *roles, n],
        ).fetchall()
        rows = list(reversed(rows))

        if json_:
            import json as _json
            sess_meta = con.execute(
                "SELECT s.project_id, p.display_name FROM sessions s "
                "LEFT JOIN projects p ON p.project_id = s.project_id "
                "WHERE s.session_id = ?",
                (session,),
            ).fetchone()
            sess_pid = sess_meta["project_id"] if sess_meta else None
            sess_display = (sess_meta["display_name"] if sess_meta and
                            sess_meta["display_name"] else resolved_project)
            out = {
                "session_id": session,
                "project": resolved_project,
                "project_id": sess_pid,
                "display_name": sess_display,
                # DEPRECATED alias for one release — equals display_name.
                "project_slug": sess_display,
                "agent": agent,
                "n": n,
                "messages": [
                    {"role": r[0], "timestamp": r[1], "content": r[2],
                     "agent": r[3], "session_id": r[4]}
                    for r in rows
                ],
            }
            print(_json.dumps(out))
            return 0 if rows else 1

        if not rows:
            print(f"No messages found in session {session}.", file=sys.stderr)
            return 1
        # Falls through to the renderer at the bottom of this function.

    # ── cross-session path (no `session` given) ────────────────────────────
    else:
        where = [f"role IN ({','.join('?' * len(roles))})"]
        params: list = list(roles)
        suggestions: "list[str]" = []
        if project:
            pids, _ = _resolve_project_ids(con, project)
            if not pids:
                # Nothing matched exactly OR via LIKE → "Did you mean".
                like = con.execute(
                    "SELECT display_name FROM projects "
                    "WHERE display_name LIKE ? COLLATE NOCASE "
                    "  AND display_name != ? COLLATE NOCASE "
                    "ORDER BY display_name",
                    (f"%{project}%", project),
                ).fetchall()
                suggestions = [r["display_name"] for r in like[:3]]
                label = f"project='{project}'"
                if agent:
                    label += f", agent='{agent}'"
                if json_:
                    import json as _json
                    payload: dict = {
                        "session_id": None,
                        "project": project,
                        "agent": agent,
                        "n": n,
                        "messages": [],
                        "error": f"no messages found for {label}",
                    }
                    if suggestions:
                        payload["did_you_mean"] = suggestions
                    print(_json.dumps(payload))
                else:
                    print(f"No messages found for {label}.", file=sys.stderr)
                    if suggestions:
                        print(f"Did you mean: {', '.join(suggestions)}?",
                              file=sys.stderr)
                return 1
            placeholders = ",".join("?" * len(pids))
            where.append(f"project_id IN ({placeholders})")
            params.extend(pids)
        if agent:
            where.append("agent = ?")
            params.append(agent)

        sql = (f"SELECT role, timestamp, content, agent, session_id, "
               f"       project_id "
               f"FROM messages WHERE {' AND '.join(where)} "
               f"ORDER BY timestamp DESC LIMIT ?")
        rows = con.execute(sql, [*params, n]).fetchall()
        rows = list(reversed(rows))  # chronological — newest at the bottom

        # Resolve display_name for headers/JSON. When `project` is given,
        # LIKE may have matched multiple display_names — keep the union.
        if project:
            pids, names = _resolve_project_ids(con, project)
            resolved_project = (names[0] if len(names) == 1
                                else (project if not names else "+".join(names)))
        elif rows:
            # No `--project` filter — derive from the rows' actual project_ids.
            distinct = {r[5] for r in rows if r[5]}
            if len(distinct) == 1:
                dn = con.execute(
                    "SELECT display_name FROM projects WHERE project_id = ?",
                    (next(iter(distinct)),),
                ).fetchone()
                resolved_project = dn["display_name"] if dn else None
            else:
                resolved_project = "all projects"

        if json_:
            import json as _json
            # Build a per-session summary (in chronological order of last_msg).
            sess_seen: "dict[str, dict]" = {}
            for r in rows:
                sid = r[4]
                if sid not in sess_seen:
                    sess_seen[sid] = {
                        "session_id": sid,
                        "first_msg": r[1],
                        "last_msg": r[1],
                        "n_messages": 0,
                    }
                sess_seen[sid]["last_msg"] = r[1]
                sess_seen[sid]["n_messages"] += 1
            out = {
                "session_id": None,
                "project": project,
                "display_name": resolved_project,
                # DEPRECATED alias for one release — equals display_name.
                "project_slug": resolved_project,
                "agent": agent,
                "n": n,
                "sessions": list(sess_seen.values()),
                "messages": [
                    {"role": r[0], "timestamp": r[1], "content": r[2],
                     "agent": r[3], "session_id": r[4]}
                    for r in rows
                ],
            }
            if not rows and suggestions:
                out["did_you_mean"] = suggestions
            print(_json.dumps(out))
            return 0 if rows else 1

        if not rows:
            label = (f"project='{project}'" if project else "any project")
            if agent:
                label += f", agent='{agent}'"
            print(f"No messages found for {label}.", file=sys.stderr)
            return 1
        # Falls through to the renderer below.

    g = _TAIL_GLYPHS["ascii" if ascii_only else "unicode"]
    now = datetime.now(timezone.utc)
    cross_session = session is None

    # Each row is (role, ts, content, agent, session_id [, project_id]).
    # Pre-compute speaker labels and column widths so the metadata column
    # is uniform across all rows (Option-E layout). Newest message is #1
    # (reverse-numbered from the bottom up).
    total = len(rows)

    def _speaker_for(role: str, msg_agent: "str | None") -> str:
        if role == "user":
            return _TAIL_USER_LABEL
        if role == "assistant" and msg_agent:
            return msg_agent
        return role

    speakers = [_speaker_for(r[0], r[3]) for r in rows]
    speaker_w = max((len(s) for s in speakers), default=4)
    num_w = max(2, len(str(total))) + 1   # +1 for the leading '#'
    meta_strs: "list[str]" = []
    for i, r in enumerate(rows):
        rev_n = total - i                    # newest = #1
        clock = _tail_clock(r[1])
        ago = _tail_format_ago(r[1], now=now)
        meta_strs.append(
            f"{('#' + str(rev_n)):<{num_w}} {clock}  {ago:<8}  "
            f"{speakers[i]:<{speaker_w}} "
        )
    meta_w = max((len(m) for m in meta_strs), default=0)
    blank_meta = " " * meta_w

    # Distinct sessions (in chronological order of first appearance).
    sids_in_order: "list[str]" = []
    for r in rows:
        sid = r[4]
        if sid and (not sids_in_order or sids_in_order[-1] != sid):
            sids_in_order.append(sid)

    # ── header ───────────────────────────────────────────────────────────
    if cross_session:
        n_sess = len(sids_in_order)
        scope = resolved_project or "all projects"
        sess_phrase = (f"{total} messages across {n_sess} sessions"
                       if n_sess > 1
                       else f"{total} messages in 1 session")
        header_bits = [scope, sess_phrase]
    else:
        short_session = session[:8] if len(session) >= 8 else session
        header_bits = [
            f"session {short_session}",
            resolved_project or "?",
            f"{total} messages",
        ]
    rng = _tail_session_range(rows)
    if rng:
        header_bits.append(rng)
    if rows:
        header_bits.append(f"latest {_tail_format_ago(rows[-1][1], now=now)}")
    print(f" {g['dot']} ".join(header_bits))
    print()

    # ── messages ─────────────────────────────────────────────────────────
    truncated_turns: "list[int]" = []
    prev_sid: "str | None" = None

    for i, r in enumerate(rows):
        role, ts, content_raw, msg_agent, msg_sid = r[0], r[1], r[2], r[3], r[4]
        content = content_raw if content_raw is not None else ""
        rev_n = total - i
        force_full = rev_n in expand

        # Session-boundary rule (cross-session mode only). Skipped on the
        # very first row; only fires when the session_id actually changes.
        if cross_session and msg_sid and msg_sid != prev_sid:
            short = msg_sid[:8]
            date = (ts or "")[:10]
            print(f"  {g['rule']}{g['rule']} session {short} "
                  f"{g['dot']} {date} {g['rule']}{g['rule']}")
            print()
        prev_sid = msg_sid

        original_len = len(content)
        if not force_full and original_len > width:
            body = content[:width].rstrip()
            extra = original_len - width
            body += f" {g['ellipsis']} [+{extra} more]"
            truncated_turns.append(rev_n)
        else:
            body = content

        bar = g["pipe_user"] if role == "user" else g["pipe"]
        meta = meta_strs[i].ljust(meta_w)

        wrapped = _tail_wrap(body, cols)
        for j, line in enumerate(wrapped):
            prefix = meta if j == 0 else blank_meta
            print(f"{prefix}{bar}  {line}".rstrip())
        print()

    # ── footer hint ──────────────────────────────────────────────────────
    if truncated_turns and not expand:
        sample = truncated_turns[-1]   # most recent truncated turn (smallest #)
        print(f"(use `recall tail {n} --expand {sample}` "
              f"to see message #{sample} in full)")
    return 0


# ── search() ─────────────────────────────────────────────────────────────────

def search(con, query: str, limit: int = 10,
           recent: bool = False, project: "str | None" = None,
           context: int = 1, agent: "str | None" = None,
           json_: bool = False) -> None:
    # Read EMBED_SOCK through the ingest namespace at call time so test
    # fixtures patching `ingest.EMBED_SOCK` reach this codepath.
    from . import ingest as _ing

    if len(query) > MAX_QUERY_LEN:
        query = query[:MAX_QUERY_LEN]

    use_vec = _vec_ok(con) and _ing.EMBED_SOCK.exists()
    qvec = None
    if use_vec:
        qvec = embed(query, mode="query")
        use_vec = qvec is not None

    # FTS5 interprets `-`, `.`, `:`, `(`, `*`, etc. as query operators or
    # column refs, so passing a raw user query into `messages_fts MATCH ?`
    # crashes on common inputs (e.g. `app-gemini` → "no such column: gemini",
    # `.*` → "syntax error near '.'"). Wrap each whitespace-separated token
    # in double quotes — FTS5's phrase syntax — so special chars inside are
    # literal. Implicit-AND semantics across multiple quoted tokens preserve
    # the prior behavior for normal queries. Embedding path uses the raw
    # query (the model handles any string).
    fts_query = _safe_fts_query(query)

    # Pre-compute the rowid set for the (project, agent) filter so we can
    # narrow both FTS and vec result sets down before scoring.
    filter_rowids: "set[int] | None" = None
    resolved_project_ids: "list[str]" = []
    if project or agent:
        clauses = []
        params: list = []
        if project:
            resolved_project_ids, _ = _resolve_project_ids(con, project)
            if not resolved_project_ids:
                # No exact and no LIKE match → no rows; "did you mean" suggests
                # display_name LIKE matches.
                filter_rowids = set()
            else:
                placeholders = ",".join("?" * len(resolved_project_ids))
                clauses.append(f"project_id IN ({placeholders})")
                params.extend(resolved_project_ids)
        if agent:
            clauses.append("agent = ?")
            params.append(agent)
        if filter_rowids is None and clauses:
            where = " AND ".join(clauses)
            rows = con.execute(
                f"SELECT rowid FROM messages WHERE {where}", params
            ).fetchall()
            filter_rowids = {r[0] for r in rows}
        elif filter_rowids is None:
            filter_rowids = None  # no filter at all (shouldn't reach)
        if not filter_rowids:
            # "Did you mean" hint: surface display_names that fuzzily match
            # the passed --project. Slug variants no longer exist post-v4.
            suggestions = []
            if project:
                like = con.execute(
                    "SELECT display_name FROM projects "
                    "WHERE display_name LIKE ? COLLATE NOCASE "
                    "  AND display_name != ? COLLATE NOCASE "
                    "ORDER BY display_name",
                    (f"%{project}%", project),
                ).fetchall()
                suggestions = [r["display_name"] for r in like[:3]]
            if json_:
                import json as _json
                payload: dict = {
                    "query": query,
                    "project": project,
                    "agent": agent,
                    "n": limit,
                    "results": [],
                }
                if suggestions:
                    payload["did_you_mean"] = suggestions
                print(_json.dumps(payload))
            else:
                label = ", ".join(filter(None, [
                    f"project='{project}'" if project else None,
                    f"agent='{agent}'" if agent else None,
                ]))
                print(f"No messages found for {label}.")
                if suggestions:
                    print(f"Did you mean: {', '.join(suggestions)}?")
            return
    project_rowids = filter_rowids  # keep alias to minimize downstream churn

    # Corpus mismatch guard: fall back to FTS if vector coverage < 95%
    if use_vec and project and _vec_ok(con):
        cov = con.execute(
            """SELECT COUNT(*) AS total,
                      SUM(CASE WHEN v.rowid IS NOT NULL THEN 1 ELSE 0 END) AS embedded
               FROM messages m
               LEFT JOIN message_vecs v ON v.rowid = m.rowid
               WHERE m.project_id IN ({})""".format(
                   ",".join("?" * len(resolved_project_ids))
               ),
            tuple(resolved_project_ids),
        ).fetchone()
        total, embedded = cov[0], cov[1] or 0
        if total > 0 and (embedded / total) < 0.95:
            pct = embedded * 100 // total
            print(f"[warn] Vector coverage {pct}% (<95%) for '{project}' — using FTS only. "
                  f"Run `recall ingest` to heal.", file=sys.stderr)
            use_vec = False

    # Filter-aware retrieval strategy. When the (project, agent) filter set is
    # a small fraction of the corpus, a global top-100 prefilter rarely
    # overlaps with it (recall cliff). Choose strategy by cardinality:
    #   - no filter / >= 5000 rows : global top-100 prefilter, intersect after
    #   - 500..4999                : bump prefilter to min(n*2, 1000)
    #   - < 500                    : push filter into FTS, brute-force vec
    filter_size = len(filter_rowids) if filter_rowids is not None else None
    if filter_size is None or filter_size >= 5_000:
        prefilter_k = 100
    elif filter_size >= 500:
        prefilter_k = min(filter_size * 2, 1000)
    else:
        prefilter_k = filter_size  # exact retrieval below

    if use_vec:
        # FTS side: when the filter is small, push `rowid IN (...)` into the
        # query so we don't waste a global top-100 fetch that gets filtered
        # to nothing.
        if filter_rowids is not None and filter_size < 5_000:
            placeholders = ",".join("?" * filter_size)
            fts_rows = con.execute(
                f"""SELECT m.rowid, ROW_NUMBER() OVER (ORDER BY rank) AS fts_rank
                    FROM messages_fts
                    JOIN messages m ON messages_fts.rowid = m.rowid
                    WHERE messages_fts MATCH ? AND m.rowid IN ({placeholders})
                    LIMIT ?""",
                (fts_query, *filter_rowids, prefilter_k),
            ).fetchall()
            fts_map = {r["rowid"]: r["fts_rank"] for r in fts_rows}
        else:
            fts_rows = con.execute(
                """SELECT m.rowid, ROW_NUMBER() OVER (ORDER BY rank) AS fts_rank
                   FROM messages_fts
                   JOIN messages m ON messages_fts.rowid = m.rowid
                   WHERE messages_fts MATCH ?
                   LIMIT ?""",
                (fts_query, prefilter_k),
            ).fetchall()
            fts_map = {r["rowid"]: r["fts_rank"] for r in fts_rows
                       if project_rowids is None or r["rowid"] in project_rowids}

        vec_rowids = _vec_search(con, qvec, k=prefilter_k,
                                 restrict_rowids=filter_rowids)
        if filter_rowids is None or filter_size < 500:
            # _vec_search already restricted; trust the order
            vec_map = {rid: rank + 1 for rank, rid in enumerate(vec_rowids)}
        else:
            vec_map = {rid: rank + 1 for rank, rid in enumerate(vec_rowids)
                       if rid in filter_rowids}

        all_rowids = list(set(fts_map) | set(vec_map))

        if recent and all_rowids:
            placeholders = ",".join("?" * len(all_rowids))
            ts_rows = con.execute(
                f"SELECT rowid, timestamp FROM messages WHERE rowid IN ({placeholders})",
                all_rowids,
            ).fetchall()
            ts_map = {r["rowid"]: r["timestamp"] for r in ts_rows}
        else:
            ts_map = {}

        def _score(rid: int) -> float:
            rrf = (1.0 / (RRF_K + fts_map.get(rid, 101))
                   + 1.0 / (RRF_K + vec_map.get(rid, 101)))
            if recent:
                rrf *= _decay(ts_map.get(rid))
            return rrf

        scored = sorted(all_rowids, key=_score, reverse=True)[:limit]
        if not scored:
            rows = []
        else:
            placeholders = ",".join("?" * len(scored))
            rows = con.execute(
                f"""SELECT rowid, session_id, project_id, role, timestamp, agent,
                           SUBSTR(content, 1, 300) AS excerpt
                    FROM messages WHERE rowid IN ({placeholders})""",
                scored,
            ).fetchall()
    else:
        # FTS-only path. When a filter is set, push `rowid IN (...)` into the
        # query so the filter is honored (without it, --agent X foo against a
        # corpus dominated by another agent silently returns 0 hits — the
        # original recall cliff).
        if filter_rowids is not None:
            placeholders = ",".join("?" * filter_size)
            rows = con.execute(
                f"""SELECT m.rowid, m.session_id, m.project_id, m.role,
                           m.timestamp, m.agent,
                           snippet(messages_fts, 0, '[', ']', '…', 20) AS excerpt
                    FROM messages_fts
                    JOIN messages m ON messages_fts.rowid = m.rowid
                    WHERE messages_fts MATCH ? AND m.rowid IN ({placeholders})
                    ORDER BY rank
                    LIMIT ?""",
                (fts_query, *filter_rowids, limit),
            ).fetchall()
        else:
            rows = con.execute(
                """SELECT m.rowid, m.session_id, m.project_id, m.role,
                          m.timestamp, m.agent,
                          snippet(messages_fts, 0, '[', ']', '…', 20) AS excerpt
                   FROM messages_fts
                   JOIN messages m ON messages_fts.rowid = m.rowid
                   WHERE messages_fts MATCH ?
                   ORDER BY rank
                   LIMIT ?""",
                (fts_query, limit),
            ).fetchall()

    if not rows:
        if json_:
            import json as _json
            print(_json.dumps({
                "query": query,
                "project": project,
                "agent": agent,
                "n": limit,
                "results": [],
            }))
        else:
            print("No results.")
        return

    mode = ("hybrid+recent" if use_vec and recent
            else "hybrid" if use_vec
            else "fts")

    # Build a project_id → display_name lookup for output formatting (item 14).
    pid_set = {r["project_id"] for r in rows}
    if pid_set:
        placeholders = ",".join("?" * len(pid_set))
        pid_to_name = {
            r["project_id"]: r["display_name"]
            for r in con.execute(
                f"SELECT project_id, display_name FROM projects "
                f"WHERE project_id IN ({placeholders})",
                tuple(pid_set),
            ).fetchall()
        }
    else:
        pid_to_name = {}

    if json_:
        import json as _json
        results = []
        for r in rows:
            display = pid_to_name.get(r["project_id"], r["project_id"])
            results.append({
                "session_id": r["session_id"],
                "project_id": r["project_id"],
                "display_name": display,
                # DEPRECATED alias for one release — equals display_name.
                "project_slug": display,
                "agent": r["agent"],
                "role": r["role"],
                "timestamp": r["timestamp"],
                "snippet": r["excerpt"],
            })
        print(_json.dumps({
            "query": query,
            "project": project,
            "agent": agent,
            "mode": mode,
            "n": limit,
            "results": results,
        }))
        return

    print(f"[{mode} search]\n")
    # Only show the agent tag when the result set actually mixes agents (or
    # the user explicitly filtered to a non-claude agent). Single-Claude
    # users — the entire pre-v0.2.0 cohort — see output identical to before.
    distinct_agents = {r["agent"] for r in rows}
    show_agent = len(distinct_agents) > 1 or distinct_agents != {"claude"}
    for r in rows:
        ts = (r["timestamp"] or "")[:10]
        role_label = "[⚠ error]" if r["role"] == "tool_error" else f"[{r['role']}]"
        agent_tag = f"[{r['agent']}] " if show_agent else ""
        display = pid_to_name.get(r["project_id"], r["project_id"])
        print(f"[{display}] {agent_tag}{role_label} {ts}")
        if context > 0:
            before, after = _fetch_context(con, r["session_id"], r["timestamp"], context)
            for c in before:
                print(f"  ↑ [{c['role']}] {c['excerpt']}")
        print(f"  {r['excerpt']}")
        if context > 0:
            for c in after:
                print(f"  ↓ [{c['role']}] {c['excerpt']}")
        print()
