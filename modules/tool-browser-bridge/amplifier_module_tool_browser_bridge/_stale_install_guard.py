"""Guard against the "stale editable install" namespace-package shadow.

Concrete failure this exists to catch:

    cannot import name 'HubClient' from 'amplifier_browser_bridge' (unknown location)

Root cause (measured, not theoretical): `pyproject.toml`'s
`[tool.hatch.build.targets.wheel.force-include]` ships the `extension/` tree (and
INSTALL.md) INSIDE the `amplifier_browser_bridge` package namespace so
`find_extension_source()` and the `/setup` zip can locate it from a real
(non-editable) install too -- see that force-include's own comment in
`pyproject.toml`, and `tests/test_packaging.py`. That force-include is load-bearing
and must not be weakened.

The side effect: for an *editable* install, hatchling/uv still materialize those
force-included files as real static files directly under
`site-packages/amplifier_browser_bridge/` (e.g. `.../amplifier_browser_bridge/
extension/manifest.json`), alongside a `_editable_impl_amplifier_browser_bridge.pth`
file that is the ACTUAL redirect to the real source (this repo's `src/
amplifier_browser_bridge/`, containing `__init__.py`, `client.py`, etc.).

If that `.pth` file's target directory stops existing -- e.g. the Amplifier module
cache directory it pointed at was deleted, or the repo's clone URL moved and the
cache-dir hash changed -- Python's own `site` module skips adding that (now
nonexistent) directory to `sys.path`. Nothing points at the real source anymore. But
`site-packages/amplifier_browser_bridge/` (the directory of leftover force-included
extension files, with NO `__init__.py`) still physically exists on disk and is still
on `sys.path` (it's inside site-packages itself). Python 3's implicit namespace
package mechanism finds that bare directory and resolves the *name*
`amplifier_browser_bridge` to it -- so `import amplifier_browser_bridge` quietly
succeeds, but the module has no code, no `__file__`, and no `HubClient`. Every name
import from it then fails with the cryptic "(unknown location)" ImportError above,
which names a missing class instead of the actual problem: a dead install pointer.

`import_hub_client_or_explain()` detects this exact shape (a resolved module with no
`__file__`) and raises `StaleEditableInstallError` naming what was found -- the stale
`.pth` file and the path it points at that no longer exists, if locatable -- and the
fix (reinstalling the package so its editable-install pointer is rebuilt), instead of
leaving the caller to debug a missing-class ImportError that has nothing to do with
`HubClient` itself.

Note: this module has exactly one declared dependency (`amplifier-browser-bridge`,
this repo's own root package -- see `pyproject.toml`). It has no dependency on, and
must not name, any specific *host* tool's reinstall/cache-reset command -- whatever
process installed this package (uv, pip, or a host application's own dependency
manager) is what the fix below applies to.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from typing import Any, NoReturn


class StaleEditableInstallError(ImportError):
    """`amplifier_browser_bridge` resolved as an empty namespace-package shadow
    instead of the real module -- see this file's module docstring for the full
    mechanism."""


def _find_stale_editable_pth(distribution_import_name: str) -> tuple[Path, str] | None:
    """Search `sys.path` for this package's hatchling/uv editable-install `.pth`
    file, and return `(pth_path, stale_target)` if its recorded target directory no
    longer exists on disk. Returns `None` if no such `.pth` is found anywhere on
    `sys.path`, or if its target is fine -- this is best-effort diagnostics, not a
    required part of detecting the shadow itself.
    """
    pth_name = f"_editable_impl_{distribution_import_name}.pth"
    for entry in sys.path:
        if not entry:
            continue
        candidate = Path(entry) / pth_name
        try:
            if not candidate.is_file():
                continue
            target = candidate.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if target and not Path(target).exists():
            return candidate, target
    return None


def _explain_namespace_shadow(module: Any) -> str:
    stale = _find_stale_editable_pth("amplifier_browser_bridge")
    search_paths = [str(p) for p in (getattr(module, "__path__", None) or [])]

    lines = [
        (
            "amplifier_browser_bridge resolved as an empty namespace package (no "
            "__init__.py, no code) instead of the real module -- HubClient/HubError/"
            "Target are not defined anywhere reachable."
        ),
        "",
        (
            "This is the 'stale editable install' hazard: pyproject.toml force-includes "
            "extension/ inside the amplifier_browser_bridge package directory, so a "
            "site-packages/amplifier_browser_bridge/ directory of real (non-code) files "
            "exists even for an editable install. When that install's .pth redirect "
            "points at a source checkout that no longer exists -- e.g. after an "
            "Amplifier cache reset, or after the repo's clone URL changed and the cache "
            "directory hash changed with it -- Python silently falls back to that "
            "leftover directory and treats it as a namespace package: "
            "'import amplifier_browser_bridge' succeeds, but every real name import "
            "from it fails."
        ),
    ]

    if stale is not None:
        pth_path, target = stale
        lines += [
            "",
            f"Detected stale editable-install pointer: {pth_path}",
            f"  -> points at: {target}",
            "  -> that path does not exist on disk.",
        ]

    if search_paths:
        lines += ["", f"amplifier_browser_bridge namespace search path(s): {search_paths}"]

    lines += [
        "",
        (
            "Fix: reinstall the amplifier_browser_bridge package so this editable-install "
            "pointer is rebuilt (e.g. `pip install -e .` or `uv sync` from this "
            "repository, or whatever your host tool uses to (re)install Python packages), "
            "then restart."
        ),
    ]
    return "\n".join(lines)


def reraise_with_diagnosis(exc: ImportError) -> NoReturn:
    """Call from the `except ImportError` clause wrapping a real, static
    `from amplifier_browser_bridge import HubClient, HubError, Target` -- see this
    module's docstring for why that import (not a dynamic `importlib` lookup) is
    kept as the actual source of these names: pyright needs the real static import
    to type `HubClient`/`HubError`/`Target` as their real classes, not as generic
    `type`, so callers can use them in annotations (`-> Target:`) and `except`
    clauses (`except HubError:`).

    Distinguishes the empty-namespace-package shadow (see module docstring) from a
    genuine "not installed at all" failure:

    - Shadow (namespace package, no `__file__`): raises `StaleEditableInstallError`
      with the full diagnosis instead of the original cryptic
      "cannot import name 'HubClient' ... (unknown location)".
    - Anything else (package genuinely missing, or a real module missing these
      names for some other reason): re-raises the original exception unchanged --
      it is already a clear, correct error naming exactly what's missing, and this
      guard has no more specific diagnosis to add.
    """
    try:
        module = importlib.import_module("amplifier_browser_bridge")
    except ImportError:
        raise exc from None  # not installed at all; the original error is already clear

    if getattr(module, "__file__", None) is None:
        raise StaleEditableInstallError(_explain_namespace_shadow(module)) from exc

    raise exc
