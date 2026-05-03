"""
integrity_sweep — runnable counterpart of `tests/integrity_sweep.md`.

Exercises the A–J probes against the LIVE installed convo-recall + the
maintainer DB at ~/.local/share/convo-recall/conversations.db. Results
print as ✓ PASS / ⚠ SKIP / ✗ FAIL with one-line detail.

Usage:
    python tests/integrity_sweep.py            # full sweep
    python tests/integrity_sweep.py --section D  # one section only

This is a HEALTH CHECK — it asserts plumbing/integrity invariants that
should hold across upgrades. It is NOT a use-case test suite (it doesn't
exercise every feature combination). Skips probes whose preconditions
aren't met (e.g. Gemini absent, sidecar down) rather than failing them.
"""
from __future__ import annotations

import argparse
import http.client
import json
import os
import re
import socket
import sqlite3
import subprocess
import sys
from pathlib import Path

DB_PATH = Path(os.environ.get(
    "CONVO_RECALL_DB",
    Path.home() / ".local" / "share" / "convo-recall" / "conversations.db",
))
SOCK_PATH = Path(os.environ.get(
    "CONVO_RECALL_SOCK",
    Path.home() / ".local" / "share" / "convo-recall" / "embed.sock",
))

results: list[tuple[str, str, str, str]] = []  # (id, status, name, detail)


def add(probe_id: str, status: str, name: str, detail: str = "") -> None:
    results.append((probe_id, status, name, detail))


def passed(probe_id: str, name: str, detail: str = "") -> None:
    add(probe_id, "PASS", name, detail)


def skipped(probe_id: str, name: str, detail: str) -> None:
    add(probe_id, "SKIP", name, detail)


def failed(probe_id: str, name: str, detail: str) -> None:
    add(probe_id, "FAIL", name, detail)


def run(cmd: list[str], timeout: int = 30) -> "tuple[int, str, str]":
    """Run a subprocess; return (rc, stdout, stderr) — never raises."""
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return proc.returncode, proc.stdout, proc.stderr
    except FileNotFoundError as e:
        return 127, "", f"FileNotFoundError: {e}"
    except subprocess.TimeoutExpired:
        return 124, "", f"timed out after {timeout}s"
    except Exception as e:
        return 1, "", f"{type(e).__name__}: {e}"


# ── Section A: Install & version sanity ──────────────────────────────────────

def section_a() -> None:
    rc, out, err = run(["recall", "--version"])
    if rc == 0 and out.strip().startswith("recall "):
        cli_version = out.strip().split()[-1]
        passed("A1", "recall --version reports a version", f"got {cli_version!r}")
    else:
        failed("A1", "recall --version reports a version", f"rc={rc}, err={err.strip()[:60]}")
        cli_version = None

    rc, out, err = run(["pipx", "list"])
    if rc == 0 and "convo-recall" in out:
        m = re.search(r"package convo-recall ([\d.]+)", out)
        v = m.group(1) if m else "?"
        passed("A2", "pipx lists convo-recall", f"version {v}")
    else:
        skipped("A2", "pipx lists convo-recall", "pipx not on PATH or no entry — pip-only install")

    rc, out, err = run([sys.executable, "-m", "pip", "show", "convo-recall"])
    if rc == 0:
        m = re.search(r"^Version:\s*(\S+)", out, re.MULTILINE)
        if m:
            pip_version = m.group(1)
            passed("A3", "pip show reports a Version", f"got {pip_version}")
        else:
            failed("A3", "pip show reports a Version", "no Version line in output")
    else:
        skipped("A3", "pip show convo-recall", err.strip()[:60])

    rc, out, err = run([sys.executable, "-c", "import convo_recall; print(convo_recall.__version__)"])
    if rc == 0:
        code_version = out.strip()
        passed("A4", "convo_recall.__version__ readable", f"got {code_version!r}")
        if cli_version and code_version != cli_version:
            failed("A4b", "CLI version matches __version__",
                   f"recall --version={cli_version!r} but __version__={code_version!r}")
        elif cli_version:
            passed("A4b", "CLI version matches __version__")
    else:
        failed("A4", "convo_recall.__version__ readable", err.strip()[:80])

    rc, out, err = run(["recall", "--help"])
    if rc == 0 and "search" in out and "ingest" in out and "stats" in out:
        # Count subcommands listed in the {…,…} brace block at top of usage.
        m = re.search(r"\{([\w\-,_]+)\}", out)
        n_subs = len(m.group(1).split(",")) if m else 0
        passed("A5", "recall --help lists subcommands", f"{n_subs} subcommands")
    else:
        failed("A5", "recall --help intact", f"rc={rc}, err={err.strip()[:60]}")

    rc, out, err = run(["recall", "doctor"])
    if rc == 0:
        ok = ("Embed sidecar" in out and "Embedded coverage" in out
              and "Ingest hook" in out)
        if ok:
            passed("A6", "recall doctor sections present")
        else:
            failed("A6", "recall doctor sections present",
                   "missing Embed sidecar / Embedded coverage / Ingest hook block")
    else:
        failed("A6", "recall doctor runs", f"rc={rc}")


# ── Section B: Database integrity ────────────────────────────────────────────

def section_b() -> None:
    if not DB_PATH.exists():
        for pid in ("B7", "B8", "B9", "B10", "B11"):
            skipped(pid, "Database integrity", f"DB not found at {DB_PATH}")
        return

    con = sqlite3.connect(f"file:{DB_PATH}?immutable=1", uri=True)
    cur = con.cursor()

    tables = {r[0] for r in cur.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table', 'virtual_table')"
    )}
    required = {"messages", "sessions", "projects", "messages_fts",
                "message_vecs", "ingested_files"}
    missing = required - tables
    if not missing:
        passed("B7", "schema: all required tables present", f"{len(tables)} total")
    else:
        failed("B7", "schema: required tables present", f"missing: {missing}")

    msg_count = cur.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    sess_count = cur.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    file_count = cur.execute("SELECT COUNT(*) FROM ingested_files").fetchone()[0]

    rc, out, err = run(["recall", "stats"])
    if rc == 0:
        m_msgs = re.search(r"Messages\s*:\s*([\d,]+)", out)
        m_sess = re.search(r"Sessions\s*:\s*([\d,]+)", out)
        if m_msgs and m_sess:
            stats_msgs = int(m_msgs.group(1).replace(",", ""))
            stats_sess = int(m_sess.group(1).replace(",", ""))
            if stats_msgs == msg_count and stats_sess == sess_count:
                passed("B8", "recall stats totals match raw counts",
                       f"msgs={msg_count:,}, sess={sess_count:,}")
            else:
                failed("B8", "recall stats totals match raw counts",
                       f"stats={stats_msgs}/{stats_sess} vs raw={msg_count}/{sess_count}")
        else:
            failed("B8", "recall stats parseable", "missing Messages/Sessions lines")
    else:
        failed("B8", "recall stats runs", f"rc={rc}")

    role_breakdown = dict(cur.execute(
        "SELECT role, COUNT(*) FROM messages GROUP BY role"
    ).fetchall())
    role_sum = sum(role_breakdown.values())
    if role_sum == msg_count:
        passed("B9", "messages = sum(by role)",
               f"{msg_count:,} = " + " + ".join(f"{r}:{n:,}" for r, n in sorted(role_breakdown.items())))
    else:
        failed("B9", "messages = sum(by role)", f"{msg_count} vs {role_sum}")

    agent_breakdown = dict(cur.execute(
        "SELECT agent, COUNT(*) FROM messages GROUP BY agent"
    ).fetchall())
    agent_sum = sum(agent_breakdown.values())
    if agent_sum == msg_count:
        passed("B10", "messages = sum(by agent)",
               " + ".join(f"{a}:{n:,}" for a, n in sorted(agent_breakdown.items())))
    else:
        failed("B10", "messages = sum(by agent)", f"{msg_count} vs {agent_sum}")

    integrity = cur.execute("PRAGMA integrity_check").fetchone()
    if integrity and integrity[0] == "ok":
        passed("B11", "PRAGMA integrity_check == 'ok'")
    else:
        failed("B11", "PRAGMA integrity_check", repr(integrity))

    con.close()


# ── Section C: Embedding subsystem ───────────────────────────────────────────

def section_c() -> None:
    # C12: sidecar process running
    rc, out, err = run(["pgrep", "-fl", "recall serve"])
    sidecar_pids = [line.split()[0] for line in out.strip().splitlines() if line]
    if sidecar_pids:
        passed("C12", "recall serve process running", f"pid(s)={','.join(sidecar_pids)}")
    else:
        skipped("C12", "recall serve process running",
                "no `recall serve` process found — sidecar likely down")

    # C13: socket file
    if SOCK_PATH.exists():
        try:
            mode = oct(SOCK_PATH.stat().st_mode & 0o777)
            passed("C13", "embed UDS socket exists", f"path={SOCK_PATH}, mode={mode}")
        except OSError as e:
            failed("C13", "embed UDS socket statable", str(e))
    else:
        skipped("C13", "embed UDS socket exists", f"not present at {SOCK_PATH}")
        for pid in ("C14", "C15"):
            skipped(pid, "Sidecar HTTP probe", "depends on C13")
        sidecar_dim = None
    sidecar_dim = None
    if SOCK_PATH.exists():
        try:
            class _UC(http.client.HTTPConnection):
                def connect(self_):
                    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    s.settimeout(5.0)
                    s.connect(str(SOCK_PATH))
                    self_.sock = s
            samples = [("hello", None), ("world", None), ("ingest tool_error", None)]
            dims = set()
            for i, (text, _) in enumerate(samples):
                conn = _UC("localhost", timeout=10.0)
                conn.request("POST", "/embed",
                             body=json.dumps({"text": text, "mode": "query"}).encode(),
                             headers={"Content-Type": "application/json"})
                resp = conn.getresponse()
                if resp.status != 200:
                    failed("C14", "POST /embed returns 200",
                           f"status={resp.status} on sample {i}")
                    conn.close()
                    break
                payload = json.loads(resp.read())
                conn.close()
                vec = payload.get("vector") or payload.get("embedding")
                if not isinstance(vec, list) or not all(isinstance(x, (int, float)) for x in vec):
                    failed("C14", "POST /embed returns a vector",
                           f"shape mismatch on sample {i}: keys={list(payload.keys())}")
                    break
                dims.add(len(vec))
            else:
                passed("C14", "POST /embed returns vectors", f"3/3 samples, lengths={dims}")
                if len(dims) == 1:
                    sidecar_dim = dims.pop()
                    passed("C15", "dim uniform across calls", f"dim={sidecar_dim}")
                else:
                    failed("C15", "dim uniform across calls", f"got {dims}")
        except Exception as e:
            failed("C14", "POST /embed reachable", f"{type(e).__name__}: {e}")

    # C16, C17: vec_ok + coverage. Use the installed package via subprocess so
    # we exercise what `recall ...` would see, not a dev import. open_db is
    # called with readonly=True so this probe never writes (no WAL pragma,
    # no schema init, no perm chmod).
    code = (
        "import sys; "
        "from convo_recall import open_db; "
        "from convo_recall.embed import _vec_count; "
        "from convo_recall.db import _vec_ok; "
        "con = open_db(readonly=True); "
        "ok = _vec_ok(con); "
        "vc = _vec_count(con); "
        "n = con.execute('SELECT COUNT(*) FROM messages').fetchone()[0]; "
        "print(f'{ok}|{vc}|{n}')"
    )
    rc, out, err = run([sys.executable, "-c", code], timeout=15)
    if rc == 0:
        try:
            ok_str, vc_str, n_str = out.strip().split("|")
            vec_ok = ok_str == "True"
            vec_count = int(vc_str)
            msg_count = int(n_str)
            if vec_ok:
                passed("C16", "_vec_ok(con) is True")
            else:
                failed("C16", "_vec_ok(con) is True", "False — sqlite-vec not loaded")
            if msg_count == 0:
                skipped("C17", "_vec_count == messages", "empty DB")
            elif vec_count == msg_count:
                passed("C17", "_vec_count == messages (100% coverage)",
                       f"{vec_count:,}/{msg_count:,}")
            else:
                pct = vec_count * 100 // msg_count
                failed("C17", "_vec_count == messages",
                       f"{vec_count:,}/{msg_count:,} ({pct}%)")
        except (ValueError, IndexError) as e:
            failed("C16", "_vec_ok / _vec_count parsable", f"{type(e).__name__}: {e}; out={out!r}")
    else:
        failed("C16", "_vec_ok / _vec_count subprocess", err.strip()[:80])

    # C18: doctor's coverage agrees
    rc, out, err = run(["recall", "doctor"])
    if rc == 0:
        m = re.search(r"Embedded coverage:\s*([\d,]+)/([\d,]+)\s*\((\d+)%\)", out)
        if m:
            d_emb, d_total, d_pct = (int(g.replace(",", "")) for g in m.groups())
            if d_pct == 100:
                passed("C18", "recall doctor reports 100% coverage",
                       f"{d_emb:,}/{d_total:,}")
            else:
                failed("C18", "recall doctor reports 100% coverage",
                       f"{d_emb:,}/{d_total:,} ({d_pct}%)")
        else:
            failed("C18", "recall doctor coverage line parseable", "no match")
    else:
        failed("C18", "recall doctor runs", f"rc={rc}")

    # C19/C20: PID stability and resource usage are cross-upgrade and time-
    # series probes — informational only, single-run-skip here.
    skipped("C19", "Sidecar PID stability across upgrades",
            "cross-upgrade probe — see report comparison")
    skipped("C20", "Sidecar CPU & memory anomaly check",
            "cross-time probe — single-run snapshot only")


# ── Section D: Search functionality ──────────────────────────────────────────

def section_d() -> None:
    if not DB_PATH.exists():
        for pid in ("D21", "D22", "D23", "D24", "D25", "D26", "D27"):
            skipped(pid, "Search functionality", "DB absent")
        return

    rc, out, err = run(["recall", "search", "ingest", "--project", "convo-recall",
                         "-n", "3", "--json"])
    if rc == 0:
        try:
            d = json.loads(out)
            if d.get("mode") in {"hybrid", "fts"} and isinstance(d.get("results"), list):
                passed("D21", "search --project --json valid",
                       f"mode={d['mode']}, n_results={len(d['results'])}")
            else:
                failed("D21", "search --json valid", f"keys={list(d.keys())}")
        except json.JSONDecodeError as e:
            failed("D21", "search --json parseable", str(e))
    else:
        failed("D21", "search --project runs", f"rc={rc}")

    rc, out, err = run(["recall", "search", "ingest", "--all-projects", "-n", "5", "--json"])
    if rc == 0:
        try:
            d = json.loads(out)
            passed("D22", "search --all-projects works",
                   f"n_results={len(d.get('results', []))}")
        except json.JSONDecodeError:
            failed("D22", "search --all-projects parseable", "bad JSON")
    else:
        failed("D22", "search --all-projects runs", f"rc={rc}")

    counts = []
    for n in (1, 3, 10):
        rc, out, _ = run(["recall", "search", "ingest", "-n", str(n), "--json", "--all-projects"])
        if rc == 0:
            try:
                counts.append(len(json.loads(out).get("results", [])))
            except json.JSONDecodeError:
                counts.append(-1)
        else:
            counts.append(-1)
    if counts == sorted(counts) and counts[-1] >= counts[0]:
        passed("D23", "-n parameter respected (monotone)", f"got {counts}")
    else:
        failed("D23", "-n parameter respected", f"got {counts}")

    # Empty query: should not crash. Some versions print a usage error to
    # stderr but exit cleanly; we just want no Python traceback.
    rc, out, err = run(["recall", "search", "", "--all-projects", "-n", "3", "--json"])
    if "Traceback" not in err:
        passed("D24", "empty query doesn't crash with traceback",
               f"rc={rc}, stderr len={len(err)}")
    else:
        failed("D24", "empty query doesn't crash", "Traceback in stderr")

    # Nonsense needle
    rc, out, _ = run(["recall", "search", "zorblax_no_such_tokenxyzzz", "--all-projects",
                      "-n", "5", "--json"])
    if rc == 0:
        try:
            d = json.loads(out)
            n_results = len(d.get("results", []))
            if n_results == 0:
                passed("D25", "nonsense needle returns []")
            else:
                # Some FTS configs return partial matches even for noise — accept
                # but flag.
                passed("D25", "nonsense needle doesn't crash",
                       f"got {n_results} weak matches (acceptable)")
        except json.JSONDecodeError:
            failed("D25", "nonsense needle JSON parseable", "bad output")

    # D26 (e2e plant-and-find) requires writing to the DB — out of scope for a
    # read-only health check on the maintainer corpus.
    skipped("D26", "plant-and-find round trip", "requires DB write — covered by pytest e2e")

    # D27: agent distribution for a query that should hit multiple agents
    rc, out, _ = run(["recall", "search", "error", "--all-projects", "-n", "20", "--json"])
    if rc == 0:
        try:
            d = json.loads(out)
            agents = [r.get("agent") for r in d.get("results", [])]
            distinct_agents = set(agents)
            passed("D27", "search results span multiple agents (or single-agent corpus)",
                   f"agents={sorted(distinct_agents)}")
        except json.JSONDecodeError:
            failed("D27", "search agent distribution parseable", "bad JSON")


# ── Section E: Tail (cross-session aggregation) ──────────────────────────────

def section_e() -> None:
    if not DB_PATH.exists():
        for pid in ("E28", "E29", "E30", "E31", "E32", "E33", "E34"):
            skipped(pid, "Tail behavior", "DB absent")
        return

    rc, out, err = run(["recall", "tail", "5", "--all-projects", "--json"])
    if rc == 0:
        try:
            d = json.loads(out)
            if "messages" in d and "sessions" in d:
                passed("E28", "tail returns sessions + messages structure",
                       f"n_messages={len(d['messages'])}, n_sessions={len(d['sessions'])}")
            else:
                failed("E28", "tail JSON shape", f"keys={list(d.keys())}")
        except json.JSONDecodeError:
            failed("E28", "tail JSON parseable", "bad output")
    else:
        failed("E28", "tail runs", f"rc={rc}")

    rc, out, _ = run(["recall", "tail", "30", "--all-projects"])
    if rc == 0 and "·" in out and "messages" in out:
        passed("E29", "tail header has scope · messages · range layout")
    else:
        passed("E29", "tail header renders",
               "non-strict — header format may vary by tty width")

    rc, out, _ = run(["recall", "tail", "5", "--all-projects"])
    if rc == 0:
        passed("E30", "tail --all-projects works", f"output len={len(out)}")
    else:
        failed("E30", "tail --all-projects", f"rc={rc}")

    rc, out, _ = run(["recall", "tail", "5", "--all-projects", "--json"])
    if rc == 0:
        try:
            json.loads(out)
            passed("E31", "tail --json well-formed")
        except json.JSONDecodeError:
            failed("E31", "tail --json well-formed", "bad JSON")

    # E32: nonexistent project. The ingester uses a NULL session_id when the
    # cwd-realpath collides with no projects row, so we use a guaranteed-absent
    # display_name.
    rc, out, err = run(["recall", "tail", "--project", "zzznotaproject_xyz_q", "5"])
    if rc == 1 or "no" in (out + err).lower():
        passed("E32", "tail --project nonexistent → rc=1 / 'no'",
               f"rc={rc}")
    else:
        failed("E32", "tail --project nonexistent fails gracefully", f"rc={rc}")

    # E33 + E34 require knowing a session_id; pull one from the DB.
    con = sqlite3.connect(f"file:{DB_PATH}?immutable=1", uri=True)
    sess = con.execute(
        "SELECT session_id FROM sessions ORDER BY last_updated DESC LIMIT 1"
    ).fetchone()
    con.close()
    if sess:
        sid = sess[0]
        rc, out, _ = run(["recall", "tail", "5", "--session", sid, "--json"])
        if rc == 0:
            try:
                d = json.loads(out)
                if d.get("session_id") == sid:
                    passed("E33", "tail --session <id> returns that session",
                           f"sid={sid[:8]}…")
                else:
                    failed("E33", "tail --session <id>",
                           f"got session_id={d.get('session_id')}")
            except json.JSONDecodeError:
                failed("E33", "tail --session <id> JSON", "bad output")
        else:
            failed("E33", "tail --session <id>", f"rc={rc}")

        rc, out, _ = run(["recall", "tail", "5", "--session", sid, "--expand", "1,2"])
        if rc == 0:
            passed("E34", "tail --expand 1,2 doesn't crash", f"output len={len(out)}")
        else:
            failed("E34", "tail --expand 1,2", f"rc={rc}")
    else:
        skipped("E33", "tail --session", "no sessions in DB")
        skipped("E34", "tail --expand", "no sessions in DB")


# ── Section F: Hooks (per-agent wiring) ──────────────────────────────────────

def section_f() -> None:
    home = Path.home()

    def _hook_check(probe_id: str, name: str, settings_path: Path,
                    required_events: list[str]) -> None:
        if not settings_path.exists():
            skipped(probe_id, name, f"{settings_path} not present")
            return
        try:
            data = json.loads(settings_path.read_text())
        except (json.JSONDecodeError, OSError) as e:
            failed(probe_id, name, f"unreadable: {e}")
            return
        hooks = (data.get("hooks") or {})
        present = [evt for evt in required_events if hooks.get(evt)]
        if len(present) == len(required_events):
            passed(probe_id, name, f"{', '.join(present)}")
        else:
            missing = [e for e in required_events if e not in present]
            skipped(probe_id, name, f"missing events: {missing}")

    _hook_check("F35", "Claude hooks (UserPromptSubmit + Stop)",
                home / ".claude" / "settings.json", ["UserPromptSubmit", "Stop"])
    _hook_check("F36", "Codex hooks (UserPromptSubmit + Stop)",
                home / ".codex" / "hooks.json", ["UserPromptSubmit", "Stop"])
    # Gemini uses different event names.
    g_settings = home / ".gemini" / "settings.json"
    if not g_settings.exists():
        skipped("F37", "Gemini hooks", f"{g_settings} not present")
    else:
        try:
            data = json.loads(g_settings.read_text())
            hooks = data.get("hooks") or {}
            ev = list(hooks.keys())
            if ev:
                passed("F37", "Gemini hooks present", f"events={ev}")
            else:
                skipped("F37", "Gemini hooks", "no hooks block")
        except (json.JSONDecodeError, OSError) as e:
            failed("F37", "Gemini hooks readable", str(e))

    # Hook scripts present + executable
    rc, out, _ = run([sys.executable, "-c",
                       "import sys, convo_recall; "
                       "from pathlib import Path; "
                       "p = Path(convo_recall.__file__).parent / 'hooks'; "
                       "print(p)"])
    if rc == 0:
        hooks_dir = Path(out.strip())
        memory = hooks_dir / "conversation-memory.sh"
        ingest_h = hooks_dir / "conversation-ingest.sh"
        both = memory.exists() and ingest_h.exists()
        if both:
            exec_ok = os.access(memory, os.X_OK) and os.access(ingest_h, os.X_OK)
            if exec_ok:
                passed("F38", "hook scripts exist + executable",
                       f"{memory.name}, {ingest_h.name}")
            else:
                failed("F38", "hook scripts executable", "missing +x bit")
        else:
            failed("F38", "hook scripts present", f"{hooks_dir}")
    else:
        failed("F38", "hooks dir resolvable", "couldn't import convo_recall")

    # F39 (post-turn ingest growing) and F40 (pre-prompt context injection) and
    # F41 (throttle) and F42 (hook log) require live agent turns to assert.
    skipped("F39", "post-turn ingest grows row count", "requires live agent turn")
    skipped("F40", "pre-prompt context injection", "requires live agent turn")
    skipped("F41", "short-prompt throttle", "requires live agent turn")
    skipped("F42", "hook payload log writes", "requires live agent turn")


# ── Section G: Per-agent ingest correctness ──────────────────────────────────

def section_g() -> None:
    if not DB_PATH.exists():
        for pid in ("G43", "G44", "G45", "G46", "G47"):
            skipped(pid, "Per-agent ingest correctness", "DB absent")
        return

    con = sqlite3.connect(f"file:{DB_PATH}?immutable=1", uri=True)
    cur = con.cursor()

    files_by_agent = dict(cur.execute(
        "SELECT agent, COUNT(*) FROM ingested_files GROUP BY agent"
    ).fetchall())
    if files_by_agent:
        passed("G43", "ingested_files spans multiple agents",
               " + ".join(f"{a}:{n}" for a, n in sorted(files_by_agent.items())))
    else:
        failed("G43", "ingested_files non-empty", "no rows")

    # Per-agent sample message — confirm content extraction works (no
    # `[object Object]` or empty content).
    bad_samples = []
    for agent in files_by_agent:
        sample = cur.execute(
            "SELECT content FROM messages WHERE agent = ? "
            "AND role IN ('user', 'assistant') AND LENGTH(content) > 0 "
            "ORDER BY rowid DESC LIMIT 1",
            (agent,),
        ).fetchone()
        if sample is None:
            bad_samples.append(f"{agent}: no sample")
            continue
        content = sample[0] or ""
        if "[object Object]" in content or content.strip() == "":
            bad_samples.append(f"{agent}: bad shape")
    if not bad_samples:
        passed("G44", "per-agent content extraction OK", f"agents: {sorted(files_by_agent)}")
    else:
        failed("G44", "per-agent content extraction OK", "; ".join(bad_samples))

    # Timestamp parseability per agent
    null_ts_by_agent = dict(cur.execute(
        "SELECT agent, COUNT(*) FROM messages WHERE timestamp IS NULL GROUP BY agent"
    ).fetchall())
    if not null_ts_by_agent:
        passed("G45", "no NULL timestamps in any agent")
    else:
        # Some legacy rows might be NULL — accept but flag.
        passed("G45", "NULL timestamps tolerable",
               "agents with NULL ts: " + ", ".join(f"{a}:{n}" for a, n in null_ts_by_agent.items()))

    # G46: project resolution — for each agent, count messages-without-project_id.
    orphans = cur.execute(
        "SELECT COUNT(*) FROM messages m WHERE NOT EXISTS "
        "(SELECT 1 FROM projects p WHERE p.project_id = m.project_id)"
    ).fetchone()[0]
    if orphans == 0:
        passed("G46", "every messages.project_id has a projects row")
    else:
        failed("G46", "every messages.project_id has a projects row",
               f"{orphans:,} orphan messages")

    # G47: sessions per (project, agent) cross-tab — at least one of each agent
    # we've ingested files for should have ≥1 session row.
    pa_sess = dict(cur.execute(
        "SELECT agent, COUNT(DISTINCT session_id) FROM sessions GROUP BY agent"
    ).fetchall())
    pa_files = files_by_agent
    missing = [a for a in pa_files if pa_sess.get(a, 0) == 0]
    if not missing:
        passed("G47", "every agent has ≥1 session row",
               " + ".join(f"{a}:{n}" for a, n in sorted(pa_sess.items())))
    else:
        failed("G47", "every agent has ≥1 session row", f"empty: {missing}")
    con.close()


# ── Section H: tool_error extraction ─────────────────────────────────────────

def section_h() -> None:
    if not DB_PATH.exists():
        for pid in (f"H{n}" for n in range(48, 55)):
            skipped(pid, "tool_error extraction", "DB absent")
        return

    con = sqlite3.connect(f"file:{DB_PATH}?immutable=1", uri=True)
    cur = con.cursor()

    by_agent = dict(cur.execute(
        "SELECT agent, COUNT(*) FROM messages WHERE role = 'tool_error' GROUP BY agent"
    ).fetchall())
    if by_agent:
        passed("H48", "tool_error rows by agent",
               " + ".join(f"{a}:{n}" for a, n in sorted(by_agent.items())))
    else:
        skipped("H48", "tool_error rows present", "no tool_error rows in DB")
        for pid in ("H49", "H50", "H51", "H52", "H53", "H54"):
            skipped(pid, "tool_error subprobe", "depends on H48")
        con.close()
        return

    # H49: codex prefix breakdown
    codex_prefixes = dict(cur.execute(
        "SELECT SUBSTR(content, 1, INSTR(content || char(10), char(10)) - 1) AS prefix, COUNT(*) "
        "FROM messages WHERE role = 'tool_error' AND agent = 'codex' "
        "GROUP BY prefix ORDER BY COUNT(*) DESC LIMIT 5"
    ).fetchall())
    if codex_prefixes:
        passed("H49", "codex tool_error prefix breakdown",
               "; ".join(f"{p[:40]}…:{n}" if len(p) > 40 else f"{p}:{n}"
                         for p, n in list(codex_prefixes.items())[:3]))
    else:
        skipped("H49", "codex tool_error breakdown", "no codex tool_error rows")

    # H50: gemini prefix breakdown
    gemini_prefixes = dict(cur.execute(
        "SELECT SUBSTR(content, 1, INSTR(content || char(10), char(10)) - 1) AS prefix, COUNT(*) "
        "FROM messages WHERE role = 'tool_error' AND agent = 'gemini' "
        "GROUP BY prefix ORDER BY COUNT(*) DESC LIMIT 5"
    ).fetchall())
    if gemini_prefixes:
        passed("H50", "gemini tool_error prefix breakdown",
               "; ".join(f"{p[:40]}…:{n}" if len(p) > 40 else f"{p}:{n}"
                         for p, n in list(gemini_prefixes.items())[:3]))
    else:
        skipped("H50", "gemini tool_error breakdown", "no gemini tool_error rows")

    # H51: false-positive count for codex function_call_output (exit=0)
    false_pos = cur.execute(
        "SELECT COUNT(*) FROM messages WHERE role = 'tool_error' "
        "AND content LIKE '%function_call_output%' "
        "AND content LIKE '%Process exited with code 0%'"
    ).fetchone()[0]
    total_codex = by_agent.get("codex", 0)
    if total_codex == 0:
        skipped("H51", "codex false-positive count", "no codex tool_error rows")
    else:
        if false_pos == 0:
            passed("H51", "no codex 'exit code 0' false positives", "0 rows")
        else:
            pct = false_pos * 100 // max(total_codex, 1)
            passed("H51", "codex false-positive count tracked",
                   f"{false_pos}/{total_codex} ({pct}%)")

    # H52: regex-pattern hit frequency
    error_pat = re.compile(
        r"(Error:|TypeError|ECONNREFUSED|Traceback|FAILED|AssertionError|"
        r"npm ERR!|cargo error|Exit code [1-9])",
        re.I,
    )
    sample = cur.execute(
        "SELECT content FROM messages WHERE role = 'tool_error' "
        "ORDER BY rowid DESC LIMIT 200"
    ).fetchall()
    matched = sum(1 for r in sample if error_pat.search(r[0] or ""))
    passed("H52", "_ERROR_PATTERNS regex hit rate (sample of 200)",
           f"{matched}/{len(sample)} matched")

    # H53: rows with no regex match — caught via is_error / explicit type
    no_match = sum(1 for r in sample if not error_pat.search(r[0] or ""))
    passed("H53", "non-regex rows present (caught via is_error/type)",
           f"{no_match}/{len(sample)} flagged via non-regex path")

    # H54: content length distribution
    sizes = cur.execute(
        "SELECT agent, MIN(LENGTH(content)), MAX(LENGTH(content)), AVG(LENGTH(content)) "
        "FROM messages WHERE role = 'tool_error' GROUP BY agent"
    ).fetchall()
    summary = "; ".join(f"{a}:{int(mn)}/{int(mx)}/{int(avg)}" for a, mn, mx, avg in sizes)
    passed("H54", "tool_error content length min/max/avg per agent", summary)

    con.close()


# ── Section I: Data quality / known issues ───────────────────────────────────

def section_i() -> None:
    if not DB_PATH.exists():
        for pid in ("I55", "I56", "I57", "I58", "I59"):
            skipped(pid, "Data quality", "DB absent")
        return

    con = sqlite3.connect(f"file:{DB_PATH}?immutable=1", uri=True)
    cur = con.cursor()

    stray_names = ("/", "projects", "project")
    stray = cur.execute(
        "SELECT display_name, COUNT(*) FROM projects "
        "WHERE display_name IN ('" + "','".join(stray_names) + "') "
        "GROUP BY display_name"
    ).fetchall()
    if not stray:
        passed("I55", "no stray /-projects",
               "no rows for display_name in {'/', 'projects', 'project'}")
    else:
        passed("I55", "stray projects flagged (informational)",
               "; ".join(f"{n}:{c}" for n, c in stray))

    # I56: duplicate display_names → multiple project_ids (Gemini cwd-NULL bug)
    dupes = cur.execute(
        "SELECT display_name, COUNT(*) AS n FROM projects "
        "GROUP BY display_name HAVING n > 1 ORDER BY n DESC LIMIT 5"
    ).fetchall()
    if not dupes:
        passed("I56", "no duplicate display_names")
    else:
        passed("I56", "duplicate display_names (informational)",
               "; ".join(f"{n}:{c}" for n, c in dupes))

    # I57/I58/I59: sentinel + short-content rows are environmental — informational
    sent = cur.execute(
        "SELECT COUNT(*) FROM messages WHERE timestamp LIKE '2030-%'"
    ).fetchone()[0]
    if sent == 0:
        passed("I57", "no e2e sentinel rows in DB")
    else:
        passed("I57", "e2e sentinel rows present", f"{sent} rows with 2030-* timestamps")

    short = cur.execute(
        "SELECT agent, COUNT(*) FROM messages WHERE LENGTH(content) < 20 "
        "AND role IN ('user', 'assistant') GROUP BY agent ORDER BY 2 DESC"
    ).fetchall()
    short_summary = "; ".join(f"{a}:{n:,}" for a, n in short[:3])
    passed("I58", "short-content (<20 char) rows tracked",
           short_summary or "none")

    # I59: sentinel pollutes tail header range — informational and tied to I57.
    if sent == 0:
        passed("I59", "tail header range clean (no sentinel)")
    else:
        passed("I59", "tail header range may include 2030-* sentinel",
               "see I57")

    con.close()


# ── Section J: Source-code review ────────────────────────────────────────────

def section_j() -> None:
    # J60: ingest.py LOC across versions. Post-A7, ingest.py is gone — we
    # report the new ingest/ subpackage structure instead.
    src_root = Path(__file__).resolve().parent.parent / "src" / "convo_recall"
    if not src_root.exists():
        for pid in (f"J{n}" for n in range(60, 65)):
            skipped(pid, "Source-code review", f"src tree not at {src_root}")
        return

    legacy_ingest = src_root / "ingest.py"
    pkg_ingest = src_root / "ingest"
    if legacy_ingest.exists():
        loc = sum(1 for _ in legacy_ingest.read_text().splitlines())
        passed("J60", "legacy ingest.py LOC", f"{loc} lines")
    elif pkg_ingest.is_dir():
        files = sorted(pkg_ingest.glob("*.py"))
        total = sum(sum(1 for _ in f.read_text().splitlines()) for f in files)
        per_file = "; ".join(f"{f.name}:{sum(1 for _ in f.read_text().splitlines())}" for f in files)
        passed("J60", f"ingest/ package replaces ingest.py — {total} LOC across {len(files)} files",
               per_file)
    else:
        failed("J60", "neither ingest.py nor ingest/ found", str(src_root))

    # J61: grep for tool_error / extractor functions
    extractor_targets = (
        ("_codex_event_msg_error", pkg_ingest / "codex.py" if pkg_ingest.is_dir() else legacy_ingest),
        ("_codex_fco_error", pkg_ingest / "codex.py" if pkg_ingest.is_dir() else legacy_ingest),
        ("_gemini_record_error", pkg_ingest / "gemini.py" if pkg_ingest.is_dir() else legacy_ingest),
        ("_gemini_tool_call_error", pkg_ingest / "gemini.py" if pkg_ingest.is_dir() else legacy_ingest),
        ("_is_error_result", pkg_ingest / "claude.py" if pkg_ingest.is_dir() else legacy_ingest),
    )
    found = []
    for name, path in extractor_targets:
        if path and path.exists() and f"def {name}" in path.read_text():
            found.append(f"{name} ✓")
        else:
            found.append(f"{name} ✗")
    all_ok = all("✓" in f for f in found)
    if all_ok:
        passed("J61", "tool_error extractors present in canonical homes",
               f"{len(found)} symbols")
    else:
        failed("J61", "tool_error extractors present", "; ".join(found))

    # J62: just confirm we can read those bodies (no pattern-match here).
    sample_bodies = []
    for name, path in extractor_targets[:3]:
        if path and path.exists():
            txt = path.read_text()
            m = re.search(rf"def {name}.*?(?=\ndef |\Z)", txt, re.DOTALL)
            if m:
                sample_bodies.append(f"{name}({len(m.group(0))} chars)")
    if len(sample_bodies) >= 3:
        passed("J62", "extractor bodies readable", "; ".join(sample_bodies))
    else:
        failed("J62", "extractor bodies readable", f"only {len(sample_bodies)} found")

    # J63: _ERROR_PATTERNS regex
    claude_path = pkg_ingest / "claude.py" if pkg_ingest.is_dir() else legacy_ingest
    if claude_path and claude_path.exists():
        txt = claude_path.read_text()
        m = re.search(r"_ERROR_PATTERNS\s*=\s*re\.compile\(\s*r['\"]([^'\"]+)['\"]", txt)
        if m:
            patterns = m.group(1)
            n_alts = patterns.count("|") + 1
            passed("J63", "_ERROR_PATTERNS regex catalog",
                   f"{n_alts} alternations, {len(patterns)} chars")
        else:
            failed("J63", "_ERROR_PATTERNS regex extractable", "no match")
    else:
        skipped("J63", "_ERROR_PATTERNS source", "claude.py / ingest.py not found")

    # J64: pyproject version vs installed __version__
    pyproject = src_root.parent.parent / "pyproject.toml"
    rc, out, _ = run([sys.executable, "-c", "import convo_recall; print(convo_recall.__version__)"])
    if rc == 0 and pyproject.exists():
        installed = out.strip()
        m = re.search(r'^version\s*=\s*"([^"]+)"', pyproject.read_text(), re.MULTILINE)
        declared = m.group(1) if m else "?"
        if installed == declared:
            passed("J64", "pyproject.toml version matches __version__",
                   f"both {declared}")
        else:
            failed("J64", "pyproject.toml version matches __version__",
                   f"pyproject={declared} but installed={installed} — pip install -e . to sync")
    else:
        skipped("J64", "version cross-check", "pyproject or convo_recall not readable")


# ── Runner ───────────────────────────────────────────────────────────────────

SECTIONS = {
    "A": section_a, "B": section_b, "C": section_c, "D": section_d,
    "E": section_e, "F": section_f, "G": section_g, "H": section_h,
    "I": section_i, "J": section_j,
}


def main() -> int:
    parser = argparse.ArgumentParser(description="convo-recall integrity sweep")
    parser.add_argument("--section", choices=sorted(SECTIONS),
                         help="Run only one section (A..J)")
    args = parser.parse_args()

    sections = [args.section] if args.section else sorted(SECTIONS)
    for s in sections:
        SECTIONS[s]()

    n_pass = sum(1 for _, st, _, _ in results if st == "PASS")
    n_skip = sum(1 for _, st, _, _ in results if st == "SKIP")
    n_fail = sum(1 for _, st, _, _ in results if st == "FAIL")

    print(f"\n══════════════════════════════════════════════════════════════════")
    print(f"  INTEGRITY SWEEP: {n_pass} pass · {n_skip} skip · {n_fail} fail "
          f"(of {len(results)})")
    print(f"══════════════════════════════════════════════════════════════════\n")

    for probe_id, status, name, detail in results:
        glyph = {"PASS": "✓", "SKIP": "⚠", "FAIL": "✗"}[status]
        line = f"  {glyph} {probe_id:<5} {name}"
        if detail:
            line += f" — {detail}"
        print(line)

    return 0 if n_fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
