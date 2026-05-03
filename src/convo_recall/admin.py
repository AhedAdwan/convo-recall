"""
Admin / observability commands for convo-recall: stats, doctor, forget.

These are CLI-only entry points that surface DB state and let the user
remove rows safely. None of them runs on the per-message ingest hot path.

Provides:
  - stats(con) — DB row counts, embedding coverage, agent breakdown,
    sidecar/embed-extra status, in-flight progress bar.
  - doctor(con, scan_secrets=False) — health checks: DB-path drift,
    embed sidecar reachability, project-id integrity, hook installation,
    stale `.bak` files. With `scan_secrets=True` also surfaces
    credential-shaped tokens already present in messages.content.
  - forget(con, *, session/pattern/before/project/agent/uuid, confirm) —
    scoped deletion with mutually-exclusive scope flags. Defaults to
    dry-run preview; `confirm=True` performs the delete and prunes
    message_vecs / sessions / ingested_files.
  - _render_phase_bar / _render_progress_bar — top-of-stats bars when
    a backfill chain is running.
  - _scan_stale_bak_files / _BAK_STALE_AGE_DAYS — `.bak` file aging.

Extracted from ingest.py in v0.4.0 (TD-008). Back-compat re-exports keep
`from convo_recall.ingest import stats, doctor, forget` working through
one release.

Test-monkeypatch contract: doctor() and stats() read DB_PATH and
EMBED_SOCK at call time via `from . import ingest as _ing` so test
fixtures patching `ingest.DB_PATH` / `ingest.EMBED_SOCK` reach this
codepath. Both constants stay defined in ingest.py through v0.4.0
(docstring-truth rule); A8 finalizes the move.
"""

import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

import apsw

from . import redact as _redact
from .db import _vec_ok
from .embed import _vec_count
from .query import _resolve_project_ids


_BAK_STALE_AGE_DAYS = 30


def _scan_stale_bak_files(db_dir: Path) -> "list[tuple[Path, float, int]]":
    """Return a list of `(path, age_days, size_bytes)` for `.bak` files in
    `db_dir` older than `_BAK_STALE_AGE_DAYS`. Used by `recall doctor`."""
    if not db_dir.exists():
        return []
    out: "list[tuple[Path, float, int]]" = []
    now = datetime.now().timestamp()
    for p in db_dir.glob("*.bak"):
        try:
            stat = p.stat()
            age_days = (now - stat.st_mtime) / 86400.0
            if age_days >= _BAK_STALE_AGE_DAYS:
                out.append((p, age_days, stat.st_size))
        except OSError:
            continue
    return out


def doctor(con: apsw.Connection, scan_secrets: bool = False) -> None:
    """Run health checks against the DB. Subset is selected by flags.

    Default: scan for stray `.bak` files older than `_BAK_STALE_AGE_DAYS`
    in the DB directory. With `--scan-secrets`: also counts how many
    existing rows match each redaction pattern (so users discover what
    already leaked into their DB pre-redaction).
    """
    from . import ingest as _ing  # DB_PATH + EMBED_SOCK live in ingest through v0.4.0

    if scan_secrets:
        rows = con.execute("SELECT content FROM messages").fetchall()
        totals: "dict[str, int]" = {}
        affected_rows = 0
        for r in rows:
            counts = _redact.scan_secrets(r["content"])
            if counts:
                affected_rows += 1
                for label, n in counts.items():
                    totals[label] = totals.get(label, 0) + n
        if not totals:
            print("No secret-shaped tokens found.")
        else:
            print(f"Found secret-shaped tokens in {affected_rows:,} row(s):")
            for label, n in sorted(totals.items()):
                print(f"  {label:30s}  {n:,}")
            print("\nRun `recall backfill-redact` to redact existing rows.")

    # DB path drift: warn when CONVO_RECALL_DB overrides the canonical default.
    canonical_db = Path.home() / ".local" / "share" / "convo-recall" / "conversations.db"
    if os.environ.get("CONVO_RECALL_DB") and Path(os.environ["CONVO_RECALL_DB"]).resolve() != canonical_db.resolve():
        print(f"\nCONVO_RECALL_DB override in effect:")
        print(f"  configured  : {_ing.DB_PATH}")
        print(f"  canonical   : {canonical_db}")
        print("Different docs may reference different paths. If unintentional, "
              "unset the env var.")

    # Embed sidecar + coverage status. Three independent signals (extra
    # installed, sidecar reachable, coverage) so the user can act on the
    # right one.
    msg_count = con.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    vec_count = _vec_count(con) if _vec_ok(con) else 0
    coverage_pct = (vec_count * 100 // msg_count) if msg_count else 0
    try:
        import sentence_transformers  # noqa: F401
        extra_installed = True
    except ImportError:
        extra_installed = False
    sock_exists = _ing.EMBED_SOCK.exists()
    print(f"\nEmbed extra      : {'installed' if extra_installed else 'NOT installed'}")
    print(f"Embed sidecar    : {'reachable at ' + str(_ing.EMBED_SOCK) if sock_exists else 'down (no socket)'}")
    print(f"Embedded coverage: {vec_count:,}/{msg_count:,} ({coverage_pct}%)")
    if msg_count > 0 and vec_count == 0:
        if not extra_installed:
            print("  → install with: pipx install 'convo-recall[embeddings]'")
            print("    then re-run:  recall install --with-embeddings")
        elif not sock_exists:
            print("  → start the sidecar: recall serve")
        else:
            print("  → backfill embeddings: recall embed-backfill")
    elif msg_count > 0 and coverage_pct < 95:
        print(f"  → low coverage; run `recall embed-backfill` to heal")

    # Project-id integrity: every messages.project_id must have a row in
    # `projects`. Surfaces drift from the v4 migration or partial ingest.
    distinct_projects = con.execute("SELECT COUNT(*) FROM projects").fetchone()[0]
    print(f"\nProjects         : {distinct_projects} (display_name index)")
    orphan_msgs = con.execute(
        "SELECT COUNT(*) FROM messages m WHERE NOT EXISTS "
        "(SELECT 1 FROM projects p WHERE p.project_id = m.project_id)"
    ).fetchone()[0]
    if orphan_msgs:
        print(f"⚠ {orphan_msgs:,} messages reference a project_id with no "
              f"projects-table row.")
        print("  → re-ingest will recreate the missing rows; otherwise file an issue.")

    # Ingest hook installation status — surfaces missing-hook state for users
    # who upgraded but didn't re-run `recall install`.
    print("\nIngest hook (response-completion driven):")
    try:
        from .install._hooks import _hook_target, _find_hook_script
        ingest_script = _find_hook_script("ingest")
        not_wired_count = 0
        for agent in ("claude", "codex", "gemini"):
            try:
                settings_path, event, label = _hook_target(agent, "ingest")
            except ValueError:
                continue
            wired = False
            if settings_path.exists():
                try:
                    data = json.loads(settings_path.read_text())
                    groups = (data.get("hooks") or {}).get(event) or []
                    for g in groups:
                        for h in g.get("hooks", []):
                            if h.get("command") == str(ingest_script):
                                wired = True
                                break
                        if wired:
                            break
                except (OSError, json.JSONDecodeError):
                    pass
            marker = "✅" if wired else "·"
            state = "wired" if wired else "NOT wired"
            print(f"  {marker} {label:<7} {event:<14} {state}  ({settings_path})")
            if not wired:
                not_wired_count += 1
        if not_wired_count:
            print("  → re-run `recall install-hooks --kind ingest` to wire missing hooks")
    except (RuntimeError, ImportError):
        print("  (could not locate conversation-ingest.sh)")

    stale = _scan_stale_bak_files(_ing.DB_PATH.parent)
    if stale:
        print(f"\nStale `.bak` files in {_ing.DB_PATH.parent} "
              f"(older than {_BAK_STALE_AGE_DAYS} days):")
        for path, age, size in sorted(stale):
            mb = size / (1024 * 1024)
            print(f"  {path.name}  {age:.0f}d old  {mb:,.1f} MB")
        print("\nReview and remove manually if no longer needed.")
    elif not scan_secrets:
        print("\nNo other issues found. "
              "Pass `--scan-secrets` to scan for credential-shaped tokens.")


def forget(con: apsw.Connection, *,
           session: "str | None" = None,
           pattern: "str | None" = None,
           before: "str | None" = None,
           project: "str | None" = None,
           agent: "str | None" = None,
           uuid: "str | None" = None,
           confirm: bool = False) -> int:
    """Delete messages by scope. Mutually-exclusive scope kwargs.

    Always prints a preview (count + first-3 excerpts). Without `confirm=True`
    no rows are deleted. Returns the number of rows deleted (0 in dry-run).
    """
    scopes = {"session": session, "pattern": pattern, "before": before,
              "project": project, "agent": agent, "uuid": uuid}
    set_scopes = [k for k, v in scopes.items() if v is not None]
    if len(set_scopes) != 1:
        raise ValueError(
            "exactly one scope flag is required "
            f"(--session/--pattern/--before/--project/--agent/--uuid); got {set_scopes!r}"
        )
    scope = set_scopes[0]

    where_clauses: "list[str]" = []
    params: list = []
    if session is not None:
        where_clauses.append("session_id = ?"); params.append(session)
    elif uuid is not None:
        where_clauses.append("uuid = ?"); params.append(uuid)
    elif project is not None:
        # Destructive op — exact display_name match only, NO LIKE fallback.
        pids, names = _resolve_project_ids(con, project, exact_only=True)
        if len(pids) == 0:
            raise ValueError(
                f"forget --project requires exact display_name match; "
                f"got 0 matches for {project!r}. "
                f"List candidates with: recall stats"
            )
        if len(pids) > 1:
            raise ValueError(
                f"forget --project requires exact display_name match; "
                f"got {len(pids)} matches for {project!r}: {', '.join(names)}. "
                f"Be more specific."
            )
        placeholders = ",".join("?" * len(pids))
        where_clauses.append(f"project_id IN ({placeholders})")
        params.extend(pids)
    elif agent is not None:
        where_clauses.append("agent = ?"); params.append(agent)
    elif before is not None:
        where_clauses.append("timestamp < ?"); params.append(before)
    elif pattern is not None:
        where_clauses.append("content REGEXP ?"); params.append(pattern)

    where = " AND ".join(where_clauses)

    # Pattern uses Python regex via apsw's createscalarfunction. Register on
    # demand so we don't pay the cost when forget() isn't called.
    if pattern is not None:
        compiled = re.compile(pattern)
        con.createscalarfunction(
            "REGEXP", lambda p, t: 1 if t and compiled.search(t) else 0, 2,
        )

    matches = con.execute(
        f"SELECT m.rowid AS rowid, m.uuid AS uuid, m.session_id AS session_id, "
        f"       m.project_id AS project_id, p.display_name AS display_name, "
        f"       m.agent AS agent, m.role AS role, "
        f"       SUBSTR(m.content, 1, 120) AS excerpt "
        f"FROM messages m LEFT JOIN projects p ON p.project_id = m.project_id "
        f"WHERE m.rowid IN (SELECT rowid FROM messages WHERE {where}) "
        f"ORDER BY m.rowid LIMIT ?",
        (*params, 3),
    ).fetchall()
    total = con.execute(
        f"SELECT COUNT(*) FROM messages WHERE {where}", params
    ).fetchone()[0]

    print(f"forget [{scope}]: {total:,} message(s) match.")
    for r in matches:
        display = r["display_name"] or r["project_id"]
        print(f"  · [{r['agent']}] [{display}] {r['role']}: {r['excerpt']}")
    if total > len(matches):
        print(f"  · … and {total - len(matches):,} more")

    if not confirm:
        print("\nDry-run. Re-run with --confirm to delete.")
        return 0

    if total == 0:
        return 0

    # Capture rowids before delete so we can prune message_vecs.
    rowids = [r[0] for r in con.execute(
        f"SELECT rowid FROM messages WHERE {where}", params
    ).fetchall()]

    con.execute("BEGIN IMMEDIATE")
    try:
        # Messages: deletion triggers messages_ad → FTS row removed.
        con.execute(f"DELETE FROM messages WHERE {where}", params)
        # message_vecs: prune by rowid (no triggers on vec0).
        if _vec_ok(con) and rowids:
            placeholders = ",".join("?" * len(rowids))
            try:
                con.execute(
                    f"DELETE FROM message_vecs WHERE rowid IN ({placeholders})",
                    rowids,
                )
            except Exception as e:
                print(f"[warn] message_vecs prune failed: {type(e).__name__}: {e}",
                      file=sys.stderr)
        # Prune sessions / ingested_files rows that lost all message refs.
        con.execute(
            "DELETE FROM sessions WHERE session_id NOT IN "
            "(SELECT DISTINCT session_id FROM messages)"
        )
        con.execute(
            "DELETE FROM ingested_files WHERE session_id NOT IN "
            "(SELECT DISTINCT session_id FROM messages)"
        )
        con.execute("COMMIT")
    except Exception:
        try: con.execute("ROLLBACK")
        except Exception: pass
        raise

    print(f"\nDeleted {total:,} message(s).")
    return total


def _render_phase_bar(phase: dict) -> None:
    """Render one phase as a single line at the top of `recall stats`.

    State-dependent:
    - `pending`         → "⏳ {name}: pending"
    - `done`, total=0   → "✅ {name}: nothing to do"
    - `done`            → 100% bar (so user sees what just finished)
    - `running`         → live snapshot bar with current/total + rate
    """
    name = phase.get("name", "phase")
    state = phase.get("state", "running")
    total = int(phase.get("total", 0))
    completed = int(phase.get("completed", 0))

    if state == "pending":
        print(f"  ⏳ {name}: pending")
        return
    if state == "done" and total == 0:
        print(f"  ✅ {name}: nothing to do")
        return

    safe_total = total or 1
    pct = min(100, completed * 100 // safe_total)
    try:
        from tqdm import tqdm  # type: ignore
        # file=sys.stdout so the bar lands in the same stream as stats.
        marker = "✅" if state == "done" else "  "
        bar = tqdm(total=safe_total, initial=completed,
                   desc=f"{marker} {name}",
                   unit="file" if name == "ingest" else "msg",
                   leave=True, dynamic_ncols=True, file=sys.stdout,
                   bar_format="{desc}: {percentage:3.0f}%|{bar}| {n_fmt}/{total_fmt} [{rate_fmt}]")
        bar.refresh()
        bar.close()
    except ImportError:
        bar_width = 30
        filled = bar_width * completed // safe_total
        plain = "█" * filled + "░" * (bar_width - filled)
        marker = "✅" if state == "done" else "  "
        print(f"{marker} {name}: {pct:3d}%|{plain}| {completed:,}/{total:,}")


def _render_progress_bar(status: dict) -> None:
    """Render every phase in the snapshot at the top of `recall stats`.

    No live refresh — one render per stats invocation. Phases are shown
    in declared order so the user sees the queued sequence (e.g. ingest
    first, embed-backfill second).
    """
    phases = status.get("phases") or []
    if not phases:
        return
    for phase in phases:
        _render_phase_bar(phase)
    print()  # blank line before stats body


def stats(con: apsw.Connection) -> None:
    from . import _progress
    from . import ingest as _ing  # EMBED_SOCK lives in ingest through v0.4.0

    progress = _progress.read_status()
    if progress is not None:
        _render_progress_bar(progress)

    msg_count = con.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    session_count = con.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    project_count = con.execute(
        "SELECT COUNT(*) FROM projects"
    ).fetchone()[0]
    role_counts = con.execute(
        "SELECT role, COUNT(*) FROM messages GROUP BY role ORDER BY 2 DESC"
    ).fetchall()
    agent_counts = con.execute(
        "SELECT agent, COUNT(*) FROM messages GROUP BY agent ORDER BY 2 DESC"
    ).fetchall()
    vec_count = _vec_count(con)
    fts_row = con.execute(
        "SELECT sql FROM sqlite_master WHERE name='messages_fts'"
    ).fetchone()
    fts_tokenizer = "porter" if fts_row and "porter" in (fts_row[0] or "") else "default"
    print(f"Messages   : {msg_count:,}")
    print(f"Embedded   : {vec_count:,}  ({vec_count * 100 // msg_count if msg_count else 0}%)")
    print(f"Sessions   : {session_count:,}")
    print(f"Projects   : {project_count}")
    print(f"FTS        : {fts_tokenizer} tokenizer")
    print("By role    :")
    for role, count in role_counts:
        print(f"  {role:14s}: {count:,}")
    print("By agent   :")
    for agent, count in agent_counts:
        print(f"  {agent:14s}: {count:,}")

    # Hybrid-search readiness warning. Surface the most likely cause + the
    # exact command to fix it, so users don't silently run in FTS-only mode
    # without knowing the headline feature is off.
    if msg_count > 0 and vec_count == 0:
        print()
        try:
            import sentence_transformers  # noqa: F401
            extra_installed = True
        except ImportError:
            extra_installed = False
        if not extra_installed:
            print("⚠ Vector search disabled — `[embeddings]` extra not installed.")
            print("  pipx install 'convo-recall[embeddings]' && recall install --with-embeddings")
        elif not _ing.EMBED_SOCK.exists():
            print("⚠ Vector search disabled — embed sidecar not running.")
            print("  recall serve --sock " + str(_ing.EMBED_SOCK) + "  (or restart `recall install`)")
        elif progress is not None:
            # A backfill chain is currently running — re-word the message so
            # the user doesn't manually re-trigger something already in flight.
            # First-run on a 60K-msg DB takes 5-15 min; small DBs finish in
            # seconds. The progress bar at the top shows live status.
            print("ℹ First-run embedding in progress — see the progress bar above.")
            print("  Re-run `recall stats` to track. Vector search becomes "
                  "available as embeddings complete.")
        else:
            # No active chain — the user can manually start one OR wait for
            # the next watcher-driven ingest tick (which auto-heals up to
            # 2000 missing rows per call).
            print("ℹ Vector search ready but rows aren't embedded yet.")
            print("  • First-run? Embedding takes time proportional to DB size")
            print("    (~50ms per message; 60K msgs ≈ 5-15 min).")
            print("  • Track progress: re-run `recall stats` until the bar")
            print("    completes, then it disappears.")
            print("  • To kick it off now: `recall embed-backfill`")
            print("    (otherwise next watcher-driven ingest auto-heals 2000")
            print("    rows/tick — fully automatic but slower).")
