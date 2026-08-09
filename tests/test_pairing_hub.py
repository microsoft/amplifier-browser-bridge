"""Hub-level integration tests for the pairing flow: minting a ticket over the
token-authenticated `/agent` route (`create_pairing`), and redeeming it over the
UNauthenticated `POST /pair/redeem` HTTP route -- exercised through a real
aiohttp TestServer/TestClient (matching test_kill_switch.py's/test_doctor.py's
precedent for proving the actual wire protocol, not just the internal handler).

See src/amplifier_browser_bridge/pairing.py's module docstring for the full
entropy/lifetime/threat-model reasoning this flow is built on.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient as AiohttpTestClient
from aiohttp.test_utils import TestServer

from amplifier_browser_bridge.audit import AuditLog
from amplifier_browser_bridge.auth import TokenStore, load_token_store
from amplifier_browser_bridge.hub import Hub


def _hub(tmp_path: Path, *, token_store: TokenStore | None = None, token_file: Path | None = None) -> Hub:
    """Mirrors cli.py's `hub` command's real wiring: when a `token_file` is
    given, the token_store is LOADED from it (not constructed in bare memory)
    -- so a redemption's persist_device_token() read-modify-write sees the
    SAME on-disk `default` a real hub process would have started from."""
    if token_store is None:
        if token_file is not None:
            token_file.write_text(json.dumps({"default": "agent-secret", "devices": {}}), encoding="utf-8")
            token_store = load_token_store(token_file)
        else:
            token_store = TokenStore(default_token="agent-secret")
    return Hub(
        token_store=token_store,
        audit_log=AuditLog(tmp_path / "audit.jsonl"),
        token_file=token_file,
    )


# ---------------------------------------------------------------------------
# Minting (`create_pairing`) -- internal handler, same technique test_kill_switch.py
# uses for its non-wire-level assertions.
# ---------------------------------------------------------------------------


def test_create_pairing_returns_a_formatted_ticket_and_ttl(tmp_path: Path) -> None:
    hub = _hub(tmp_path)
    result = hub._handle_create_pairing({})
    assert result["ok"] is True
    assert "-" in result["ticket"]  # formatted (grouped) form, not raw
    assert result["expires_in"] == 600.0  # DEFAULT_TICKET_TTL_SECONDS
    assert result["persisted"] is False  # no token_file given to this hub


def test_create_pairing_honors_explicit_ttl_seconds(tmp_path: Path) -> None:
    hub = _hub(tmp_path)
    result = hub._handle_create_pairing({"ttl_seconds": 30})
    assert result["ok"] is True
    assert result["expires_in"] == 30.0


def test_create_pairing_rejects_non_numeric_ttl(tmp_path: Path) -> None:
    hub = _hub(tmp_path)
    result = hub._handle_create_pairing({"ttl_seconds": "soon"})
    assert result["ok"] is False
    assert "numeric" in result["error"]


def test_create_pairing_rejects_non_positive_ttl(tmp_path: Path) -> None:
    hub = _hub(tmp_path)
    result = hub._handle_create_pairing({"ttl_seconds": 0})
    assert result["ok"] is False
    assert "positive" in result["error"]


def test_create_pairing_reports_persisted_true_when_a_token_file_is_configured(tmp_path: Path) -> None:
    hub = _hub(tmp_path, token_file=tmp_path / "tokens.json")
    result = hub._handle_create_pairing({})
    assert result["persisted"] is True


# ---------------------------------------------------------------------------
# Redemption (`POST /pair/redeem`) -- real HTTP round trip via aiohttp TestServer.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_redeem_mints_and_persists_a_real_per_device_token(tmp_path: Path) -> None:
    token_file = tmp_path / "tokens.json"
    hub = _hub(tmp_path, token_file=token_file)
    ticket_result = hub._handle_create_pairing({})
    raw_ticket = ticket_result["ticket"]

    async with TestServer(hub.build_app()) as server, AiohttpTestClient(server) as client:
        resp = await client.post(
            "/pair/redeem",
            json={"ticket": raw_ticket, "device_id": "dev-1", "label": "edge-macos", "platform": "MacIntel"},
        )
        assert resp.status == 200
        body = await resp.json()

    assert body["ok"] is True
    assert body["device_id"] == "dev-1"
    assert len(body["token"]) == 32  # secrets.token_hex(16)
    assert body["persisted"] is True

    # The hub's in-memory TokenStore recognizes it immediately (no restart needed)...
    assert hub.token_store.validate(body["token"], device_id="dev-1") is True
    # ...and it survived to disk under the SAME token file, alongside the
    # pre-existing default token (persist_device_token never clobbers siblings).
    reloaded = load_token_store(token_file)
    assert reloaded.device_tokens["dev-1"] == body["token"]
    assert reloaded.default_token == "agent-secret"


@pytest.mark.asyncio
async def test_redeem_is_single_use(tmp_path: Path) -> None:
    hub = _hub(tmp_path)
    raw_ticket = hub._handle_create_pairing({})["ticket"]

    async with TestServer(hub.build_app()) as server, AiohttpTestClient(server) as client:
        first = await client.post("/pair/redeem", json={"ticket": raw_ticket, "device_id": "dev-1"})
        assert (await first.json())["ok"] is True

        second = await client.post("/pair/redeem", json={"ticket": raw_ticket, "device_id": "dev-2"})
        assert second.status == 403
        second_body = await second.json()
        assert second_body["ok"] is False
        assert "unknown or already-used" in second_body["error"]


@pytest.mark.asyncio
async def test_redeem_rejects_an_unknown_ticket(tmp_path: Path) -> None:
    hub = _hub(tmp_path)
    async with TestServer(hub.build_app()) as server, AiohttpTestClient(server) as client:
        resp = await client.post("/pair/redeem", json={"ticket": "NOTREAL123", "device_id": "dev-1"})
        assert resp.status == 403
        body = await resp.json()
        assert body["ok"] is False
        assert "unknown or already-used" in body["error"]


@pytest.mark.asyncio
async def test_redeem_requires_device_id(tmp_path: Path) -> None:
    hub = _hub(tmp_path)
    raw_ticket = hub._handle_create_pairing({})["ticket"]
    async with TestServer(hub.build_app()) as server, AiohttpTestClient(server) as client:
        resp = await client.post("/pair/redeem", json={"ticket": raw_ticket})
        assert resp.status == 400
        body = await resp.json()
        assert body["ok"] is False
        assert "device_id" in body["error"]


@pytest.mark.asyncio
async def test_redeem_rejects_malformed_json_body(tmp_path: Path) -> None:
    hub = _hub(tmp_path)
    async with TestServer(hub.build_app()) as server, AiohttpTestClient(server) as client:
        resp = await client.post(
            "/pair/redeem", data="not json", headers={"Content-Type": "application/json"}
        )
        assert resp.status == 400
        body = await resp.json()
        assert body["ok"] is False


@pytest.mark.asyncio
async def test_redeem_returns_empty_token_when_hub_auth_is_disabled(tmp_path: Path) -> None:
    """No default token, no device tokens -- the hub is running dev-mode with auth
    disabled entirely (auth.py's TokenStore.auth_enabled is False). Pairing still
    "succeeds" (the ticket is real and consumed) but there is nothing to protect,
    so it honestly returns an empty token -- consistent with every OTHER device on
    this hub already being accepted with no token check at all."""
    hub = _hub(tmp_path, token_store=TokenStore())  # no default_token, no device_tokens
    raw_ticket = hub._handle_create_pairing({})["ticket"]
    async with TestServer(hub.build_app()) as server, AiohttpTestClient(server) as client:
        resp = await client.post("/pair/redeem", json={"ticket": raw_ticket, "device_id": "dev-1"})
        assert resp.status == 200
        body = await resp.json()
    assert body["ok"] is True
    assert body["token"] == ""
    assert body["persisted"] is False


@pytest.mark.asyncio
async def test_redeem_does_not_require_the_agent_token_at_all(tmp_path: Path) -> None:
    """The load-bearing asymmetry (pairing.py's module docstring): minting a
    ticket requires the real agent token (create_pairing runs behind the
    token-gated /agent route); REDEEMING one deliberately does not -- no
    `token` field is sent here at all, and the request still succeeds on a
    valid ticket. This is not an oversight; it is the entire point of the
    ticket mechanism (bootstrapping trust for a device that does not yet hold
    any token)."""
    hub = _hub(tmp_path)  # token_store has a real default_token, i.e. auth IS enabled
    raw_ticket = hub._handle_create_pairing({})["ticket"]
    async with TestServer(hub.build_app()) as server, AiohttpTestClient(server) as client:
        resp = await client.post("/pair/redeem", json={"ticket": raw_ticket, "device_id": "dev-1"})
        assert resp.status == 200
        body = await resp.json()
    assert body["ok"] is True
    assert body["token"] != ""


@pytest.mark.asyncio
async def test_pair_status_is_pending_before_redemption(tmp_path: Path) -> None:
    hub = _hub(tmp_path)
    raw_ticket = hub._handle_create_pairing({})["ticket"]
    async with TestServer(hub.build_app()) as server, AiohttpTestClient(server) as client:
        resp = await client.post("/pair/status", json={"ticket": raw_ticket})
        assert resp.status == 200
        body = await resp.json()
    assert body == {"ok": True, "status": "pending"}


@pytest.mark.asyncio
async def test_pair_status_flips_to_redeemed_after_a_real_redemption(tmp_path: Path) -> None:
    """The real bug this closes: the `/setup` page must be able to learn --
    without redeeming the ticket itself -- that ITS OWN code was redeemed by
    the extension's options page (a different tab, possibly a different
    device), so it can flip from a live countdown to "Connected" instead of
    silently continuing to count down a code that has already been used."""
    hub = _hub(tmp_path)
    raw_ticket = hub._handle_create_pairing({})["ticket"]
    async with TestServer(hub.build_app()) as server, AiohttpTestClient(server) as client:
        redeem_resp = await client.post("/pair/redeem", json={"ticket": raw_ticket, "device_id": "dev-1"})
        assert (await redeem_resp.json())["ok"] is True

        status_resp = await client.post("/pair/status", json={"ticket": raw_ticket})
        assert status_resp.status == 200
        body = await status_resp.json()
    assert body == {"ok": True, "status": "redeemed"}


@pytest.mark.asyncio
async def test_pair_status_is_unknown_for_a_ticket_that_never_existed(tmp_path: Path) -> None:
    hub = _hub(tmp_path)
    async with TestServer(hub.build_app()) as server, AiohttpTestClient(server) as client:
        resp = await client.post("/pair/status", json={"ticket": "NOTREAL123"})
        body = await resp.json()
    assert body == {"ok": True, "status": "unknown"}


@pytest.mark.asyncio
async def test_pair_status_requires_a_ticket_field(tmp_path: Path) -> None:
    hub = _hub(tmp_path)
    async with TestServer(hub.build_app()) as server, AiohttpTestClient(server) as client:
        resp = await client.post("/pair/status", json={})
        assert resp.status == 400
        body = await resp.json()
        assert body["ok"] is False
        assert "ticket" in body["error"]


@pytest.mark.asyncio
async def test_pair_status_rejects_malformed_json_body(tmp_path: Path) -> None:
    hub = _hub(tmp_path)
    async with TestServer(hub.build_app()) as server, AiohttpTestClient(server) as client:
        resp = await client.post(
            "/pair/status", data="not json", headers={"Content-Type": "application/json"}
        )
        assert resp.status == 400
        body = await resp.json()
        assert body["ok"] is False


@pytest.mark.asyncio
async def test_pair_status_never_requires_the_agent_token(tmp_path: Path) -> None:
    """Same asymmetry as `/pair/redeem` (see pairing.py's module docstring):
    a status check reveals only the state of a ticket the caller already
    holds the code for, so it is unauthenticated by design."""
    hub = _hub(tmp_path)
    raw_ticket = hub._handle_create_pairing({})["ticket"]
    async with TestServer(hub.build_app()) as server, AiohttpTestClient(server) as client:
        resp = await client.post("/pair/status", json={"ticket": raw_ticket})
        assert resp.status == 200


@pytest.mark.asyncio
async def test_create_pairing_over_the_real_agent_ws_route_requires_the_token(tmp_path: Path) -> None:
    """The MINTING half of the asymmetry, proven over the real wire route (not
    just the internal handler): an agent connection with no/wrong token cannot
    mint a ticket at all -- same token check every other agent message goes
    through (`_handle_agent_ws`)."""
    hub = _hub(tmp_path)
    async with TestServer(hub.build_app()) as server, AiohttpTestClient(server) as client:
        async with client.ws_connect("/agent") as ws:
            await ws.send_json({"v": 1, "id": "r1", "type": "create_pairing"})  # no token field at all
            msg = await ws.receive()
            body = json.loads(msg.data)
            assert body["type"] == "error"
            assert body["error"] == "unauthorized"

        async with client.ws_connect("/agent") as ws:
            await ws.send_json({"v": 1, "id": "r2", "type": "create_pairing", "token": "agent-secret"})
            msg = await ws.receive()
            body = json.loads(msg.data)
            assert body["type"] == "result"
            assert body["ok"] is True
            assert "ticket" in body
