"""Integration tests for the hub's onboarding routes (GET /setup,
GET /setup/android, GET /setup/extension.zip, GET /setup/android-extension.bin)
-- exercised over a real aiohttp TestServer/TestClient, matching
test_kill_switch.py's precedent for proving a route is reachable over the
actual HTTP surface, not just callable as an internal method.

These routes are deliberately NOT token-gated (see hub.py's "Onboarding"
section) -- every request here is made with no Authorization header at all,
proving the routes work for the unauthenticated caller they exist for.

On-demand Android packing (android_pack.py) is exercised here against a FAKE
packer -- CI has no real Chromium/Chrome/Edge binary (see
.github/workflows/ci.yml and test_android_pack.py's module docstring for the
same reasoning applied there).
"""

from __future__ import annotations

import io
import json
import stat
import struct
import textwrap
import zipfile
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient as AiohttpTestClient
from aiohttp.test_utils import TestServer

from amplifier_browser_bridge.audit import AuditLog
from amplifier_browser_bridge.auth import TokenStore
from amplifier_browser_bridge.hub import Hub


def _hub(
    tmp_path: Path, *, android_artifact: Path | None = None, token_store: TokenStore | None = None
) -> Hub:
    return Hub(
        token_store=token_store if token_store is not None else TokenStore(),
        audit_log=AuditLog(tmp_path / "audit.jsonl"),
        android_artifact=android_artifact,
    )


_FAKE_PACKER_SCRIPT = textwrap.dedent(
    """\
    #!/usr/bin/env python3
    import io, sys, zipfile, struct
    from pathlib import Path
    stage_dir = None
    for arg in sys.argv[1:]:
        if arg.startswith("--pack-extension="):
            stage_dir = Path(arg.split("=", 1)[1])
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for p in sorted(stage_dir.rglob("*")):
            if p.is_file():
                zf.write(p, arcname=str(p.relative_to(stage_dir)))
    crx_path = stage_dir.parent / (stage_dir.name + ".crx")
    with open(crx_path, "wb") as f:
        f.write(b"Cr24")
        f.write(struct.pack("<I", 3))
        f.write(struct.pack("<I", 0))
        f.write(buf.getvalue())
    pem_path = stage_dir.parent / (stage_dir.name + ".pem")
    if not pem_path.exists():
        pem_path.write_bytes(b"fake-pem")
    sys.exit(0)
    """
)


@pytest.fixture
def fake_packer(tmp_path: Path) -> Path:
    script = tmp_path / "fake-chrome.py"
    script.write_text(_FAKE_PACKER_SCRIPT, encoding="utf-8")
    wrapper = tmp_path / "fake-chrome"
    wrapper.write_text(f"#!/usr/bin/env bash\nexec python3 '{script}' \"$@\"\n", encoding="utf-8")
    wrapper.chmod(wrapper.stat().st_mode | stat.S_IEXEC)
    return wrapper


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
async def test_setup_page_links_to_android_page_regardless_of_platform(tmp_path: Path) -> None:
    hub = _hub(tmp_path)
    server = TestServer(hub.build_app())
    client = AiohttpTestClient(server)
    await client.start_server()
    try:
        resp = await client.get("/setup", headers={"User-Agent": "Mozilla/5.0 (Linux; Android 14; Pixel 8)"})
        body = await resp.text()
        assert 'href="/setup/android"' in body
        assert "Android needs different steps" in body
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


# --- /setup/android (the standalone page) ---------------------------------------


@pytest.mark.asyncio
async def test_setup_android_page_is_reachable_unauthenticated(tmp_path: Path) -> None:
    hub = _hub(tmp_path)
    server = TestServer(hub.build_app())
    client = AiohttpTestClient(server)
    await client.start_server()
    try:
        resp = await client.get("/setup/android")
        assert resp.status == 200
        assert resp.content_type == "text/html"
        body = await resp.text()
        assert "Android (experimental)" in body
        assert 'href="/setup"' in body
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_setup_android_page_shows_unavailable_reason_with_no_packer_and_no_static_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CHROME_BIN", raising=False)
    monkeypatch.setattr("amplifier_browser_bridge.hub.find_packer_binary", lambda: None)
    monkeypatch.setattr("amplifier_browser_bridge.android_pack.find_packer_binary", lambda: None)
    hub = _hub(tmp_path)
    server = TestServer(hub.build_app())
    client = AiohttpTestClient(server)
    await client.start_server()
    try:
        resp = await client.get("/setup/android")
        body = await resp.text()
        assert "No build available on this hub yet" in body
        assert "no Chromium/Chrome/Edge binary was found" in body
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_setup_android_page_shows_download_available_with_a_static_artifact(tmp_path: Path) -> None:
    artifact = tmp_path / "art.crx"
    artifact.write_bytes(b"Cr24-fake")
    hub = _hub(tmp_path, android_artifact=artifact)
    server = TestServer(hub.build_app())
    client = AiohttpTestClient(server)
    await client.start_server()
    try:
        resp = await client.get("/setup/android")
        body = await resp.text()
        assert 'href="/setup/android-extension.bin"' in body
        assert "No build available on this hub yet" not in body
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_setup_android_page_shows_download_available_when_a_packer_is_found(
    tmp_path: Path, fake_packer: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("amplifier_browser_bridge.hub.find_packer_binary", lambda: fake_packer)
    hub = _hub(tmp_path)
    server = TestServer(hub.build_app())
    client = AiohttpTestClient(server)
    await client.start_server()
    try:
        resp = await client.get("/setup/android")
        body = await resp.text()
        assert 'href="/setup/android-extension.bin"' in body
    finally:
        await client.close()


# --- /setup/android-extension.bin: static artifact path (unchanged) -------------


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


# --- /setup/android-extension.bin: on-demand packing path (new) -----------------


@pytest.mark.asyncio
async def test_setup_android_artifact_404_becomes_503_with_honest_reason_when_no_packer_available(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The core honesty requirement: when neither a static artifact NOR a
    packer is available, this must never look like it's about to work and
    then fail silently -- a clear, actionable 503, naming exactly what's
    missing."""
    monkeypatch.setattr("amplifier_browser_bridge.hub.find_packer_binary", lambda: None)
    monkeypatch.setattr("amplifier_browser_bridge.android_pack.find_packer_binary", lambda: None)
    hub = _hub(tmp_path)
    server = TestServer(hub.build_app())
    client = AiohttpTestClient(server)
    await client.start_server()
    try:
        resp = await client.get("/setup/android-extension.bin")
        assert resp.status == 503
        body = await resp.text()
        assert "no Chromium/Chrome/Edge binary found" in body
        assert "CHROME_BIN" in body
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_setup_android_artifact_packs_on_demand_from_the_hubs_own_live_token(
    tmp_path: Path, fake_packer: Path
) -> None:
    """The whole point of android_pack.py: no operator step, no
    --android-artifact -- the hub bakes its OWN currently-running token into
    a real artifact the moment it's asked for one."""
    token_store = TokenStore(default_token="live-hub-token")
    hub = _hub(tmp_path, token_store=token_store)
    server = TestServer(hub.build_app())
    client = AiohttpTestClient(server)
    await client.start_server()
    try:
        import amplifier_browser_bridge.hub as hub_module

        original_build = hub_module.build_android_crx

        async def build_with_fake_chrome(**kwargs):
            kwargs["chrome_bin"] = fake_packer
            kwargs["signing_key_path"] = tmp_path / "signing-key.pem"
            return await original_build(**kwargs)

        hub_module.build_android_crx = build_with_fake_chrome
        try:
            resp = await client.get("/setup/android-extension.bin")
            assert resp.status == 200
            assert resp.content_type == "application/octet-stream"
            data = await resp.read()
            assert data[:4] == b"Cr24"

            header_length = struct.unpack("<I", data[8:12])[0]
            zip_bytes = data[12 + header_length :]
            with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
                config = json.loads(zf.read("bundled_config.json").decode("utf-8"))
            assert config["hubToken"] == "live-hub-token"
        finally:
            hub_module.build_android_crx = original_build
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_setup_android_artifact_prefers_static_artifact_over_on_demand_packing(
    tmp_path: Path, fake_packer: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A configured static artifact always wins -- on-demand packing is a
    fallback for when nobody configured one, never a second competing path."""
    monkeypatch.setattr("amplifier_browser_bridge.hub.find_packer_binary", lambda: fake_packer)
    artifact = tmp_path / "static.crx"
    artifact.write_bytes(b"STATIC-BYTES")
    hub = _hub(tmp_path, android_artifact=artifact)
    server = TestServer(hub.build_app())
    client = AiohttpTestClient(server)
    await client.start_server()
    try:
        resp = await client.get("/setup/android-extension.bin")
        data = await resp.read()
        assert data == b"STATIC-BYTES"
    finally:
        await client.close()
