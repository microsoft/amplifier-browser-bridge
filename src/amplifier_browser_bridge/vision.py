"""Vision-based text extraction: calling an external vision-capable LLM over
captured pixels. This is an EXPLICIT, separately-named mechanism a caller opts
into -- see docs/designs/browser-bridge.md's "Mechanism, not policy" section
(§13) and docs/PROTOCOL.md's "Vision-based extraction" section.

## Why this lives here, not in the hub or extension

The hub/extension's job (design doc §3) is MECHANISM: reliable pixel capture,
capability-scoped, policy/denylist-enforced identically to every other
command, with zero knowledge of what happens to the bytes afterward. Calling
an external vision model is a POLICY decision -- which model, whether to spend
the money/latency at all -- that only the CALLER can make correctly. Baking a
model call into `screenshot` itself (e.g. "if the result looks thin, try
vision") would be exactly the kind of silent, automatic escalation §13
forbids -- the same mistake `read`'s original frame-ranking logic made.

Keeping the model call in this separate module, invoked only by an explicitly
different command (`vision_read` -- see `vision_read.py`), means:

  - the hub and extension never import an LLM SDK or hold a model API key;
  - `screenshot` (pixels) is fully useful with zero vision model configured;
  - the two mechanisms -- "return pixels" and "return text extracted from
    pixels" -- stay distinct, separately-invokable commands, never one
    silently substituting for the other (docs/designs/browser-bridge.md §13's
    closing test: "keep them as distinct, separately-invokable commands
    rather than folding one into the other's internals").

## Provider configuration

No project-specific model/provider convention exists in this repo (a
standalone OSS project, not bound to any single agent framework's provider
config) -- this follows the same env-var-configured-provider pattern
documented in Amplifier's `image-vision` skill: try providers in a fixed
priority order (fastest/cheapest first), first one with an API key present
wins. `ABB_VISION_PROVIDER` pins a specific provider and skips the
auto-detect order. `ABB_VISION_MODEL` overrides the default model for
whichever provider is selected.

Fails loud, naming exactly which environment variable(s) would satisfy it, if
no provider is configured -- never silently returns empty text (design doc
§8: "Fail loud. Every command ... no silent fallbacks, no synthetic results").
"""

from __future__ import annotations

import base64
import os
from dataclasses import dataclass
from typing import Any

import aiohttp

# (env var, provider name, default model) -- checked in this order. Mirrors the
# image-vision skill's robust-fallback priority (fastest/cheapest first), since
# that is the only existing project convention for this kind of choice.
_PROVIDER_ORDER: tuple[tuple[str, str, str], ...] = (
    ("GOOGLE_API_KEY", "gemini", "gemini-2.5-flash"),
    ("ANTHROPIC_API_KEY", "anthropic", "claude-3-5-sonnet-latest"),
    ("OPENAI_API_KEY", "openai", "gpt-4o"),
)

_ENV_VAR_BY_PROVIDER: dict[str, str] = {provider: env_var for env_var, provider, _ in _PROVIDER_ORDER}
_DEFAULT_MODEL_BY_PROVIDER: dict[str, str] = {provider: model for _, provider, model in _PROVIDER_ORDER}

# Conservative payload bound: refuse rather than send an oversized request to
# the provider (a real API-side rejection is a worse failure mode than an
# actionable local error naming the limit). Sized generously for several
# full-quality JPEG screenshots.
MAX_TOTAL_IMAGE_BYTES = 20 * 1024 * 1024


class VisionConfigError(RuntimeError):
    """No vision provider is configured -- see this module's docstring. Always
    names exactly which environment variable(s) would resolve it."""


class VisionError(RuntimeError):
    """The provider was configured but the API call itself failed (network
    error, non-2xx response, or an unrecognized response shape)."""


@dataclass(frozen=True)
class VisionConfig:
    provider: str
    api_key: str
    model: str


def resolve_provider() -> VisionConfig:
    """Pick a vision provider from the environment. Raises `VisionConfigError`
    naming exactly what's missing if none is usable -- never returns a
    half-configured result."""
    override = os.environ.get("ABB_VISION_PROVIDER")
    model_override = os.environ.get("ABB_VISION_MODEL")

    if override:
        override = override.strip().lower()
        if override not in _ENV_VAR_BY_PROVIDER:
            raise VisionConfigError(
                f"ABB_VISION_PROVIDER={override!r} is not a recognized provider. "
                f"Valid values: {sorted(_ENV_VAR_BY_PROVIDER)}."
            )
        env_var = _ENV_VAR_BY_PROVIDER[override]
        api_key = os.environ.get(env_var)
        if not api_key:
            raise VisionConfigError(
                f"ABB_VISION_PROVIDER={override!r} was requested, but {env_var} is not set. "
                f"Set {env_var} to a valid API key, or unset ABB_VISION_PROVIDER to auto-detect "
                "another configured provider."
            )
        return VisionConfig(
            provider=override, api_key=api_key, model=model_override or _DEFAULT_MODEL_BY_PROVIDER[override]
        )

    for env_var, provider, default_model in _PROVIDER_ORDER:
        api_key = os.environ.get(env_var)
        if api_key:
            return VisionConfig(provider=provider, api_key=api_key, model=model_override or default_model)

    checked = ", ".join(env_var for env_var, _, _ in _PROVIDER_ORDER)
    raise VisionConfigError(
        "No vision provider is configured -- vision-based extraction requires an API key for one "
        f"of: {checked}. Set one of those environment variables (or ABB_VISION_PROVIDER + its "
        "matching key, to pin a specific provider) and retry. This is a distinct, opt-in mechanism "
        "(see docs/PROTOCOL.md's 'Vision-based extraction' section) -- screenshot/fetch_bytes/"
        "grab_image/read/snapshot all work with no vision provider configured at all."
    )


def _check_total_size(images: list[bytes]) -> None:
    total = sum(len(img) for img in images)
    if total > MAX_TOTAL_IMAGE_BYTES:
        raise VisionError(
            f"vision_read refused: {len(images)} image(s) total {total} bytes, exceeding the "
            f"{MAX_TOTAL_IMAGE_BYTES}-byte cap for a single vision-model call. Narrow the capture "
            "(fewer pages via args.max_pages, or a specific args.frame_id) rather than sending "
            "everything at once."
        )


async def extract_text(
    images: list[bytes], prompt: str, *, config: VisionConfig | None = None, media_type: str = "image/jpeg"
) -> dict[str, Any]:
    """Call a vision-capable LLM over one or more images and return extracted text.

    `images` is a list of raw image bytes (JPEG by default) -- multiple images are
    sent as a single multi-image message (e.g. one call per page of a multi-page
    capture), so the model can reason across all of them together (page ordering,
    continuation, etc.) rather than requiring the caller to stitch per-page answers.

    Returns `{"text": ..., "provider": ..., "model": ..., "image_count": ...}` on
    success. Raises `VisionConfigError` if no provider is configured, or
    `VisionError` if the configured provider's API call itself fails.
    """
    if not images:
        raise VisionError("extract_text requires at least one image")
    _check_total_size(images)
    cfg = config or resolve_provider()

    if cfg.provider == "anthropic":
        text = await _call_anthropic(cfg, images, prompt, media_type)
    elif cfg.provider == "openai":
        text = await _call_openai(cfg, images, prompt, media_type)
    elif cfg.provider == "gemini":
        text = await _call_gemini(cfg, images, prompt, media_type)
    else:  # pragma: no cover -- resolve_provider() only ever returns a known provider
        raise VisionConfigError(f"unrecognized provider: {cfg.provider!r}")

    return {"text": text, "provider": cfg.provider, "model": cfg.model, "image_count": len(images)}


async def _call_anthropic(cfg: VisionConfig, images: list[bytes], prompt: str, media_type: str) -> str:
    content: list[dict[str, Any]] = [
        {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": media_type,
                "data": base64.b64encode(img).decode("ascii"),
            },
        }
        for img in images
    ]
    content.append({"type": "text", "text": prompt})
    body = {
        "model": cfg.model,
        "max_tokens": 4096,
        "messages": [{"role": "user", "content": content}],
    }
    headers = {
        "x-api-key": cfg.api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    async with (
        aiohttp.ClientSession() as session,
        session.post(
            "https://api.anthropic.com/v1/messages",
            json=body,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=90),
        ) as resp,
    ):
        data = await resp.json()
        if resp.status >= 400:
            raise VisionError(f"Anthropic API returned HTTP {resp.status}: {data}")
    blocks = data.get("content") or []
    texts = [b.get("text", "") for b in blocks if isinstance(b, dict) and b.get("type") == "text"]
    if not texts:
        raise VisionError(f"Anthropic API response had no text content: {data}")
    return "\n".join(texts)


async def _call_openai(cfg: VisionConfig, images: list[bytes], prompt: str, media_type: str) -> str:
    content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
    for img in images:
        b64 = base64.b64encode(img).decode("ascii")
        content.append({"type": "image_url", "image_url": {"url": f"data:{media_type};base64,{b64}"}})
    body = {"model": cfg.model, "max_tokens": 4096, "messages": [{"role": "user", "content": content}]}
    headers = {"authorization": f"Bearer {cfg.api_key}", "content-type": "application/json"}
    base_url = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")
    async with (
        aiohttp.ClientSession() as session,
        session.post(
            f"{base_url.rstrip('/')}/chat/completions",
            json=body,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=90),
        ) as resp,
    ):
        data = await resp.json()
        if resp.status >= 400:
            raise VisionError(f"OpenAI API returned HTTP {resp.status}: {data}")
    choices = data.get("choices") or []
    if not choices:
        raise VisionError(f"OpenAI API response had no choices: {data}")
    message = choices[0].get("message") or {}
    text = message.get("content")
    if not text:
        raise VisionError(f"OpenAI API response had no message content: {data}")
    return str(text)


async def _call_gemini(cfg: VisionConfig, images: list[bytes], prompt: str, media_type: str) -> str:
    parts: list[dict[str, Any]] = [{"text": prompt}]
    for img in images:
        parts.append(
            {"inline_data": {"mime_type": media_type, "data": base64.b64encode(img).decode("ascii")}}
        )
    body = {"contents": [{"parts": parts}]}
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{cfg.model}:generateContent?key={cfg.api_key}"
    async with (
        aiohttp.ClientSession() as session,
        session.post(
            url,
            json=body,
            headers={"content-type": "application/json"},
            timeout=aiohttp.ClientTimeout(total=90),
        ) as resp,
    ):
        data = await resp.json()
        if resp.status >= 400:
            raise VisionError(f"Gemini API returned HTTP {resp.status}: {data}")
    candidates = data.get("candidates") or []
    if not candidates:
        raise VisionError(f"Gemini API response had no candidates: {data}")
    parts_out = (candidates[0].get("content") or {}).get("parts") or []
    texts = [p.get("text", "") for p in parts_out if isinstance(p, dict) and "text" in p]
    if not texts:
        raise VisionError(f"Gemini API response had no text parts: {data}")
    return "\n".join(texts)
