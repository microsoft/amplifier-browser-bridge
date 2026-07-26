"""Tests for vision.py (provider resolution) and vision_read.py (the composition of
a `screenshot` capture with a vision-model text-extraction call).

No real network calls are made here -- `extract_text`'s provider dispatch is
exercised via `resolve_provider()` (pure env-var logic) and `vision_read()`'s
composition logic is tested with a fake `HubClient` + a monkeypatched
`extract_text`, never a real `aiohttp` call. Provider-specific HTTP request
shapes (`_call_anthropic`/`_call_openai`/`_call_gemini`) are integration-tested
live (see the issue's proof section), not here -- this file covers the pure,
deterministic logic this codebase's testing convention actually unit-tests.
"""

from __future__ import annotations

from typing import Any

import pytest

from amplifier_browser_bridge.addressing import Target
from amplifier_browser_bridge.vision import VisionConfigError, resolve_provider
from amplifier_browser_bridge.vision_read import vision_read

# ---------------------------------------------------------------------------
# resolve_provider() -- pure env-var logic
# ---------------------------------------------------------------------------


def test_resolve_provider_fails_loud_with_no_keys_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        "GOOGLE_API_KEY",
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "ABB_VISION_PROVIDER",
        "ABB_VISION_MODEL",
    ):
        monkeypatch.delenv(var, raising=False)

    with pytest.raises(VisionConfigError) as exc_info:
        resolve_provider()

    message = str(exc_info.value)
    assert "GOOGLE_API_KEY" in message
    assert "ANTHROPIC_API_KEY" in message
    assert "OPENAI_API_KEY" in message


def test_resolve_provider_picks_first_configured_in_priority_order(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ABB_VISION_PROVIDER", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-test")

    cfg = resolve_provider()

    assert cfg.provider == "anthropic"  # gemini not configured, anthropic is next in priority
    assert cfg.api_key == "sk-ant-test"


def test_resolve_provider_override_pins_a_specific_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GOOGLE_API_KEY", "google-test")
    monkeypatch.setenv("OPENAI_API_KEY", "openai-test")
    monkeypatch.setenv("ABB_VISION_PROVIDER", "openai")

    cfg = resolve_provider()

    assert cfg.provider == "openai"
    assert cfg.api_key == "openai-test"


def test_resolve_provider_override_without_matching_key_fails_loud(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("ABB_VISION_PROVIDER", "anthropic")

    with pytest.raises(VisionConfigError) as exc_info:
        resolve_provider()
    assert "ANTHROPIC_API_KEY" in str(exc_info.value)


def test_resolve_provider_model_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.setenv("ABB_VISION_MODEL", "claude-custom-model")

    cfg = resolve_provider()

    assert cfg.model == "claude-custom-model"


# ---------------------------------------------------------------------------
# vision_read() -- composition of screenshot capture + extract_text
# ---------------------------------------------------------------------------


class _FakeHubClient:
    def __init__(self, response: dict[str, Any]) -> None:
        self.response = response
        self.command_calls: list[tuple[Target, str, dict[str, Any]]] = []

    async def command(self, target: Target, command: str, args: dict[str, Any]) -> dict[str, Any]:
        self.command_calls.append((target, command, args))
        return self.response


@pytest.mark.asyncio
async def test_vision_read_passes_through_queued_result_without_calling_vision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queued = {"status": "queued", "command_id": "cmd-1", "tier": "intermittent", "queue_position": 1}
    fake = _FakeHubClient(queued)

    called = False

    async def _fake_extract_text(*args: Any, **kwargs: Any) -> dict[str, Any]:
        nonlocal called
        called = True
        return {"text": "should not be called", "provider": "x", "model": "y", "image_count": 1}

    monkeypatch.setattr("amplifier_browser_bridge.vision_read.extract_text", _fake_extract_text)

    result = await vision_read(fake, Target(device_id="d1", tab_id=7), prompt="extract text")

    assert result == queued
    assert called is False


@pytest.mark.asyncio
async def test_vision_read_passes_through_capture_error_without_calling_vision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    error = {"ok": False, "error": "capability unavailable on this device: chrome.debugger is not present"}
    fake = _FakeHubClient(error)

    called = False

    async def _fake_extract_text(*args: Any, **kwargs: Any) -> dict[str, Any]:
        nonlocal called
        called = True
        return {"text": "nope", "provider": "x", "model": "y", "image_count": 1}

    monkeypatch.setattr("amplifier_browser_bridge.vision_read.extract_text", _fake_extract_text)

    result = await vision_read(fake, Target(device_id="d1", tab_id=7))

    assert result == error
    assert called is False


@pytest.mark.asyncio
async def test_vision_read_single_image_composes_capture_and_extraction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import base64

    raw_bytes = b"\xff\xd8\xfake-jpeg-bytes"
    b64 = base64.b64encode(raw_bytes).decode("ascii")
    fake = _FakeHubClient(
        {"ok": True, "result": {"tab_id": 7, "format": "jpeg", "base64": b64, "via": "cdp"}}
    )

    captured_images: list[bytes] = []

    async def _fake_extract_text(images: list[bytes], prompt: str, **kwargs: Any) -> dict[str, Any]:
        captured_images.extend(images)
        return {
            "text": "hello world",
            "provider": "anthropic",
            "model": "claude-3-5-sonnet-latest",
            "image_count": 1,
        }

    monkeypatch.setattr("amplifier_browser_bridge.vision_read.extract_text", _fake_extract_text)

    result = await vision_read(fake, Target(device_id="d1", tab_id=7), prompt="read this")

    assert result["ok"] is True
    assert result["result"]["text"] == "hello world"
    assert result["result"]["vision_provider"] == "anthropic"
    assert result["result"]["image_count"] == 1
    assert captured_images == [raw_bytes]
    # capture_hidden defaults to True for vision_read (unlike raw screenshot)
    _target, command, args = fake.command_calls[0]
    assert command == "screenshot"
    assert args["capture_hidden"] is True


@pytest.mark.asyncio
async def test_vision_read_multi_page_composes_every_page(monkeypatch: pytest.MonkeyPatch) -> None:
    import base64

    pages = [
        {"index": i, "format": "jpeg", "base64": base64.b64encode(f"page-{i}".encode()).decode("ascii")}
        for i in range(3)
    ]
    fake = _FakeHubClient(
        {
            "ok": True,
            "result": {
                "tab_id": 7,
                "format": "jpeg",
                "pages": pages,
                "page_count": 3,
                "capped": False,
                "stopped_reason": "reached end of scrollable content",
                "via": "cdp",
            },
        }
    )

    captured_images: list[bytes] = []

    async def _fake_extract_text(images: list[bytes], prompt: str, **kwargs: Any) -> dict[str, Any]:
        captured_images.extend(images)
        return {
            "text": "all pages",
            "provider": "gemini",
            "model": "gemini-2.5-flash",
            "image_count": len(images),
        }

    monkeypatch.setattr("amplifier_browser_bridge.vision_read.extract_text", _fake_extract_text)

    result = await vision_read(
        fake, Target(device_id="d1", tab_id=7), multi_page=True, max_pages=5, frame_id=862
    )

    assert result["ok"] is True
    assert result["result"]["image_count"] == 3
    assert result["result"]["page_count"] == 3
    assert result["result"]["stopped_reason"] == "reached end of scrollable content"
    assert captured_images == [b"page-0", b"page-1", b"page-2"]
    _, _, args = fake.command_calls[0]
    assert args["multi_page"] is True
    assert args["max_pages"] == 5
    assert args["frame_id"] == 862


@pytest.mark.asyncio
async def test_vision_read_missing_image_data_fails_loud_without_calling_vision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeHubClient({"ok": True, "result": {"tab_id": 7, "format": "jpeg", "data_url_length": 12345}})

    called = False

    async def _fake_extract_text(*args: Any, **kwargs: Any) -> dict[str, Any]:
        nonlocal called
        called = True
        return {"text": "nope", "provider": "x", "model": "y", "image_count": 1}

    monkeypatch.setattr("amplifier_browser_bridge.vision_read.extract_text", _fake_extract_text)

    result = await vision_read(fake, Target(device_id="d1", tab_id=7))

    assert result["ok"] is False
    assert "no image data" in result["error"]
    assert called is False
