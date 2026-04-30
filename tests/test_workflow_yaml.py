"""C-3 — fast local check that `.github/workflows/test.yml` runs the
OS matrix on both macos-latest and ubuntu-latest.

A failed CI run is a slow signal; this gives the same info in <50ms.
"""

from pathlib import Path

import pytest


_WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "test.yml"


def _load_workflow():
    yaml = pytest.importorskip("yaml")
    return yaml.safe_load(_WORKFLOW.read_text())


def test_test_yml_exists():
    assert _WORKFLOW.is_file(), f"missing {_WORKFLOW}"


def test_workflow_runs_on_matrix_os():
    wf = _load_workflow()
    matrix = wf["jobs"]["test"]["strategy"]["matrix"]
    assert "os" in matrix, "jobs.test.strategy.matrix.os missing"
    assert "macos-latest" in matrix["os"], matrix["os"]
    assert "ubuntu-latest" in matrix["os"], matrix["os"]


def test_workflow_runs_on_matrix_os_var():
    wf = _load_workflow()
    runs_on = wf["jobs"]["test"]["runs-on"]
    assert "${{ matrix.os }}" in str(runs_on), (
        f"runs-on must reference the os matrix var; got {runs_on!r}"
    )


def test_workflow_does_not_fail_fast():
    """fail-fast: false so a Linux flake doesn't cancel the macOS leg."""
    wf = _load_workflow()
    strategy = wf["jobs"]["test"]["strategy"]
    assert strategy.get("fail-fast") is False, (
        f"fail-fast should be False; got {strategy.get('fail-fast')!r}"
    )


def test_workflow_python_version_matrix_kept():
    """Don't accidentally drop the Python-version axis when adding os."""
    wf = _load_workflow()
    matrix = wf["jobs"]["test"]["strategy"]["matrix"]
    assert "python-version" in matrix
    assert "3.11" in matrix["python-version"]
    assert "3.12" in matrix["python-version"]
