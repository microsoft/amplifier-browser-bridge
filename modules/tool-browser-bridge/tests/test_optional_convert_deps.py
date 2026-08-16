"""Regression test for the eager-import bug: `amplifier_module_tool_browser_bridge`
must import (and register every one of its tools) even when neither `html2text`
nor `trafilatura` (this repo's optional `convert` extra -- pyproject.toml's
`[project.optional-dependencies]`) is installed. Before this fix, `mhtml_convert.py`
imported both at module top level, so a normal install without the `convert`
extra crashed the WHOLE tool module at import time -- none of its 27+ tools ever
registered, with no error surfaced to the caller at all.

Only actually CALLING the conversion path (`convert_mhtml`/`convert_mhtml_file`,
composed by `archive_convert.run_archive_convert`, exposed as the
`browser_archive_convert` tool) without the extra installed should fail -- and
it must fail LOUD with a clear, actionable remediation message, never a bare
`ModuleNotFoundError`.

No real dependency is uninstalled to prove this (that would break every other
test in this shared environment). Instead, "the convert extra is not
installed" is SIMULATED via `sys.modules`: setting `sys.modules[name] = None`
makes any subsequent `import name` raise `ImportError` -- CPython's own import
machinery (`importlib._bootstrap._find_and_load`) treats a `None` entry as "this
name is blocked", the exact mechanism `unittest.mock`'s import-blocking recipes
rely on. Combined with removing the already-imported tool module (and
mhtml_convert.py/archive_convert.py) from `sys.modules`, this forces a REAL
fresh import to run under the simulated-missing-deps condition -- not just a
no-op reuse of an already-imported (real-deps) module object from an earlier
test in this same process.
"""

from __future__ import annotations

import importlib
import sys
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

# Every package this repo's optional `convert` extra provides (trafilatura,
# html2text) or that arrives transitively through it (lxml, imported directly
# by mhtml_convert.py too -- see that module's docstring). Blocking all of
# these, not just the two extra-declared packages, is what proves the fix
# handles the transitive case as well.
_BLOCKED_MODULES = ("html2text", "trafilatura", "lxml", "lxml.etree", "lxml.html")

# Every module in the real import chain between the tool module and
# mhtml_convert.py's own (now-lazy) imports -- these must be evicted from
# sys.modules so re-importing the tool module actually re-executes their
# top-level code under the simulated-missing-deps condition, rather than
# reusing an already-imported module object from an earlier test.
_RELOAD_MODULES = (
    "amplifier_module_tool_browser_bridge",
    "amplifier_browser_bridge.archive_convert",
    "amplifier_browser_bridge.mhtml_convert",
)


@pytest.fixture
def convert_extra_missing(monkeypatch: pytest.MonkeyPatch):
    """Simulates "the optional `convert` extra is not installed" for the
    duration of one test -- see module docstring for the mechanism. Restores
    real sys.modules state automatically (monkeypatch's own teardown), then
    evicts the reload-chain modules once more so the NEXT test to import them
    gets a real, un-blocked re-import rather than reusing this test's
    simulated-missing-deps module objects.
    """
    for name in _BLOCKED_MODULES:
        monkeypatch.setitem(sys.modules, name, None)
    for name in _RELOAD_MODULES:
        monkeypatch.delitem(sys.modules, name, raising=False)
    yield
    for name in _RELOAD_MODULES:
        sys.modules.pop(name, None)


# ---------------------------------------------------------------------------
# The critical acceptance test: importing (and mounting) the tool module must
# succeed with zero optional convert deps present.
# ---------------------------------------------------------------------------


def test_tool_module_imports_and_registers_every_tool_without_convert_extra(
    convert_extra_missing: None,
) -> None:
    module = importlib.import_module("amplifier_module_tool_browser_bridge")
    tools = module._build_tools()
    names = {t.name for t in tools}

    # The two tools that (indirectly, via archive_convert/mhtml_convert) touch
    # the optional convert deps must still be registered -- only calling them
    # is gated on the extra, not the tool module loading at all.
    assert "browser_archive_convert" in names
    assert "browser_archive_catalog" in names
    # Every other tool this module ships must also still be present -- the
    # original bug crashed the ENTIRE module, not just these two tools.
    assert "browser_devices" in names
    assert "browser_archive" in names
    assert len(tools) == 31


@pytest.mark.asyncio
async def test_tool_module_mounts_every_tool_without_convert_extra(convert_extra_missing: None) -> None:
    module = importlib.import_module("amplifier_module_tool_browser_bridge")
    coordinator = MagicMock()
    coordinator.mount = AsyncMock()

    result = await module.mount(coordinator)

    expected_names = {t.name for t in module._build_tools()}
    registered_names = {c.kwargs.get("name") for c in coordinator.mount.call_args_list}
    assert registered_names == expected_names
    assert coordinator.mount.call_count == len(expected_names)
    assert "browser_archive_convert" in result["provides"]


def test_archive_convert_module_itself_imports_without_convert_extra(convert_extra_missing: None) -> None:
    """archive_convert.py's own top-level `from .mhtml_convert import ...`
    (the direct culprit named in the bug report) must not require
    html2text/trafilatura/lxml either."""
    module = importlib.import_module("amplifier_browser_bridge.archive_convert")
    assert hasattr(module, "run_archive_convert")
    assert hasattr(module, "ConversionError")


def test_mhtml_convert_module_itself_imports_without_convert_extra(convert_extra_missing: None) -> None:
    module = importlib.import_module("amplifier_browser_bridge.mhtml_convert")
    assert hasattr(module, "convert_mhtml")
    assert hasattr(module, "convert_mhtml_file")


# ---------------------------------------------------------------------------
# Fail-loud-at-point-of-use: actually calling the conversion path without the
# extra installed must raise a CLEAR, ACTIONABLE error, not a bare
# ModuleNotFoundError.
# ---------------------------------------------------------------------------

_SYNTHETIC_MHTML = (
    b"MIME-Version: 1.0\r\n"
    b'Content-Type: multipart/related; boundary="X"\r\n\r\n'
    b"--X\r\nContent-Type: text/html\r\nContent-Location: https://example.com/\r\n\r\n"
    b"<html><body><article><p>Synthetic test article text, not real user "
    b"browsing data.</p></article></body></html>\r\n"
    b"--X--\r\n"
)


def test_convert_mhtml_file_raises_clear_remediation_not_bare_import_error(
    convert_extra_missing: None, tmp_path: Any
) -> None:
    mhtml_convert = importlib.import_module("amplifier_browser_bridge.mhtml_convert")
    mhtml_path = tmp_path / "page.mhtml"
    mhtml_path.write_bytes(_SYNTHETIC_MHTML)

    with pytest.raises(ImportError) as exc_info:
        mhtml_convert.convert_mhtml_file(
            mhtml_path,
            assets_dir=tmp_path / "assets",
            markdown_dir=tmp_path / "markdown",
        )

    message = str(exc_info.value)
    # The load-bearing assertion: this must NOT be a bare ModuleNotFoundError
    # with no remediation -- it is our own ImportError carrying the exact fix.
    assert type(exc_info.value) is ImportError
    assert "convert" in message
    assert "pip install 'amplifier-browser-bridge[convert]'" in message


@pytest.mark.asyncio
async def test_browser_archive_convert_tool_surfaces_clear_remediation_without_convert_extra(
    convert_extra_missing: None, tmp_path: Any
) -> None:
    """End-to-end through the actual Amplifier tool surface: calling
    browser_archive_convert without the convert extra installed must not
    silently hang or crash uninformatively -- the ImportError (with the clear
    remediation message) propagates out of the tool's execute() call."""
    module = importlib.import_module("amplifier_module_tool_browser_bridge")

    archive_dir = tmp_path / "archive_d1_20260816T000000Z"
    tab_dir = archive_dir / "tabs" / "101"
    tab_dir.mkdir(parents=True)
    (tab_dir / "page.mhtml").write_bytes(_SYNTHETIC_MHTML)

    tools = module._build_tools()
    tool = next(t for t in tools if t.name == "browser_archive_convert")

    with pytest.raises(ImportError) as exc_info:
        await tool.execute({"archive_dir": str(archive_dir)})

    assert "pip install 'amplifier-browser-bridge[convert]'" in str(exc_info.value)
