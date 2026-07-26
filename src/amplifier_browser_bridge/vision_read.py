"""`vision_read`: compose a `screenshot` capture with `vision.extract_text`.

This is the "return text extracted from pixels" mechanism (see
docs/designs/browser-bridge.md §13 and docs/PROTOCOL.md's "Vision-based
extraction" section) -- a distinct, explicitly-named operation from plain
`screenshot` (which returns pixels and makes no model call). Nothing here runs
unless a caller invokes this exact function/command; `screenshot`,
`fetch_bytes`, `grab_image`, `read`, and `snapshot` are all completely
unaffected and make no model call, ever.

Lives at the agent-surface layer (this Python lib), not in the hub or
extension -- see vision.py's module docstring for why. This module owns only
the COMPOSITION: call `screenshot` (with whatever capture args the caller
supplied) via the existing `HubClient`, then hand the resulting image bytes to
`vision.extract_text`. It adds no new wire-protocol command and no new
extension code.
"""

from __future__ import annotations

import base64
from typing import Any, Protocol

from .addressing import Target
from .vision import VisionConfig, extract_text

DEFAULT_PROMPT = (
    "Transcribe all text visible in this image (or these images, in order, if there are "
    "multiple pages) as accurately as possible. Preserve reading order, headings, and "
    "structure where evident. If a page/image contains no readable text, say so explicitly "
    "for that page rather than omitting it."
)


class _CommandClient(Protocol):
    """Structural type for the one `HubClient` method this module actually needs --
    `HubClient` itself satisfies this, and so does a duck-typed test double (see
    tests/test_vision.py) without either needing to inherit from the other."""

    async def command(self, target: Target, command: str, args: dict[str, Any]) -> dict[str, Any]: ...


async def vision_read(
    client: _CommandClient,
    target: Target,
    *,
    prompt: str | None = None,
    frame_id: int | None = None,
    multi_page: bool = False,
    max_pages: int | None = None,
    scroll_selector: str | None = None,
    page_delay_ms: int | None = None,
    capture_hidden: bool = True,
    timeout_s: float | None = None,
    vision_config: VisionConfig | None = None,
) -> dict[str, Any]:
    """Capture a screenshot (or multi-page sequence) and extract text from it via
    a vision-capable LLM. Returns the hub's own `{"status": "queued", ...}` shape
    UNCHANGED if the target device isn't live (never blocks, same guarantee as
    every other command -- see docs/PROTOCOL.md's tier model) -- the vision model
    is only ever called once real pixels have actually come back. On a capture
    failure (`{"ok": false, ...}`), that failure is returned as-is; the vision
    model is never called with no image.

    `capture_hidden` defaults to True here (unlike the raw `screenshot` command,
    which defaults to False) because `vision_read` exists specifically to reach
    tabs/content that other mechanisms cannot -- co-working etiquette (never
    activating a tab to look at it) is exactly the scenario this command is for.
    Pass `capture_hidden=False` to require the target tab already be active
    instead (e.g. on a device without the debugger/CDP capability).
    """
    screenshot_args: dict[str, Any] = {"capture_hidden": capture_hidden}
    if frame_id is not None:
        screenshot_args["frame_id"] = frame_id
    if multi_page:
        screenshot_args["multi_page"] = True
        screenshot_args["max_pages"] = max_pages if max_pages is not None else 10
    if scroll_selector is not None:
        screenshot_args["scroll_selector"] = scroll_selector
    if page_delay_ms is not None:
        screenshot_args["page_delay_ms"] = page_delay_ms
    if timeout_s is not None:
        screenshot_args["timeout_s"] = timeout_s

    shot = await client.command(target, "screenshot", screenshot_args)

    # Pass through queued/error verbatim -- the same tier-pass-through
    # guarantee every other agent-surface adapter in this codebase honors
    # (see mcp_server.py's module docstring). The vision model is never
    # called without real pixels in hand.
    if not shot.get("ok"):
        return shot

    result = shot["result"]
    images_b64: list[str]
    if "pages" in result:
        images_b64 = [p["base64"] for p in result["pages"]]
        capture_meta = {
            "page_count": result.get("page_count"),
            "capped": result.get("capped"),
            "stopped_reason": result.get("stopped_reason"),
            "region": result.get("region"),
            "via": result.get("via"),
        }
    else:
        if "base64" not in result:
            return {
                "ok": False,
                "error": (
                    "vision_read: the screenshot capture returned no image data "
                    f"(result keys: {sorted(result)}) -- cannot extract text from nothing"
                ),
            }
        images_b64 = [result["base64"]]
        capture_meta = {
            "page_count": 1,
            "capped": False,
            "region": result.get("region"),
            "via": result.get("via"),
        }

    images = [base64.b64decode(b64) for b64 in images_b64]
    extraction = await extract_text(images, prompt or DEFAULT_PROMPT, config=vision_config)

    return {
        "ok": True,
        "result": {
            "text": extraction["text"],
            "vision_provider": extraction["provider"],
            "vision_model": extraction["model"],
            "image_count": extraction["image_count"],
            "frame_id": result.get("frame_id"),
            **capture_meta,
        },
    }
