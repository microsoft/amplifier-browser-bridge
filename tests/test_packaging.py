"""Regression test for Bug A: extension/ assets must ship INSIDE the wheel, not
just be inferable from an editable-install checkout.

The concrete failure this guards against: `abb init` worked for the developer (an
editable install, `uv pip install -e .`, resolves straight back to the repo
checkout) but raised `ExtensionSourceNotFoundError` for anyone who installed the
published package normally (`uv tool install .`, `pip install .`) -- the wheel
simply didn't contain `extension/` at all.

This builds a REAL wheel via `uv build` and inspects its actual contents -- a
config-shape assertion alone (e.g. "pyproject.toml has a force-include entry")
would not have caught a typo'd path or a build backend that silently ignores the
config the way opening the real archive does.
"""

from __future__ import annotations

import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

from amplifier_browser_bridge.setup import _EXTENSION_FILES

REPO_ROOT = Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.skipif(shutil.which("uv") is None, reason="uv not available to build a real wheel")


def test_wheel_contains_every_runtime_required_extension_file(tmp_path: Path) -> None:
    """Build the real wheel this project ships and assert every file
    `stage_extension` needs (setup.py's `_EXTENSION_FILES` -- the single source of
    truth for what's actually required at runtime) is present inside it, at the
    exact path `find_extension_source`'s packaged-install branch expects
    (`amplifier_browser_bridge/extension/<name>`)."""
    result = subprocess.run(
        ["uv", "build", "--wheel", "-o", str(tmp_path), str(REPO_ROOT)],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, f"uv build failed:\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"

    wheels = list(tmp_path.glob("*.whl"))
    assert len(wheels) == 1, f"expected exactly one built wheel, got {wheels}"

    with zipfile.ZipFile(wheels[0]) as zf:
        packaged_names = set(zf.namelist())

    missing = [
        name
        for name in _EXTENSION_FILES
        if f"amplifier_browser_bridge/extension/{name}" not in packaged_names
    ]
    assert not missing, (
        f"extension file(s) required by stage_extension() are NOT in the built wheel: {missing}. "
        "This is exactly the Bug A failure mode: `abb init` works from an editable checkout but "
        "breaks for a real (non-editable) install. Check [tool.hatch.build.targets.wheel."
        "force-include] in pyproject.toml."
    )
