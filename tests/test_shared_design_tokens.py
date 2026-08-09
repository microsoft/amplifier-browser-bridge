"""Guards docs/designs/onboarding-ux.md section 2's central claim: the CSS design
token block is pasted BYTE-FOR-BYTE identical into both `/setup` (onboarding.py)
and the extension's options page (extension/options.html). The two pages cannot
share a stylesheet by URL (one is server-rendered Python, the other ships inside
a browser extension), so the shared visual language depends entirely on this
block never drifting between the two copies -- this test is the drift detector.
"""

from __future__ import annotations

import re
from pathlib import Path

from amplifier_browser_bridge.onboarding import _TOKENS_CSS

_REPO_ROOT = Path(__file__).resolve().parents[1]
_OPTIONS_HTML = _REPO_ROOT / "extension" / "options.html"

_MARKER_RE = re.compile(r"/\* TOKENS:BEGIN.*?\*/(.*)/\* TOKENS:END \*/", re.DOTALL)


def _extract_tokens_block(text: str) -> str:
    match = _MARKER_RE.search(text)
    assert match is not None, "TOKENS:BEGIN/TOKENS:END markers not found"
    return match.group(1)


def test_onboarding_py_tokens_block_has_markers() -> None:
    assert "TOKENS:BEGIN" in _TOKENS_CSS
    assert "TOKENS:END" in _TOKENS_CSS


def test_options_html_tokens_block_matches_onboarding_py_byte_for_byte() -> None:
    options_html_text = _OPTIONS_HTML.read_text(encoding="utf-8")
    onboarding_tokens = _extract_tokens_block(_TOKENS_CSS)
    options_tokens = _extract_tokens_block(options_html_text)
    assert onboarding_tokens == options_tokens, (
        "The CSS token block in extension/options.html has drifted from the one in "
        "onboarding.py's _TOKENS_CSS -- docs/designs/onboarding-ux.md section 2 requires "
        "these to be byte-identical. Copy the block from one file to the other verbatim."
    )


def test_tokens_block_defines_every_token_the_spec_names() -> None:
    """A light structural check (not just equality with itself) -- pins down
    that the block actually contains the tokens docs/designs/onboarding-ux.md
    section 2 lists, so a future edit that accidentally drops one is caught
    here rather than only visually."""
    required_tokens = [
        "--t-meta",
        "--t-sm",
        "--t-body",
        "--t-title",
        "--t-page",
        "--lh-tight",
        "--lh-body",
        "--w-normal",
        "--w-semi",
        "--font",
        "--font-mono",
        "--s-1",
        "--s-2",
        "--s-3",
        "--s-4",
        "--s-6",
        "--s-8",
        "--s-12",
        "--radius",
        "--radius-sm",
        "--measure",
        "--surface",
        "--surface-raised",
        "--border",
        "--ink",
        "--ink-dim",
        "--ink-faint",
        "--accent",
        "--accent-ink",
        "--ok-bg",
        "--ok-ink",
        "--ok-line",
        "--pending-bg",
        "--pending-ink",
        "--pending-line",
        "--alert-bg",
        "--alert-ink",
        "--alert-line",
        "--caution-bg",
        "--caution-ink",
        "--caution-line",
    ]
    for token in required_tokens:
        assert f"{token}:" in _TOKENS_CSS, f"missing token {token} in onboarding.py's _TOKENS_CSS"


def test_tokens_block_uses_prefers_color_scheme_not_a_hardcoded_theme() -> None:
    """docs/designs/onboarding-ux.md section 2's explicit rationale: `/setup` was
    dark, options.html was light, unconditionally -- following the OS instead
    makes both pages agree with the browser chrome they sit in, for free."""
    assert "prefers-color-scheme: dark" in _TOKENS_CSS
