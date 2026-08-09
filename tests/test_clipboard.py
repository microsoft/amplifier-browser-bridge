"""Tests for clipboard.py -- best-effort OSC 52 terminal clipboard copy.

See that module's docstring for why OSC 52 (not a library, not shelling out to
xclip/pbcopy/clip.exe) is the right mechanism here: it works identically over
SSH into a headless machine, which is exactly how this project's hub is
frequently run.
"""

from __future__ import annotations

import base64
import io

from amplifier_browser_bridge.clipboard import copy_to_clipboard


class _FakeTty(io.StringIO):
    def isatty(self) -> bool:
        return True


class _FakeNonTty(io.StringIO):
    def isatty(self) -> bool:
        return False


def test_copy_to_clipboard_writes_a_valid_osc52_sequence_when_stream_is_a_tty() -> None:
    stream = _FakeTty()
    result = copy_to_clipboard("hello world", stream=stream)

    assert result is True
    written = stream.getvalue()
    assert written.startswith("\x1b]52;c;")
    assert written.endswith("\x07")
    payload_b64 = written[len("\x1b]52;c;") : -len("\x07")]
    assert base64.b64decode(payload_b64).decode("utf-8") == "hello world"


def test_copy_to_clipboard_is_a_no_op_when_stream_is_not_a_tty() -> None:
    """Piped/redirected output (a script, CI, non-interactive `init`) must never
    have a control sequence written into it -- that would corrupt whatever is
    actually consuming the output."""
    stream = _FakeNonTty()
    result = copy_to_clipboard("hello world", stream=stream)

    assert result is False
    assert stream.getvalue() == ""


def test_copy_to_clipboard_never_throws_for_a_stream_with_no_isatty() -> None:
    """Defensive: some stream-like objects (e.g. certain test doubles) may not
    implement isatty() at all -- treated as \"not a real terminal\", never a
    crash."""

    class _NoIsAtty:
        def write(self, _: str) -> int:
            raise AssertionError("must not write when isatty is unavailable")

        def flush(self) -> None:
            raise AssertionError("must not flush when isatty is unavailable")

    result = copy_to_clipboard("x", stream=_NoIsAtty())
    assert result is False


def test_copy_to_clipboard_handles_non_ascii_text() -> None:
    stream = _FakeTty()
    copy_to_clipboard("7F3K9-QXTM2@100.124.126.19:8900 -- \u2713", stream=stream)
    written = stream.getvalue()
    payload_b64 = written[len("\x1b]52;c;") : -len("\x07")]
    assert base64.b64decode(payload_b64).decode("utf-8") == "7F3K9-QXTM2@100.124.126.19:8900 -- \u2713"
