"""Regression test for the stale-editable-install namespace-package shadow.

The concrete production symptom this guards against:

    cannot import name 'HubClient' from 'amplifier_browser_bridge' (unknown location)

See `_stale_install_guard.py`'s module docstring for the full mechanism: a
force-included `extension/` tree materializes real files under
`site-packages/amplifier_browser_bridge/` even for an editable install; if that
install's `.pth` redirect points at a source checkout that no longer exists, Python
resolves the bare leftover directory as an implicit namespace package instead --
`import amplifier_browser_bridge` succeeds, but the module has no code.

This test REPRODUCES that exact shape (a real directory, no `__init__.py`) rather
than asserting only the happy path or mocking the import machinery. It runs in a
subprocess with a scratch `sys.path` for two reasons: (1) namespace packages are
cached in `sys.modules` once resolved, so the real `amplifier_browser_bridge`
already imported by the rest of this test suite cannot be un-resolved or shadowed
in-process; (2) a subprocess is the actual unit of failure in production -- a
fresh Amplifier session hitting this on its own first import, not a corrupted
import cache in a long-running process.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

_GUARD_PACKAGE_PARENT = Path(__file__).resolve().parents[1]  # modules/tool-browser-bridge
_GUARD_MODULE_FILE = (
    _GUARD_PACKAGE_PARENT / "amplifier_module_tool_browser_bridge" / "_stale_install_guard.py"
)
_REPO_ROOT = Path(__file__).resolve().parents[3]


def _build_shadow_site_packages(tmp_path: Path, *, with_stale_pth: bool) -> Path:
    """Create a scratch directory containing ONLY the namespace-package shadow:
    an `amplifier_browser_bridge/` directory with real files (mirroring the
    force-included `extension/` tree) but no `__init__.py` anywhere under it --
    exactly what's left behind once the real source is unreachable.
    """
    site_packages = tmp_path / "site-packages"
    shadow_pkg = site_packages / "amplifier_browser_bridge" / "extension"
    shadow_pkg.mkdir(parents=True)
    (shadow_pkg / "manifest.json").write_text("{}", encoding="utf-8")

    if with_stale_pth:
        missing_target = tmp_path / "deleted-cache-dir" / "src"
        pth = site_packages / "_editable_impl_amplifier_browser_bridge.pth"
        pth.write_text(str(missing_target), encoding="utf-8")

    return site_packages


def _run_guard_in_subprocess(site_packages: Path) -> subprocess.CompletedProcess[str]:
    script = textwrap.dedent(
        f"""
        import importlib.util
        import sys

        # Load _stale_install_guard.py directly by file path rather than via
        # `import amplifier_module_tool_browser_bridge._stale_install_guard` --
        # the package's __init__.py imports amplifier_core (a peer dependency
        # irrelevant to this guard) and would need it on sys.path here for no
        # reason. The guard module itself is stdlib-only, so this works standalone.
        _spec = importlib.util.spec_from_file_location(
            "_stale_install_guard", {str(_GUARD_MODULE_FILE)!r}
        )
        _guard = importlib.util.module_from_spec(_spec)
        _spec.loader.exec_module(_guard)

        # PEP 420 namespace packages MERGE every same-named directory found across
        # sys.path -- but a REGULAR package (one with __init__.py) found anywhere
        # later in sys.path wins outright and stops the search. This subprocess is
        # launched with this repo's own venv interpreter, whose sys.path carries
        # BOTH the venv's site-packages AND (via the editable install's .pth
        # redirect) this repo's real `src/` directory directly -- either one would
        # resolve the REAL amplifier_browser_bridge and defeat this test. Strip
        # anything under this repo's own root, or under any site-packages, keeping
        # only the stdlib entries needed to run at all.
        _repo_root = {str(_REPO_ROOT)!r}
        sys.path = [p for p in sys.path if "site-packages" not in p and not p.startswith(_repo_root)]
        sys.path.insert(0, {str(site_packages)!r})

        try:
            from amplifier_browser_bridge import HubClient, HubError, Target  # noqa: F401
        except ImportError as exc:
            print("ORIGINAL_ERROR_TEXT:" + str(exc))
            try:
                _guard.reraise_with_diagnosis(exc)
            except _guard.StaleEditableInstallError as diagnosed:
                print("DIAGNOSED_AS_SHADOW")
                print("MESSAGE_START")
                print(str(diagnosed))
                print("MESSAGE_END")
            except ImportError as reraised:
                print("RERAISED_UNCHANGED:" + str(reraised))
        else:
            print("IMPORTED_OK")
        """
    )
    return subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def test_namespace_shadow_is_diagnosed_with_actionable_message(tmp_path: Path) -> None:
    """The core regression case: a namespace-package shadow with a stale `.pth`
    file pointing at a deleted directory must be diagnosed as exactly that, not
    left as the original cryptic "cannot import name ... (unknown location)".
    """
    site_packages = _build_shadow_site_packages(tmp_path, with_stale_pth=True)
    result = _run_guard_in_subprocess(site_packages)

    assert result.returncode == 0, f"subprocess failed:\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"

    # The real, cryptic production error must actually reproduce here -- this is
    # the whole point of building a real namespace-package shadow rather than
    # mocking the import.
    assert (
        "cannot import name 'HubClient' from 'amplifier_browser_bridge' (unknown location)" in result.stdout
    )

    assert "DIAGNOSED_AS_SHADOW" in result.stdout

    # The actionable message must name: what's wrong, the stale .pth file, the
    # missing target path, and the fix -- not just "something is missing".
    assert "namespace package" in result.stdout
    assert "_editable_impl_amplifier_browser_bridge.pth" in result.stdout
    assert "deleted-cache-dir" in result.stdout
    assert "does not exist on disk" in result.stdout
    assert "amplifier reset --remove cache -y" in result.stdout


def test_namespace_shadow_without_a_locatable_pth_still_diagnoses(tmp_path: Path) -> None:
    """Even if the stale `.pth` file itself can't be located (e.g. it was already
    cleaned up, or lives somewhere this search doesn't check), the shadow itself
    -- a resolved module with no `__file__` -- is still enough to diagnose
    correctly. The message degrades gracefully: no false claim of having found a
    specific `.pth` file, but still names the real problem and the fix.
    """
    site_packages = _build_shadow_site_packages(tmp_path, with_stale_pth=False)
    result = _run_guard_in_subprocess(site_packages)

    assert result.returncode == 0, f"subprocess failed:\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    assert "DIAGNOSED_AS_SHADOW" in result.stdout
    assert "namespace package" in result.stdout
    assert "amplifier reset --remove cache -y" in result.stdout
    # Honest degradation: nothing to claim about a specific stale .pth file here.
    assert "_editable_impl_amplifier_browser_bridge.pth" not in result.stdout.split("MESSAGE_START")[1]


def test_genuinely_missing_package_is_left_unchanged(tmp_path: Path) -> None:
    """A package that is not installed at all (no directory of that name
    anywhere on sys.path) is a different, already-clear failure -- the guard
    must NOT claim the namespace-shadow diagnosis when the evidence doesn't
    support it, and must re-raise the original error unchanged.
    """
    empty_site_packages = tmp_path / "site-packages"
    empty_site_packages.mkdir()

    result = _run_guard_in_subprocess(empty_site_packages)

    assert result.returncode == 0, f"subprocess failed:\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    assert "DIAGNOSED_AS_SHADOW" not in result.stdout
    assert "RERAISED_UNCHANGED:No module named 'amplifier_browser_bridge'" in result.stdout
