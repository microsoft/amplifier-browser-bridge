"""Best-effort terminal clipboard copy for `cli.py`'s `init`/`pair` output.

## The problem this fixes

A pairing link/code is meant to be opened/pasted on a *different* device than
the one the CLI is running on (the whole point of this project -- see
`docs/designs/browser-bridge.md`), and the CLI itself is frequently run over
SSH into a headless machine with no local display at all (the maintainer's own
hub, for one). A conventional clipboard library (`pyperclip` and friends) needs
a real display/clipboard mechanism reachable from the process -- `xclip`/`xsel`
on Linux, `pbcopy` on macOS, `clip.exe` on Windows -- none of which exist on a
bare SSH session into a Linux box with no X server.

## The fix: OSC 52, not a new dependency

OSC 52 is a terminal escape sequence (`ESC ] 52 ; c ; <base64> BEL`) that asks
the *terminal emulator* -- not the remote process -- to set the system
clipboard, exactly like any other display escape sequence (colors, cursor
position). Because it travels over the same channel as the rest of the
program's output, it works identically whether the terminal is local or the
far end of an SSH session -- there is no "reach the display" problem to solve,
because nothing here needs to reach the display; the terminal is already
looking at its own stdin. Support is broad among the terminals this project's
users are likely running (iTerm2, Windows Terminal, GNOME Terminal via VTE,
kitty, wezterm, tmux with the right passthrough); a terminal that does not
support it either ignores the sequence or displays nothing meaningful --
either way, harmless, since the code is ALWAYS ALSO printed as plain text
right below it (see cli.py) -- copying is a convenience, never the only way to
get the code. This needs zero new dependencies (see IMPLEMENTATION_PHILOSOPHY's
"Library vs Custom Code": the problem is simple, well-understood, and a
library would add a dependency to solve something one clearly-scoped function
already does correctly).
"""

from __future__ import annotations

import base64
import sys

__all__ = ["copy_to_clipboard"]


def copy_to_clipboard(text: str, *, stream: object | None = None) -> bool:
    """Best-effort copy `text` to the *terminal's* clipboard via an OSC 52
    escape sequence, working across SSH exactly as well as locally (see
    module docstring).

    Args:
        text: The text to copy (e.g. a pairing link or code).
        stream: Where to write the escape sequence -- defaults to `sys.stdout`.
            Exposed for testing; never anything but stdout in real use.

    Returns:
        True if the escape sequence was written (does NOT confirm the
        terminal actually understood it -- there is no ack for OSC 52; a
        non-supporting terminal simply ignores it). False when the target
        stream is not a real terminal at all (piped/redirected output,
        non-interactive `init`, CI) -- writing control sequences into a file
        or another program's stdin would be actively wrong, not just useless.
    """
    out = stream if stream is not None else sys.stdout
    isatty = getattr(out, "isatty", None)
    if not callable(isatty) or not isatty():
        return False

    encoded = base64.b64encode(text.encode("utf-8")).decode("ascii")
    out.write(f"\x1b]52;c;{encoded}\x07")  # type: ignore[attr-defined]
    out.flush()  # type: ignore[attr-defined]
    return True
