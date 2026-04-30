"""Item 6 — wheel packaging smoke test for the new install/ subpackage.

Hatch auto-includes everything under `packages = ["src/convo_recall"]`,
but a missing `__init__.py` in a new sub-tree (`install/schedulers/`)
would silently drop those modules from the wheel. This test builds a
wheel and asserts the new modules are present + importable.

Skipped (not failed) when `build` isn't installed — keeps the suite
green for users who only run unit tests. CI installs `build` explicitly.
"""

import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
EXPECTED_FILES = {
    "convo_recall/install/__init__.py",
    "convo_recall/install/_paths.py",
    "convo_recall/install/schedulers/__init__.py",
    "convo_recall/install/schedulers/base.py",
    "convo_recall/install/schedulers/launchd.py",
}


def _have(module: str) -> bool:
    try:
        __import__(module)
        return True
    except ImportError:
        return False


@pytest.mark.skipif(not _have("build"), reason="`pip install build` to run wheel smoke test")
def test_wheel_includes_install_subpackage(tmp_path):
    out_dir = tmp_path / "dist"
    result = subprocess.run(
        [sys.executable, "-m", "build", "--wheel", "--outdir", str(out_dir)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"`python -m build --wheel` failed:\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )

    wheels = list(out_dir.glob("convo_recall-*.whl"))
    assert wheels, f"no wheel produced in {out_dir}"
    wheel_path = wheels[0]

    with zipfile.ZipFile(wheel_path) as zf:
        names = set(zf.namelist())

    missing = EXPECTED_FILES - names
    assert not missing, f"wheel missing expected install files: {sorted(missing)}"


@pytest.mark.skipif(not _have("build"), reason="`pip install build` to run wheel smoke test")
def test_wheel_imports_launchd_scheduler_in_clean_venv(tmp_path):
    """Build the wheel, install it into a fresh venv, import LaunchdScheduler.

    This catches the failure mode where the wheel includes the file at the
    right path but a missing `__init__.py` makes it un-importable as
    `convo_recall.install.schedulers.launchd`.
    """
    out_dir = tmp_path / "dist"
    subprocess.run(
        [sys.executable, "-m", "build", "--wheel", "--outdir", str(out_dir)],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
    )
    wheel = next(out_dir.glob("convo_recall-*.whl"))

    venv_dir = tmp_path / "venv"
    subprocess.run([sys.executable, "-m", "venv", str(venv_dir)], check=True)
    venv_python = venv_dir / "bin" / "python"
    assert venv_python.exists(), f"venv python not at {venv_python}"

    subprocess.run(
        [str(venv_python), "-m", "pip", "install", "--quiet", str(wheel)],
        check=True,
        capture_output=True,
    )

    probe = subprocess.run(
        [
            str(venv_python),
            "-c",
            "from convo_recall.install.schedulers.launchd import LaunchdScheduler; "
            "from convo_recall.install.schedulers.base import Scheduler, Result; "
            "from convo_recall.install._paths import scheduler_unit_dir; "
            "assert issubclass(LaunchdScheduler, Scheduler); "
            "print('ok')",
        ],
        capture_output=True,
        text=True,
    )
    assert probe.returncode == 0, (
        f"clean-venv import failed:\nstdout:{probe.stdout}\nstderr:{probe.stderr}"
    )
    assert "ok" in probe.stdout

    # Cleanup happens via tmp_path teardown; double-tap for safety on macOS.
    if venv_dir.exists():
        shutil.rmtree(venv_dir, ignore_errors=True)
