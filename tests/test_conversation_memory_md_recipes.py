"""Validity test for Python recipes embedded in ~/.claude/rules/conversation-memory.md.

Extracts every fenced ```python``` block under the `## Ad-Hoc SQLite Probing`
section and runs each against the live convo-recall DB in read-only mode.
Catches schema drift between the rule file (which loads into every Claude Code
session) and the actual DB schema.

Auto-skips when the rule file or DB is absent (CI / fresh machines).
"""
from __future__ import annotations

import re
import sqlite3
from pathlib import Path

import pytest

RULE_FILE = Path.home() / ".claude" / "rules" / "conversation-memory.md"
DB_PATH = Path.home() / ".local" / "share" / "convo-recall" / "conversations.db"
SECTION_HEADER = "## Ad-Hoc SQLite Probing"


def _extract_python_blocks(markdown: str, after_header: str) -> list[str]:
    idx = markdown.find(after_header)
    if idx < 0:
        return []
    body = markdown[idx + len(after_header):]
    next_h2 = re.search(r"^## ", body, flags=re.MULTILINE)
    if next_h2:
        body = body[: next_h2.start()]
    return re.findall(r"```python\n(.*?)```", body, flags=re.DOTALL)


def _collect_execute_calls(block: str) -> list[str]:
    """Pull every SQL string passed to con.execute(...) in the block."""
    calls: list[str] = []
    for m in re.finditer(r"con\.execute\(\s*(\"\"\"|''')(.*?)\1", block, flags=re.DOTALL):
        calls.append(m.group(2))
    for m in re.finditer(r'con\.execute\(\s*"([^"\n]+)"', block):
        calls.append(m.group(1))
    return calls


@pytest.fixture(scope="module")
def ro_con():
    if not RULE_FILE.exists():
        pytest.skip(f"{RULE_FILE} not present on this machine")
    if not DB_PATH.exists():
        pytest.skip(f"{DB_PATH} not present on this machine")
    con = sqlite3.connect(f"file:{DB_PATH}?immutable=1", uri=True)
    yield con
    con.close()


def test_ad_hoc_section_has_python_recipes(ro_con):
    blocks = _extract_python_blocks(RULE_FILE.read_text(), SECTION_HEADER)
    assert blocks, "no ```python``` recipes under '## Ad-Hoc SQLite Probing'"


def test_every_python_recipe_runs_against_live_db(ro_con):
    blocks = _extract_python_blocks(RULE_FILE.read_text(), SECTION_HEADER)
    failures: list[str] = []
    for i, block in enumerate(blocks, 1):
        for sql in _collect_execute_calls(block):
            try:
                ro_con.execute(sql).fetchall()
            except sqlite3.OperationalError as e:
                failures.append(f"recipe #{i}: OperationalError on SQL\n{sql.strip()}\n→ {e}")
            except sqlite3.ProgrammingError as e:
                failures.append(f"recipe #{i}: ProgrammingError on SQL\n{sql.strip()}\n→ {e}")
    assert not failures, "\n\n".join(failures)
