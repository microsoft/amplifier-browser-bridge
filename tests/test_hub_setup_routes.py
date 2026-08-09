"""Integration tests for the hub's onboarding routes (GET /setup,
GET /setup/extension.zip, GET /setup/android-extension.bin) -- exercised over
a real aiohttp TestServer/TestClient, matching test_kill_switch.py's
precedent for proving a route is reachable over the actual HTTP surface, not
just callable as an internal method.

These routes are deliberately NOT token-gated (see hub.py's "Onboarding"
section) -- every request here is made with no Authorization header at all,
proving the routes work for the unauthenticated caller they exist for.
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient as AiohttpTestClient
from aiohttp.test_utils import TestServer

from amplifier_browser_bridge.audit import AuditLog
from amplifier_browser_bridge.auth import TokenStore
from amplifier_browser_bridge.hub import Hub


def _hub(tmp_path: Path, *, android_artifact: Path | None = None) -> Hub:
    return Hub(
        token_store=TokenStore(),
        audit_log=AuditLog(tmp_path / "audit.jsonl"),
        android_artifact=android_artifact,
    )


@pytest.mark.asyncio
async def test_setup_page_is_reachable_unauthenticated(tmp_path: Path) -> None:
    hub = _hub(tmp_path)
    server = TestServer(hub.build_app())
    client = AiohttpTestClient(server)
    await client.start_server()
    try:
        resp = await client.get("/setup")
        assert resp.status == 200
        assert resp.content_type == "text/html"
        body = await resp.text()
        assert "Amplifier Browser Bridge" in body
        assert "/setup/extension.zip" in body
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_setup_page_detects_android_user_agent(tmp_path: Path) -> None:
    hub = _hub(tmp_path)
    server = TestServer(hub.build_app())
    client = AiohttpTestClient(server)
    await client.start_server()
    try:
        resp = await client.get("/setup", headers={"User-Agent": "Mozilla/5.0 (Linux; Android 14; Pixel 8)"})
        body = await resp.text()
        assert '<details data-platform="android" open>' in body
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_setup_extension_zip_downloads_a_real_valid_zip(tmp_path: Path) -> None:
    hub = _hub(tmp_path)
    server = TestServer(hub.build_app())
    client = AiohttpTestClient(server)
    await client.start_server()
    try:
        resp = await client.get("/setup/extension.zip")
        assert resp.status == 200
        assert resp.content_type == "application/zip"
        assert "attachment" in resp.headers.get("Content-Disposition", "")
        data = await resp.read()
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            assert zf.testzip() is None  # no corrupt member
            assert "manifest.json" in zf.namelist()
            assert "icons/icon-128.png" in zf.namelist()
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_setup_android_artifact_404s_when_not_configured(tmp_path: Path) -> None:
    hub = _hub(tmp_path)
    server = TestServer(hub.build_app())
    client = AiohttpTestClient(server)
    await client.start_server()
    try:
        resp = await client.get("/setup/android-extension.bin")
        assert resp.status == 404
        body = await resp.text()
        assert "android-artifact" in body
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_setup_android_artifact_serves_configured_file_as_octet_stream(tmp_path: Path) -> None:
    artifact = tmp_path / "amplifier-browser-bridge-android-v0.1.0.crx"
    artifact.write_bytes(b"Cr24-fake-bytes-for-test")
    hub = _hub(tmp_path, android_artifact=artifact)
    server = TestServer(hub.build_app())
    client = AiohttpTestClient(server)
    await client.start_server()
    try:
        resp = await client.get("/setup/android-extension.bin")
        assert resp.status == 200
        assert resp.content_type == "application/octet-stream"
        assert ".bin" in resp.headers.get("Content-Disposition", "")
        data = await resp.read()
        assert data == artifact.read_bytes()
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_setup_page_shows_download_link_only_when_android_artifact_configured(
    tmp_path: Path,
) -> None:
    artifact = tmp_path / "art.crx"
    artifact.write_bytes(b"x")
    hub = _hub(tmp_path, android_artifact=artifact)
    server = TestServer(hub.build_app())
    client = AiohttpTestClient(server)
    await client.start_server()
    try:
        resp = await client.get("/setup", headers={"User-Agent": "Mozilla/5.0 (Linux; Android 14)"})
        body = await resp.text()
        assert 'href="/setup/android-extension.bin"' in body
    finally:
        await client.close()
