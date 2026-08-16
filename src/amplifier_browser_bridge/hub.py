"""The hub: device registry, per-device command queue, routing/correlation, audit log.

Runs on the agent host, separate from any single agent process, for three reasons
(design doc §3.2), all load-bearing:

    1. Multiple agents can be talking to the same devices at once.
    2. The queue must outlive any one agent process -- mobile devices are reachable
       in windows, not continuously; a command issued now may execute in 90 seconds.
    3. Policy must live outside the model's reach -- see policy.py, and the note
       below on `send_command` as the single choke point.

Two WebSocket routes:

    /device -- extensions dial OUT to this. Device protocol (hello/heartbeat/result/event).
    /agent  -- CLI/lib clients connect to this, one request per short-lived connection
               (though the loop supports multiple requests per connection too).
               Agent protocol (list_devices/command/poll/confirm).

See docs/PROTOCOL.md for the full message catalogue and docs/POLICY.md for the
policy engine's denylist/gate/kill-switch model.

## `send_command` is the single choke point for policy

`QueuedCommand` (queue.py) is constructed in exactly one place in this codebase:
inside `send_command`, and only *after* `PolicyEngine.evaluate` has returned an
"allow" decision. There is no other path to `_dispatch_live` or to a device's
`DeviceCommandQueue.enqueue` -- `_drain_queue` only ever redelivers commands that
already passed through `send_command` once, at enqueue time. A command cannot
reach a device without a policy decision having been made first; this is what
makes the capability-binding guarantee in design doc §6.2 structural rather than
a convention future contributors could accidentally route around.

## `_dispatch_live` is the single choke point for CDP escalation (Phase 4)

Symmetrically: `_dispatch_live` is the only place a command's wire envelope is
actually constructed and sent to a *live* device (`_send_and_await` does the
raw send-and-await; `_dispatch_live` wraps it). Every command reaches a device
through here -- both the immediate-dispatch path (`send_command`, when the
device is already `Tier.LIVE`) and the drained-later path (`_drain_queue`,
once a queued device reconnects) call `_dispatch_live`, never `_send_and_await`
directly. This is where `cdp.requires_cdp` is checked and, if the caller's
`args` genuinely need it (trusted input, hidden-tab capture -- see cdp.py),
CDP is auto-attached (never speculatively) before the real command is sent
with a hub-asserted `_cdp` flag the device honors. See `_ensure_cdp_attached`
and docs/PROTOCOL.md's CDP section.
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import json
import logging
import math
import signal
import uuid
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from aiohttp import ContentTypeError, WSMsgType, web

from .addressing import Target, TargetError
from .android_pack import AndroidPackError, PackerUnavailableError, build_android_crx, find_packer_binary
from .args_bool import truthy
from .audit import AuditLog
from .auth import TokenStore, persist_device_token
from .build_stamp import BuildStampError, current_build_stamp
from .build_stamp import compute_freshness as _freshness_compute
from .cdp import DEFAULT_SOFT_DETACH_IDLE_SECONDS, CdpRegistry, requires_cdp
from .effects import EffectsReport
from .extension_integrity import ExtensionIntegrityError
from .extension_zip import build_extension_zip_bytes
from .hub_location import DEFAULT_PORT
from .onboarding import detect_platform, render_android_setup_page, render_setup_page
from .pairing import DEFAULT_TICKET_TTL_SECONDS, PairingError, PairingStore, format_ticket
from .policy import STATE_CHANGING_COMMANDS, PolicyEngine, PolicyError
from .protocol import COMMANDS, HUB_ONLY_ARGS, PROTOCOL_VERSION, new_id
from .queue import QueuedCommand
from .registry import DeviceConnection, DeviceRecord, DeviceRegistry
from .scope import SCOPE_FIELDS, ScopeError, SessionScope
from .setup import ExtensionSourceNotFoundError
from .setup import generate_token as generate_device_token
from .skew import capability_error as _skew_capability_error
from .skew import compute_skew as _skew_compute
from .tiers import LIVE_SILENCE_TIMEOUT_SECONDS, Tier

logger = logging.getLogger("amplifier_browser_bridge.hub")

# DEFAULT_PORT re-exported from hub_location.py (its canonical home now -- see
# that module's docstring) so `from .hub import DEFAULT_PORT` keeps working
# unchanged for every existing importer (cli.py, __init__.py).
# Real-world finding (real Edge profile, 532 open tabs): `read` on a heavy SPA
# (repos.opensource.microsoft.com's Open Source Management Portal) timed out
# at the prior default of 30.0s even though the tab was awake and
# `status: "complete"` -- injection + shadow-DOM-piercing traversal on a large
# hydrated SPA genuinely needs more than 30s sometimes. 120s is a generous
# default for real-world pages while still bounding the worst case; a caller
# with an even heavier page can raise it further per-command via
# `args.timeout_s` (see protocol.py's HUB_ONLY_ARGS) up to MAX_COMMAND_TIMEOUT,
# or the hub operator can change the default via `amplifier-browser-bridge hub --command-timeout`.
DEFAULT_COMMAND_TIMEOUT = 120.0
# Accepted range for a caller-supplied `args.timeout_s` override (see
# `Hub._extract_timeout_override`). Floor prevents a mistaken `0`/negative
# value from producing an unusable hair-trigger timeout; ceiling keeps a
# single command from being able to hang a caller indefinitely.
MIN_COMMAND_TIMEOUT = 1.0
MAX_COMMAND_TIMEOUT = 600.0
DEFAULT_SOFT_DETACH_SWEEP_INTERVAL_SECONDS = 30.0

# Keepalive sweep cadence (docs/PROTOCOL.md's `ping` entry, docs/designs/
# browser-bridge.md \u00a74: "hub pings every 20s, extension heartbeats every
# 15s"). Deliberately app-level, not aiohttp's transport-level `heartbeat`
# param (see `_handle_device_ws`'s `WebSocketResponse(heartbeat=None)`) --
# `extension/background.js`'s `onMessage` already replies to a `{"type":
# "ping"}` frame with a fresh `heartbeat` (same code path as its own 15s
# timer), shipped to every real connected device (desktop + Android) before
# this hub-side sweep existed. Re-enabling aiohttp's transport-level ping
# instead would (a) require zero extension changes too, but (b) introduce a
# SECOND, independent staleness clock -- aiohttp's own internal ping/pong
# bookkeeping -- that never updates `DeviceRecord.last_seen`, so tier
# inference (`tiers.py`'s `compute_tier`) and dead-socket detection would be
# answering the same question ("is this connection alive?") from two
# uncorrelated sources of truth. Staying app-level keeps ONE number
# (`last_seen`) driving both.
DEFAULT_KEEPALIVE_INTERVAL_SECONDS = 20.0

# Commands whose successful device result carries a `url` field directly usable
# to update the policy engine's tab-host cache (see policy.py's "Observation
# intake" section). `tabs` is handled separately since its result is a *list* of
# per-tab entries rather than one url -- see `_ingest_result` below.
_URL_BEARING_RESULT_COMMANDS = frozenset({"navigate", "snapshot", "read"})

# Commands whose successful result hands PAGE CONTENT back to the caller --
# the seal-on-first-read trigger for SessionScope (design doc section 11.2:
# "Hub calls seal() the first time a session receives page content
# (read/snapshot/vision_read/tabs result)"). Deliberately narrower than
# `_URL_BEARING_RESULT_COMMANDS`: `navigate` only reports the URL it landed
# on, never the page's own content, so it does not seal a session by itself.
# `vision_read` is agent-surface-only (not a wire command -- see protocol.py)
# and is not yet wired to this; see this PR's report for that open item.
_PAGE_CONTENT_RESULT_COMMANDS = frozenset({"read", "snapshot", "tabs"})

# A5 fix (security review finding): session scope (scope.py) is the ONLY
# page-immune protection this design has -- design doc section 4's lemma --
# and it is opt-in: a caller that omits `session_id` gets the pre-existing,
# fully-permissive default (design doc section 8, "Migration") with NO
# indication in the response that it ran unscoped. Rather than narrow that
# default (the maintainer's own stated stance is broad access by default --
# see docs/POLICY.md section 1), this makes the state VISIBLE: every
# STATE_CHANGING_COMMANDS result reached without a session_id carries this
# text in a `scope_warning` field, the same way `classification` is attached
# to every such result whether or not it gated. See docs/PROTOCOL.md's
# "Sessions" section, which states this choice plainly.
SCOPE_UNSCOPED_WARNING = (
    "no session_id supplied -- this command ran under the pre-existing, fully-permissive "
    "implicit write scope (no page-immune write restriction was enforced). Pass a session_id "
    "from session-establish/establish_session to enforce a narrower write scope -- see "
    "docs/PROTOCOL.md's 'Sessions' section."
)


class HubBindError(RuntimeError):
    """Raised by `serve_hub` when the hub cannot bind its listening socket -- e.g.
    the port is already in use. `cli.py` catches this and reports it via
    `click.ClickException` (a clean message, no raw traceback) -- never a bare
    `OSError` escaping to the user."""


async def serve_hub(
    app: web.Application, host: str, port: int, *, on_bound: Callable[[], None] | None = None
) -> None:
    """Bind `app` to (host, port) and serve until SIGINT/SIGTERM, calling `on_bound`
    (if given) only once the bind has actually succeeded.

    This is deliberately not `aiohttp.web.run_app` -- that helper hides the bind
    inside its own call, which is exactly what let the old `cli.py` print a
    "listening" banner and THEN fail with an unhandled `OSError` when the port was
    already taken. Binding explicitly, first, via `AppRunner`/`TCPSite` means a
    caller can announce success only once it is actually true -- this project's
    fail-loud convention (CONTRIBUTING.md), applied to hub startup itself.

    Raises `HubBindError` (never a raw `OSError`) if the bind fails, with a message
    that names the port and, for the common case (another hub already running),
    suggests how to check for it.
    """
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    try:
        await site.start()
    except OSError as e:
        await runner.cleanup()
        if e.errno == errno.EADDRINUSE:
            raise HubBindError(
                f"port {port} on {host} is already in use -- a hub may already be running there. "
                f"Check with: ss -ltnp | grep {port} (or lsof -i :{port}). Either stop that hub, "
                "or start this one on a different --port."
            ) from e
        raise HubBindError(f"could not bind {host}:{port}: {e}") from e

    if on_bound is not None:
        on_bound()

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):
            pass  # signal handlers unsupported on this platform/thread -- best effort
    try:
        await stop.wait()
    finally:
        await runner.cleanup()


class _DeviceAuthError(Exception):
    """Raised internally by `_handle_device_message` on a bad `hello` token.
    Signals the caller (`_handle_device_ws`) to close the connection -- kept
    as an exception rather than a sentinel return value so the "keep looping"
    vs. "stop, close the socket" distinction can't be silently lost at a call
    site."""


class Hub:
    """Owns all hub-side state. `build_app()` returns an aiohttp Application; nothing
    here depends on how it's actually served (test code can drive the Hub's methods
    directly without ever starting a real HTTP server)."""

    def __init__(
        self,
        token_store: TokenStore,
        audit_log: AuditLog,
        command_timeout: float = DEFAULT_COMMAND_TIMEOUT,
        policy: PolicyEngine | None = None,
        cdp_idle_seconds: float = DEFAULT_SOFT_DETACH_IDLE_SECONDS,
        soft_detach_sweep_interval: float = DEFAULT_SOFT_DETACH_SWEEP_INTERVAL_SECONDS,
        keepalive_interval: float = DEFAULT_KEEPALIVE_INTERVAL_SECONDS,
        token_file: Any = None,
        android_artifact: Path | None = None,
    ) -> None:
        self.registry = DeviceRegistry()
        self.token_store = token_store
        self.audit = audit_log
        # Pairing (see pairing.py's module docstring): in-memory-only ticket store,
        # fresh on every hub process start -- a restart invalidates every
        # outstanding ticket unconditionally, by construction. `token_file`, if
        # given, is where a successful redemption PERSISTS its freshly-minted
        # per-device token (see `_handle_pair_redeem`) so it survives a restart --
        # the same file `token_store` was originally loaded from (cli.py's `hub`
        # command passes it through for exactly this reason). `None` (e.g. in
        # tests, or a hub whose token_store came from AMPLIFIER_BROWSER_BRIDGE_HUB_TOKEN
        # alone with no file) means a minted device token lives only in memory
        # for this process's lifetime -- pairing still works, it just doesn't
        # survive a restart, which is honestly reported in that response.
        self.pairing = PairingStore()
        self.token_file = token_file
        self.command_timeout = command_timeout
        self.policy = policy if policy is not None else PolicyEngine(audit_log)
        # Per-(device, tab) CDP attach bookkeeping (Phase 4, design doc §7) --
        # see cdp.py and this module's "single choke point for CDP escalation"
        # docstring section above.
        self.cdp = CdpRegistry(idle_seconds=cdp_idle_seconds)
        self.soft_detach_sweep_interval = soft_detach_sweep_interval
        self._soft_detach_task: asyncio.Task[None] | None = None
        # Keepalive sweep (see DEFAULT_KEEPALIVE_INTERVAL_SECONDS above and
        # `keepalive_sweep`/`keepalive_loop` below) -- pings every connected
        # device on this cadence and proactively closes any that have gone
        # silent past LIVE_SILENCE_TIMEOUT_SECONDS.
        self.keepalive_interval = keepalive_interval
        self._keepalive_task: asyncio.Task[None] | None = None
        # command_id -> the QueuedCommand that was sent, kept only long enough to
        # know (a) whether a returning device result needs tabs-filtering / cache
        # updates, and (b) nothing more -- popped as soon as the result arrives.
        # Keyed globally by command_id (uuid4, unique across all devices) rather
        # than per-device, since that's what the device `result` handler has on
        # hand without needing to search every device's own bookkeeping.
        self._inflight: dict[str, QueuedCommand] = {}
        # session_id -> SessionScope (design doc section 11.2/15 step 5,
        # Candidate C). Hub-owned, not PolicyEngine-owned, because a session
        # outlives any one WebSocket connection -- the same reason the
        # per-device command queue outlives a device's connection (this
        # module's own docstring, reason 2). A session survives a device
        # disconnect/reconnect for exactly that reason: nothing here is keyed
        # by, or torn down by, a device's `/device`-route connection. See
        # "Sessions" section below for the full establish/narrow/seal wiring.
        self._sessions: dict[str, SessionScope] = {}
        # session_id -> asyncio.Lock (review panel F6: "session sealing has no
        # named serialization point -- two commands dispatched before the
        # first response lands may both evaluate against pre-seal scope").
        # `send_command` acquires this for the full evaluate-through-dispatch
        # span whenever a session_id is given, so two commands sharing a
        # session (e.g. issued from two concurrent agent connections, or one
        # connection racing a background poll) are strictly serialized: the
        # second command's `PolicyEngine.evaluate` call cannot begin until the
        # first command's full round trip -- including `_ingest_result`'s
        # seal-on-first-read, see `_maybe_seal_session` -- has completed. This
        # is the hub's one named serialization point for session-scoped state;
        # see `_session_lock` and `send_command`'s docstring.
        self._session_locks: dict[str, asyncio.Lock] = {}
        # Onboarding (this module's "Onboarding" section below, and
        # onboarding.py): a pre-built Android CRX/`.bin` this hub will serve at
        # `GET /setup/android-extension.bin`, if the operator has built one
        # (`scripts/package-android.sh`) and told this hub where it is
        # (`hub --android-artifact <path>`). `None` means "not configured" --
        # the route then 404s with an actionable message, and the `/setup`
        # page's Android section shows "no build available on this hub yet"
        # instead of a download link. Deliberately opt-in (never
        # auto-discovered from a guessed `dist/android/` path): serving a
        # stale or wrong artifact by accident would hand out a live
        # credential (see android_bake.py) to the wrong build.
        self.android_artifact = android_artifact
        # On-demand Android packing (android_pack.py) -- when no static
        # `--android-artifact` is configured, the hub can build one itself,
        # from its OWN currently-running token, the moment a browser asks for
        # it (see android_pack.py's module docstring for why the artifact
        # must always be baked from the LIVE token rather than a stale one).
        # Cached by the token actually baked in last time, so repeated
        # requests are fast and a token rotation (`init --force`) is picked
        # up on the very next request rather than serving a stale credential.
        self._android_pack_cache: tuple[str, bytes] | None = None
        if not token_store.auth_enabled:
            logger.warning(
                "No hub token configured (AMPLIFIER_BROWSER_BRIDGE_HUB_TOKEN / token file) -- running with "
                "auth DISABLED. Fine for local dev on a private tailnet; never ship this way."
            )

    def build_app(self) -> web.Application:
        app = web.Application()
        app.router.add_get("/healthz", self._handle_healthz)
        app.router.add_get("/device", self._handle_device_ws)
        app.router.add_get("/agent", self._handle_agent_ws)
        # Pairing redeem route (pairing.py, this module's "Agent protocol" ->
        # "Pairing" sections): deliberately plain HTTP, NOT gated by the
        # long-lived token -- see pairing.py's module docstring for why an
        # unauthenticated-by-token route is the correct design here, bounded
        # instead by the ticket's own short TTL/single-use/attempt-count limits.
        app.router.add_post("/pair/redeem", self._handle_pair_redeem)
        # Read-only redemption-status poll (pairing.py's `PairingStore.status`,
        # this module's "Onboarding" section below) -- the `/setup` page polls
        # this on the same ~1s cadence as its own visible countdown to learn
        # that ITS code was redeemed elsewhere and flip waiting -> paired,
        # instead of continuing to count down a code that has already been
        # used (see onboarding.py's module docstring for the bug this closes).
        # Unauthenticated for the identical bootstrapping reason `/pair/redeem`
        # is: the only thing it reveals is the state of a ticket the caller
        # already possesses the code for.
        app.router.add_post("/pair/status", self._handle_pair_status)
        # Onboarding routes (this module's "Onboarding" section below,
        # onboarding.py, extension_zip.py) -- deliberately plain HTTP, NOT
        # gated by the long-lived token, for the identical bootstrapping
        # reason `/pair/redeem` above is ungated: a browser that has never
        # been configured holds no token to authenticate with. See that
        # section for the full reasoning, including what these routes do and
        # do not expose.
        app.router.add_get("/setup", self._handle_setup_page)
        app.router.add_get("/setup/extension.zip", self._handle_setup_extension_zip)
        app.router.add_get("/setup/android", self._handle_setup_android_page)
        app.router.add_get("/setup/android-extension.bin", self._handle_setup_android_artifact)
        # Background soft-detach sweep (design doc §6.3/§7) -- started/stopped
        # via aiohttp's own app lifecycle so `cli.py`'s `hub` command needs no
        # changes to benefit from it. Tests that want deterministic control
        # call `soft_detach_idle_tabs()` directly instead of relying on this
        # loop's real sleep interval -- see tests/test_cdp.py.
        app.on_startup.append(self._start_soft_detach_task)
        app.on_cleanup.append(self._stop_soft_detach_task)
        # Keepalive sweep (see DEFAULT_KEEPALIVE_INTERVAL_SECONDS and
        # `keepalive_sweep`/`keepalive_loop` below) -- same lifecycle pattern
        # as the soft-detach sweep above. Tests call `keepalive_sweep()`
        # directly instead of relying on this loop's real sleep interval --
        # see tests/test_hub.py.
        app.on_startup.append(self._start_keepalive_task)
        app.on_cleanup.append(self._stop_keepalive_task)
        return app

    async def _start_soft_detach_task(self, app: web.Application) -> None:
        self._soft_detach_task = asyncio.create_task(self.soft_detach_loop())

    async def _stop_soft_detach_task(self, app: web.Application) -> None:
        if self._soft_detach_task is not None:
            self._soft_detach_task.cancel()
            self._soft_detach_task = None

    async def _start_keepalive_task(self, app: web.Application) -> None:
        self._keepalive_task = asyncio.create_task(self.keepalive_loop())

    async def _stop_keepalive_task(self, app: web.Application) -> None:
        if self._keepalive_task is not None:
            self._keepalive_task.cancel()
            self._keepalive_task = None

    # ------------------------------------------------------------------
    # HTTP
    # ------------------------------------------------------------------

    async def _handle_healthz(self, request: web.Request) -> web.Response:
        return web.json_response({"ok": True, "devices": len(self.registry.all())})

    # ------------------------------------------------------------------
    # Onboarding -- GET /setup, GET /setup/extension.zip,
    # GET /setup/android-extension.bin (onboarding.py, extension_zip.py).
    #
    # The bug this section fixes: `cli.py`'s `init` used to print a
    # filesystem path on the HUB machine ("select: /home/<user>/.local/
    # share/amplifier-browser-bridge/extension") as the instruction for
    # loading the extension into Edge. That instruction is only correct
    # when Edge happens to run on the same machine as the hub -- and this
    # project exists specifically for the opposite case (hub on a Linux
    # box, Edge on a MacBook or an Android phone), where the printed path
    # does not exist on the browser's machine at all. These routes let the
    # BROWSER fetch its own install artifact, from wherever it is, instead
    # of the operator trying to hand it a path on someone else's disk.
    #
    # ## Why these routes are NOT gated by the long-lived token
    #
    # A browser that has never been configured holds no hub token -- it
    # cannot authenticate to fetch the thing that would give it one. This
    # is the identical bootstrapping problem `/pair/redeem` above already
    # solves the same way (see pairing.py's module docstring, "Why the
    # ticket does not weaken the token as the security boundary"): gating
    # onboarding behind the very credential onboarding exists to hand out
    # would make it unreachable by the one browser that needs it.
    #
    # `muxplex` (a sibling project) hit the identical shape of problem
    # serving its own CA certificate + setup page, and resolved it the
    # same way for the same reason: the CA CERTIFICATE is not secret --
    # only its private key, which never leaves the server, is. "Sharing it
    # doesn't reduce your security. Sharing the .key does." (muxplex's
    # docs/TRUSTING_THE_LOCAL_CA.md, "Security notes"). The parallel here
    # is exact: this extension's SOURCE CODE is not a secret either -- it
    # is the same code already public on GitHub. `GET /setup` and
    # `GET /setup/extension.zip` hand out only that code plus generic
    # install instructions; downloading it here saves a `git clone`, and
    # nothing more. What actually stays secret -- the long-lived hub
    # token -- is never served by any route in this section.
    #
    # ## What "reachable" bounds this to
    #
    # Exactly the same boundary as every other hub route: something
    # already on this hub's tailnet (auth.py's module docstring,
    # docs/POLICY.md). An attacker who is not on the tailnet cannot reach
    # `/setup` at all. Someone who IS on the tailnet (during the live
    # window of an in-progress pairing) could in principle race the
    # intended device for a pairing code shown in the URL fragment -- but
    # that fragment never crosses this route at all (browsers never send
    # the URL fragment to a server; see onboarding.py's module docstring)
    # and the code itself is the same short-lived, single-use,
    # narrowly-scoped ticket `/pair/redeem` already accepts, bounded by
    # the identical entropy/TTL/attempt-count reasoning in pairing.py.
    # Nothing here widens that.
    #
    # ## The one route that DOES carry a live credential
    #
    # `GET /setup/android-extension.bin`, when configured
    # (`self.android_artifact`), serves a pre-built CRX that has a hub
    # token baked into it by design (android_bake.py's accepted trust
    # model -- Edge Android has no reachable options page, so there is no
    # other channel to configure it). That file is a real credential, and
    # this route does hand it to anyone on the tailnet who asks -- but
    # this is NOT new exposure: the retired `scripts/serve-android-setup.py`
    # already served the identical bytes, from its own unauthenticated
    # listener, under the identical tailnet-only boundary. Consolidating
    # both platforms onto this hub's existing listener REMOVES a second
    # listening port from the host's attack surface; it does not add one.
    # This route is opt-in (serves nothing unless `--android-artifact` was
    # explicitly configured) specifically so a hub never hands out a stale
    # or wrong build by accident.
    # ------------------------------------------------------------------

    async def _handle_setup_page(self, request: web.Request) -> web.Response:
        user_agent = request.headers.get("User-Agent", "")
        platform = detect_platform(user_agent)
        # request.url reflects what the CLIENT actually addressed (the Host
        # header aiohttp parsed the request against), not necessarily this
        # process's own --host bind value -- exactly the address that
        # belongs in a manual `pair`/hub-URL instruction shown back to that
        # same client.
        host = request.url.host or "127.0.0.1"
        port = request.url.port or DEFAULT_PORT
        html = render_setup_page(
            platform=platform,
            host=host,
            port=port,
            android_available=self._android_download_available(),
        )
        return web.Response(text=html, content_type="text/html")

    async def _handle_setup_android_page(self, request: web.Request) -> web.Response:
        host = request.url.host or "127.0.0.1"
        port = request.url.port or DEFAULT_PORT
        available = self._android_download_available()
        html = render_android_setup_page(
            host=host,
            port=port,
            download_available=available,
            unavailable_reason=None if available else self._android_unavailable_reason(),
        )
        return web.Response(text=html, content_type="text/html")

    async def _handle_setup_extension_zip(self, request: web.Request) -> web.Response:
        try:
            data = build_extension_zip_bytes()
        except (ExtensionSourceNotFoundError, ExtensionIntegrityError) as e:
            # Fail loud with an actionable 500, never a silently-truncated or
            # empty zip -- a bad/incomplete download is worse than a clear
            # error naming the exact staging failure.
            logger.error("could not build desktop extension zip for /setup/extension.zip: %s", e)
            return web.Response(status=500, text=f"could not build extension package: {e}")
        return web.Response(
            body=data,
            content_type="application/zip",
            headers={"Content-Disposition": 'attachment; filename="amplifier-browser-bridge-extension.zip"'},
        )

    # ------------------------------------------------------------------
    # On-demand Android packing (android_pack.py) -- see this class's
    # `_android_pack_cache` field comment and android_pack.py's module
    # docstring for the "why" behind on-demand-from-the-live-token packing.
    # ------------------------------------------------------------------

    def _android_download_available(self) -> bool:
        """True iff `GET /setup/android-extension.bin` will actually produce
        bytes right now -- a configured static artifact, OR (since no static
        artifact is a hard requirement, only a convenience) a host capable of
        packing one on demand. Never claims availability it can't back up --
        see this module's `_handle_setup_android_artifact`."""
        if self.android_artifact is not None and self.android_artifact.is_file():
            return True
        return find_packer_binary() is not None

    def _android_unavailable_reason(self) -> str:
        """A short, honest, actionable explanation for why the Android
        download isn't available right now -- never a vague "something went
        wrong". See android_pack.py's `PackerUnavailableError`."""
        return (
            "this hub can't build a package right now -- no Chromium/Chrome/Edge binary was "
            "found to pack a CRX with. An operator can fix this by setting CHROME_BIN to a real "
            "browser binary on this host, or by pre-building with scripts/package-android.sh and "
            "restarting the hub with --android-artifact <path> (see docs/ANDROID.md)."
        )

    async def _get_or_pack_android_artifact(self, *, host: str, port: int) -> bytes:
        """Return the Android CRX bytes to serve, packing on demand (and
        caching the result, keyed by the token actually baked in) if no
        static `--android-artifact` was configured. Raises `AndroidPackError`
        (including its `PackerUnavailableError` subclass) on failure -- the
        caller turns that into an honest HTTP response, never a silently
        broken download."""
        hub_token = self.token_store.default_token or ""
        cache_key = f"ws://{host}:{port}/device|{hub_token}"
        if self._android_pack_cache is not None and self._android_pack_cache[0] == cache_key:
            return self._android_pack_cache[1]

        data = await build_android_crx(hub_url=f"ws://{host}:{port}/device", hub_token=hub_token)
        self._android_pack_cache = (cache_key, data)
        return data

    async def _handle_setup_android_artifact(self, request: web.Request) -> web.Response:
        if self.android_artifact is not None and self.android_artifact.is_file():
            # Served as an opaque octet-stream `.bin` regardless of the source
            # file's own extension -- the same fix `scripts/serve-android-setup.py`
            # (now retired) applied for the same reason: Chromium intercepts a
            # `.crx`-recognized download and Edge Android silently discards it.
            # See docs/ANDROID.md's "Two packaging traps".
            data = self.android_artifact.read_bytes()
            filename = f"{self.android_artifact.stem}.bin"
            return web.Response(
                body=data,
                content_type="application/octet-stream",
                headers={"Content-Disposition": f'attachment; filename="{filename}"'},
            )

        host = request.url.host or "127.0.0.1"
        port = request.url.port or DEFAULT_PORT
        try:
            data = await self._get_or_pack_android_artifact(host=host, port=port)
        except PackerUnavailableError as e:
            # Honest, actionable unavailability -- never a download that 404s
            # or serves something broken with no explanation (see
            # android_pack.py's module docstring).
            return web.Response(status=503, text=str(e))
        except AndroidPackError as e:
            logger.error("on-demand Android packing failed for /setup/android-extension.bin: %s", e)
            return web.Response(status=500, text=f"could not build Android package: {e}")

        filename = "amplifier-browser-bridge-android.bin"
        return web.Response(
            body=data,
            content_type="application/octet-stream",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    # ------------------------------------------------------------------
    # Device protocol (extension <-> hub)
    # ------------------------------------------------------------------

    async def _handle_device_ws(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(heartbeat=None)  # app-level heartbeat, not transport-level
        await ws.prepare(request)
        device_id: str | None = None

        async for msg in ws:
            if msg.type != WSMsgType.TEXT:
                continue
            try:
                env = json.loads(msg.data)
            except json.JSONDecodeError:
                logger.warning("device sent non-JSON frame, ignoring")
                continue
            try:
                device_id = await self._handle_device_message(ws, device_id, env)
            except _DeviceAuthError:
                await ws.close()
                return ws

        if device_id:
            record = self.registry.get(device_id)
            # Race fix: only unbind if THIS connection is still the one the
            # record holds. Without this guard, a stale connection's belated
            # close -- e.g. an old TCP socket finally giving up seconds after
            # a reconnect already replaced it -- would wipe out the live
            # connection a newer `hello` had already bound. This was rare
            # when closes only happened as the OS reported them; the
            # keepalive sweep (`keepalive_sweep` below) now proactively
            # closes silent connections, which exercises this exit path far
            # more often, so the race had to be closed before that sweep
            # could ship safely.
            if record is not None and record.ws is ws:
                record.unbind()
                self.audit.record("device_disconnected", device_id=device_id)
                logger.info("device disconnected: %s", device_id)
            elif record is not None:
                self.audit.record("stale_connection_ignored", device_id=device_id)
                logger.info(
                    "stale connection for device %s closed after a newer connection already "
                    "replaced it -- ignoring (not unbinding the live connection)",
                    device_id,
                )

        return ws

    async def _handle_device_message(
        self, ws: DeviceConnection, device_id: str | None, env: dict[str, Any]
    ) -> str | None:
        """Process one `/device`-route message and return the (possibly
        newly-established) device_id for this connection. Extracted from
        `_handle_device_ws` so this logic -- including `hello`,
        `capabilities_update`, and CDP-related `event`s -- can be exercised
        directly in tests without a real WebSocket (see tests/test_hub.py,
        tests/test_capabilities.py). Only needs `send_json` (the same
        `DeviceConnection` protocol `registry.py`'s `DeviceRecord.bind` uses)
        -- never the concrete `web.WebSocketResponse` -- so test doubles
        don't need to satisfy aiohttp's full type. Raises `_DeviceAuthError`
        on a bad `hello` token; the caller is responsible for closing the
        connection in that case."""
        mtype = env.get("type")

        if mtype == "hello":
            candidate_id = env.get("device_id")
            if not candidate_id or not isinstance(candidate_id, str):
                await ws.send_json({"type": "error", "id": env.get("id"), "error": "hello missing device_id"})
                return device_id
            if not self.token_store.validate(env.get("token"), candidate_id):
                await ws.send_json({"type": "error", "id": env.get("id"), "error": "unauthorized"})
                raise _DeviceAuthError()
            device_id = candidate_id
            record = self.registry.get_or_create(device_id)
            record.bind(ws, env)
            self.audit.record(
                "device_connected",
                device_id=device_id,
                label=record.label,
                platform=record.platform,
                capabilities=record.capabilities,
            )
            logger.info("device connected: %s (%s, %s)", device_id, record.label, record.platform)
            # Now that the device is live, drain anything that queued up while it was away.
            asyncio.create_task(self._drain_queue(record))
            return device_id

        if mtype == "heartbeat":
            if device_id:
                self.registry.get_or_create(device_id).touch()
            return device_id

        if mtype == "result":
            if device_id:
                record = self.registry.get_or_create(device_id)
                record.touch()
                raw_cmd_id = env.get("id")
                if not isinstance(raw_cmd_id, str):
                    logger.warning("device sent a 'result' with no correlation id, ignoring")
                    return device_id
                cmd_id: str = raw_cmd_id
                env = self._ingest_result(device_id, cmd_id, env)
                fut = record.pending.pop(cmd_id, None)
                record.results[cmd_id] = env
                self.audit.record("result_received", device_id=device_id, command_id=cmd_id, ok=env.get("ok"))
                if fut is not None and not fut.done():
                    fut.set_result(env)
            return device_id

        if mtype == "event":
            if device_id:
                self.registry.get_or_create(device_id).touch()
                event_name = env.get("event")
                raw_data = env.get("data")
                data: dict[str, Any] = raw_data if isinstance(raw_data, dict) else {}
                # Unsolicited CDP detach (Cancel on the banner, DevTools
                # opened -- which force-detaches every session on the target
                # -- or the target crashed/was discarded). The hub did not
                # ask for this detach, so it must learn about it here rather
                # than from a `detach` command's own result -- see cdp.py's
                # module docstring and docs/POLICY.md's CDP section.
                if event_name == "cdp_detached":
                    tab_id = data.get("tab_id")
                    reason = data.get("reason")
                    if isinstance(tab_id, int):
                        self.cdp.mark_detached(
                            device_id,
                            tab_id,
                            reason=reason if isinstance(reason, str) else "unsolicited_detach",
                        )
                self.audit.record(
                    "device_event", device_id=device_id, device_event_name=event_name, data=data
                )
            return device_id

        if mtype == "capabilities_update":
            # Phase 1 finding: capture_visible_tab/scripting probes can
            # under-report `false` at `hello` time if no real tab existed yet
            # (design doc §2). The extension re-probes once a tab appears and
            # corrects the record here -- see background.js's
            # `maybeReprobe()`. Merge rather than replace: an update may
            # report a subset of keys.
            if device_id:
                record = self.registry.get_or_create(device_id)
                caps = env.get("capabilities")
                if isinstance(caps, dict):
                    merged = dict(record.capabilities)
                    for key, value in caps.items():
                        if isinstance(value, bool):
                            merged[key] = value
                    record.capabilities = merged
                    record.touch()
                    self.audit.record(
                        "capabilities_updated", device_id=device_id, capabilities=record.capabilities
                    )
            return device_id

        logger.warning("device sent unknown message type: %s", mtype)
        return device_id

    def _ingest_result(self, device_id: str, cmd_id: str | None, env: dict[str, Any]) -> dict[str, Any]:
        """Feed a device's `result` envelope into the policy engine and the
        CDP registry before it is stored or handed back to any agent -- the
        response-path half of the invisibility guarantee (design doc §6.2): a
        `tabs` result is filtered for denylisted hosts here, before it ever
        reaches `record.results` or a pending future, so both the
        immediate-dispatch and later-`poll` paths see the same sanitized
        data. Also feeds:

        - the policy engine's tab-host cache (`navigate`/`snapshot`/`read`),
          and its ref-label cache (`snapshot`/`wait_for` -- Phase 4, see
          policy.py's "Label hints are now wired") so gate detection has
          ground truth even for tabs/refs the agent never listed explicitly;
        - the CDP registry's attach state (`attach`/`detach` results -- Phase
          4, see cdp.py), handled first and unconditionally (even on
          failure), since an attach/detach's own success or failure IS the
          state transition, not a side observation of one.
        """
        cmd = self._inflight.pop(cmd_id, None) if cmd_id else None
        if cmd is None:
            return env

        if cmd.command == "attach":
            tab_id = cmd.target.tab_id
            if tab_id is not None:
                if env.get("ok"):
                    self.cdp.mark_attached(device_id, tab_id)
                    self.audit.record("cdp_attached", device_id=device_id, tab_id=tab_id)
                else:
                    self.audit.record(
                        "cdp_attach_failed", device_id=device_id, tab_id=tab_id, error=env.get("error")
                    )
            return env

        if cmd.command == "detach":
            tab_id = cmd.target.tab_id
            if tab_id is not None:
                if env.get("ok"):
                    reason = (
                        cmd.args.get("reason") if isinstance(cmd.args.get("reason"), str) else "requested"
                    )
                else:
                    reason = f"detach command failed: {env.get('error')}"
                self.cdp.mark_detached(device_id, tab_id, reason=reason)
                self.audit.record("cdp_detached", device_id=device_id, tab_id=tab_id, reason=reason)
            return env

        if not env.get("ok"):
            return env

        if cmd.command in _PAGE_CONTENT_RESULT_COMMANDS:
            # Seal-on-first-read (design doc section 11.2/15 step 5): a
            # `read`/`snapshot`/`tabs` result is page content reaching the
            # caller. From this point on, `cmd.session_id`'s SessionScope
            # (if any) can only narrow, never widen -- see scope.py's module
            # docstring for why this is the property that actually matters.
            self._maybe_seal_session(cmd.session_id)

        result = env.get("result")
        if cmd.command == "tabs" and isinstance(result, list):
            filtered = self.policy.filter_tabs_result(device_id, result)
            enriched = [
                {**tab, "cdp_attached": self.cdp.is_attached(device_id, tab["tab_id"])}
                if isinstance(tab, dict) and isinstance(tab.get("tab_id"), int)
                else tab
                for tab in filtered
            ]
            return {**env, "result": enriched}

        if cmd.command in _URL_BEARING_RESULT_COMMANDS and isinstance(result, dict):
            url = result.get("url")
            tab_id = cmd.target.tab_id
            if isinstance(url, str) and tab_id is not None:
                self.policy.note_tab_url(device_id, tab_id, url)
            if cmd.command == "snapshot":
                nodes = result.get("nodes")
                if isinstance(url, str) and tab_id is not None and isinstance(nodes, list):
                    self.policy.note_snapshot(device_id, tab_id, url, nodes)

        if cmd.command == "wait_for" and isinstance(result, dict):
            tab_id = cmd.target.tab_id
            ref = result.get("ref")
            url = result.get("url")
            if tab_id is not None and isinstance(ref, str) and isinstance(url, str):
                self.policy.note_ref(
                    device_id,
                    tab_id,
                    url,
                    ref,
                    label=result.get("name"),
                    tag=result.get("tag"),
                    input_type=result.get("input_type"),
                )

        if cmd.command == "snapshot" and isinstance(result, dict):
            # D1 (docs/designs/confirmation-gate.md section 11.4):
            # page_title/headings feed `PolicyEngine.note_page_context`, the
            # weak, page-asserted flow-elevation trigger -- symmetric with
            # note_effects below, which is the browser-asserted one.
            tab_id = cmd.target.tab_id
            if tab_id is not None:
                headings = result.get("headings")
                self.policy.note_page_context(
                    device_id,
                    tab_id,
                    result.get("url") if isinstance(result.get("url"), str) else None,
                    result.get("page_title") if isinstance(result.get("page_title"), str) else None,
                    headings if isinstance(headings, list) else [],
                )

        if cmd.command in STATE_CHANGING_COMMANDS:
            # D3 (docs/designs/confirmation-gate.md section 11): parse the
            # device's browser-asserted `effects` block (absent/malformed ->
            # the honest empty/"none"-tier report -- EffectsReport.from_wire
            # never raises), feed it into flow elevation (note_effects), and
            # attach it to the outgoing envelope so the caller sees it
            # regardless of whether the gate fired. Runs BEFORE `env` is
            # handed to the pending future (this method's own caller,
            # `_handle_device_message`, does that next) so the *next*
            # command in this tab already observes any resulting
            # flow_elevated -- the design doc's explicit ordering constraint.
            effects = EffectsReport.from_wire(env.get("effects"))
            tab_id = cmd.target.tab_id
            if tab_id is not None:
                effects_url = result.get("url") if isinstance(result, dict) else None
                if not isinstance(effects_url, str):
                    effects_url = self.policy._tab_hosts.get((device_id, tab_id))
                self.policy.note_effects(device_id, tab_id, effects, effects_url)
                if effects.state_changing:
                    self.audit.record(
                        "action_effects",
                        device_id=device_id,
                        tab_id=tab_id,
                        command=cmd.command,
                        effects=effects.to_wire(),
                    )
            env = {**env, "effects": effects.to_wire()}

        return env

    async def _drain_queue(self, record: DeviceRecord) -> None:
        """Send queued commands in FIFO order now that the device is live. Stops
        (leaving the remainder queued) the moment the device disconnects again."""
        while record.connected:
            cmd = record.queue.pop_next()
            if cmd is None:
                return
            self.audit.record(
                "command_drained", device_id=record.device_id, command_id=cmd.id, command=cmd.command
            )
            try:
                await self._dispatch_live(record, cmd)
            except ConnectionError:
                return

    # ------------------------------------------------------------------
    # Agent protocol (CLI/lib <-> hub)
    # ------------------------------------------------------------------

    async def _handle_agent_ws(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)

        async for msg in ws:
            if msg.type != WSMsgType.TEXT:
                continue
            try:
                req = json.loads(msg.data)
            except json.JSONDecodeError:
                await ws.send_json({"type": "error", "id": None, "error": "invalid JSON"})
                continue

            rid = req.get("id") or new_id()
            if not self.token_store.validate(req.get("token")):
                await ws.send_json(
                    {"v": PROTOCOL_VERSION, "type": "error", "id": rid, "error": "unauthorized"}
                )
                continue

            rtype = req.get("type")
            try:
                if rtype == "list_devices":
                    await ws.send_json(
                        {
                            "v": PROTOCOL_VERSION,
                            "type": "devices",
                            "id": rid,
                            "devices": self._devices_snapshot(),
                        }
                    )
                elif rtype == "command":
                    result = await self._handle_agent_command(req)
                    await ws.send_json({"v": PROTOCOL_VERSION, "type": "result", "id": rid, **result})
                elif rtype == "poll":
                    result = self._poll(req.get("device_id", ""), req.get("command_id", ""))
                    await ws.send_json({"v": PROTOCOL_VERSION, "type": "result", "id": rid, **result})
                elif rtype == "confirm":
                    result = await self._handle_agent_confirm(req)
                    await ws.send_json({"v": PROTOCOL_VERSION, "type": "result", "id": rid, **result})
                elif rtype == "establish_session":
                    result = self._handle_establish_session(req)
                    await ws.send_json({"v": PROTOCOL_VERSION, "type": "result", "id": rid, **result})
                elif rtype == "narrow_scope":
                    result = self._handle_narrow_scope(req)
                    await ws.send_json({"v": PROTOCOL_VERSION, "type": "result", "id": rid, **result})
                elif rtype == "kill_switch_engage":
                    result = self._handle_kill_switch_engage(req)
                    await ws.send_json({"v": PROTOCOL_VERSION, "type": "result", "id": rid, **result})
                elif rtype == "kill_switch_disengage":
                    result = self._handle_kill_switch_disengage(req)
                    await ws.send_json({"v": PROTOCOL_VERSION, "type": "result", "id": rid, **result})
                elif rtype == "kill_switch_status":
                    result = self._handle_kill_switch_status()
                    await ws.send_json({"v": PROTOCOL_VERSION, "type": "result", "id": rid, **result})
                elif rtype == "create_pairing":
                    result = self._handle_create_pairing(req)
                    await ws.send_json({"v": PROTOCOL_VERSION, "type": "result", "id": rid, **result})
                else:
                    await ws.send_json(
                        {
                            "v": PROTOCOL_VERSION,
                            "type": "error",
                            "id": rid,
                            "error": f"unknown request type: {rtype}",
                        }
                    )
            except Exception as exc:  # fail loud, but never crash the agent connection
                logger.exception("error handling agent request")
                await ws.send_json({"v": PROTOCOL_VERSION, "type": "error", "id": rid, "error": str(exc)})

        return ws

    async def _handle_agent_command(self, req: dict[str, Any]) -> dict[str, Any]:
        try:
            target = Target.from_dict(req.get("target") or {})
        except TargetError as e:
            return {"ok": False, "error": str(e)}
        command = req.get("command", "")
        args = req.get("args") or {}
        raw_session_id = req.get("session_id")
        session_id = raw_session_id if isinstance(raw_session_id, str) and raw_session_id else None
        return await self.send_command(target, command, args, session_id=session_id)

    async def _handle_agent_confirm(self, req: dict[str, Any]) -> dict[str, Any]:
        """Handle a `confirm` request: consume a single-use confirmation token
        (from a prior `needs_confirmation` response) and, if valid and unexpired,
        re-submit the original gated command with `skip_gate=True`. The denylist
        check still runs (a confirmation is not a denylist bypass) -- only the
        gate itself is skipped, since a human has already explicitly approved
        this exact action. The ORIGINAL session's scope (if any) is re-resolved
        and re-checked too -- see `PolicyEngine.evaluate`'s docstring on why
        scope enforcement runs before `skip_gate`, and `PendingConfirmation.
        session_id`."""
        token = req.get("confirmation_token")
        if not token or not isinstance(token, str):
            return {"ok": False, "error": "confirm requires 'confirmation_token'"}
        try:
            # via="agent" -- the `/agent` WebSocket route is the ONLY redemption
            # surface in this codebase, and will remain so: there is no human-
            # approval channel (a design was considered and explicitly
            # CANCELLED -- see docs/designs/approval-channel-options.md).
            # Reached by both an agent's own `confirm` call and a human running
            # `amplifier-browser-bridge confirm` from the CLI; see cli.py's `confirm` docstring.
            # `consume_confirmation` refuses any token whose
            # PendingConfirmation.redeem != "agent" -- this is what makes
            # `redeem: "unredeemable"` structurally unredeemable through this
            # handler (or any other), closing the live self-attestation hole
            # (see policy.py's `consume_confirmation` docstring).
            pending = self.policy.consume_confirmation(token, via="agent")
        except PolicyError as e:
            return {"ok": False, "error": str(e)}
        self.audit.record(
            "policy_confirmed",
            device_id=pending.target.device_id,
            tab_id=pending.target.tab_id,
            command=pending.command,
            category=pending.category,
            token=token,
            session_id=pending.session_id,
        )
        return await self.send_command(
            pending.target, pending.command, pending.args, skip_gate=True, session_id=pending.session_id
        )

    # ------------------------------------------------------------------
    # Sessions -- caller-declared write scope (design doc section 11.2/15
    # step 5, Candidate C). See scope.py's module docstring for the full
    # "why a prompt-injected model can't use this to widen its own grant"
    # argument; this section is the wire-level half of that argument.
    #
    # `establish_session` ALWAYS mints a fresh session_id (uuid4) and never
    # accepts a caller-supplied one -- this is deliberate and load-bearing:
    # it is what stops `establish_session` from ever being replayed against
    # an EXISTING (possibly already-sealed) session to silently reset its
    # scope back to broad. To change an existing session, the ONLY path is
    # `narrow_scope`, which can only narrow (scope.py's SessionScope.narrow)
    # and is refused outright once the session is sealed.
    #
    # A session's scope is intentionally NOT torn down when its device
    # disconnects/reconnects -- see `self._sessions`'s declaration in
    # `__init__`. Mobile devices drop and re-attach by design (this
    # project's own three-tier connectivity model); a scope that evaporated
    # on every reconnect would defeat its own purpose the moment the
    # human's phone went to sleep. A session is torn down ONLY by hub
    # process restart (in-memory, like `_confirmations` and `_flow_elevated`
    # in policy.py) -- there is no idle-expiry sweep for sessions in this
    # phase, matching those two precedents.
    # ------------------------------------------------------------------

    def establish_session(self, **scope_kwargs: Any) -> SessionScope:
        """Create a brand-new session with a caller-declared initial scope.
        Returns the new `SessionScope` (its `session_id` is the hub-minted
        one -- never accepted from the caller)."""
        session_id = uuid.uuid4().hex
        scope = SessionScope.from_wire(session_id, scope_kwargs)
        self._sessions[session_id] = scope
        self.audit.record("session_established", session_id=session_id, scope=scope.to_wire())
        return scope

    def narrow_session_scope(self, session_id: str, **kwargs: Any) -> SessionScope:
        """Narrow an EXISTING session's scope. Raises `ScopeError` for an
        unknown session_id, any widening attempt, or any change at all once
        the session is sealed (`SessionScope.narrow`'s own guarantees)."""
        scope = self._sessions.get(session_id)
        if scope is None:
            raise ScopeError(f"unknown session_id: {session_id!r}")
        scope.narrow(**kwargs)
        self.audit.record("policy_scope_narrowed", session_id=session_id, scope=scope.to_wire())
        return scope

    def _handle_establish_session(self, req: dict[str, Any]) -> dict[str, Any]:
        try:
            scope = self.establish_session(**{k: v for k, v in req.items() if k in SCOPE_FIELDS})
        except ScopeError as e:
            return {"ok": False, "error": str(e)}
        return {"ok": True, "session_id": scope.session_id, "scope": scope.to_wire()}

    def _handle_narrow_scope(self, req: dict[str, Any]) -> dict[str, Any]:
        session_id = req.get("session_id")
        if not session_id or not isinstance(session_id, str):
            return {"ok": False, "error": "narrow_scope requires 'session_id'"}
        try:
            scope = self.narrow_session_scope(
                session_id, **{k: v for k, v in req.items() if k in SCOPE_FIELDS}
            )
        except ScopeError as e:
            return {"ok": False, "error": str(e)}
        return {"ok": True, "session_id": scope.session_id, "scope": scope.to_wire()}

    # ------------------------------------------------------------------
    # Kill switch (A4 fix, security review finding): docs/POLICY.md section
    # 5 always described this as a real, available control -- README's
    # consent table listed it right alongside the denylist and confirmation
    # gates. It was true only at the library level (`Hub.engage_kill_switch`/
    # `disengage_kill_switch`, called directly by a test or an embedding
    # app) -- no operator running the shipped `amplifier-browser-bridge` CLI, and no agent
    # over the wire protocol, had any way to reach it. These three wire
    # messages (and the matching `amplifier-browser-bridge kill-switch` CLI subcommand) are
    # what make the documented control actually reachable, from the same
    # `/agent` route and the same token-checked path every other agent
    # request already goes through.
    # ------------------------------------------------------------------

    def _handle_kill_switch_engage(self, req: dict[str, Any]) -> dict[str, Any]:
        del req  # no fields consumed today; kept for symmetry/future audit context
        rejected = self.engage_kill_switch()
        return {"ok": True, "kill_switch_active": True, "rejected_queued_commands": rejected}

    def _handle_kill_switch_disengage(self, req: dict[str, Any]) -> dict[str, Any]:
        del req  # no fields consumed today; kept for symmetry/future audit context
        self.disengage_kill_switch()
        return {"ok": True, "kill_switch_active": False}

    def _handle_kill_switch_status(self) -> dict[str, Any]:
        return {"ok": True, "kill_switch_active": self.policy.kill_switch_active}

    # ------------------------------------------------------------------
    # Pairing (pairing.py) -- ticket MINTING is here, on the SAME
    # token-authenticated `/agent` route as every other agent command (see
    # `_handle_agent_ws`'s token check, which runs before `rtype` is even
    # inspected). Ticket REDEMPTION is the separate, unauthenticated-by-token
    # `_handle_pair_redeem` HTTP handler below -- see pairing.py's module
    # docstring for why that asymmetry is the whole point.
    # ------------------------------------------------------------------

    def _handle_create_pairing(self, req: dict[str, Any]) -> dict[str, Any]:
        raw_ttl = req.get("ttl_seconds")
        ttl_seconds = DEFAULT_TICKET_TTL_SECONDS
        if raw_ttl is not None:
            try:
                ttl_seconds = float(raw_ttl)
            except (TypeError, ValueError):
                return {"ok": False, "error": f"ttl_seconds must be numeric, got {raw_ttl!r}"}
            if ttl_seconds <= 0:
                return {"ok": False, "error": "ttl_seconds must be positive"}
        record = self.pairing.create(ttl_seconds=ttl_seconds)
        self.audit.record("pairing_created", expires_in=ttl_seconds)
        return {
            "ok": True,
            "ticket": format_ticket(record.ticket),
            "expires_in": ttl_seconds,
            "persisted": self.token_file is not None,
        }

    async def _handle_pair_redeem(self, request: web.Request) -> web.Response:
        """`POST /pair/redeem` -- the ONE hub route a brand-new, never-before-seen
        device can reach without already holding the long-lived token. See
        pairing.py's module docstring for the full entropy/lifetime/threat-model
        reasoning behind why this is safe: the ticket this consumes is short-
        lived, single-use, and attempt-bounded, and its only power is minting a
        real per-device token -- it grants no other capability.
        """
        try:
            body = await request.json()
        except (json.JSONDecodeError, ContentTypeError, UnicodeDecodeError):
            return web.json_response({"ok": False, "error": "request body must be JSON"}, status=400)
        if not isinstance(body, dict):
            return web.json_response({"ok": False, "error": "request body must be a JSON object"}, status=400)

        raw_ticket = body.get("ticket")
        device_id = body.get("device_id")
        if not isinstance(raw_ticket, str) or not raw_ticket:
            return web.json_response({"ok": False, "error": "'ticket' is required"}, status=400)
        if not isinstance(device_id, str) or not device_id:
            return web.json_response({"ok": False, "error": "'device_id' is required"}, status=400)

        try:
            self.pairing.redeem(raw_ticket)
        except PairingError as e:
            self.audit.record("pairing_redeem_failed", device_id=device_id, error=str(e))
            return web.json_response({"ok": False, "error": str(e)}, status=403)

        if self.token_store.auth_enabled:
            token = generate_device_token()
            self.token_store.device_tokens[device_id] = token
            persisted = False
            if self.token_file is not None:
                try:
                    persist_device_token(self.token_file, device_id, token)
                    persisted = True
                except OSError:
                    logger.exception(
                        "pairing: minted a device token for %s but could not persist it to %s -- "
                        "it will be lost on the next hub restart",
                        device_id,
                        self.token_file,
                    )
        else:
            # Auth disabled hub-wide (dev mode, loudly logged at startup -- see
            # __init__) -- nothing to protect, so pairing hands back an empty
            # token, consistent with every other device on this hub already
            # being accepted with no token at all (auth.py's TokenStore.validate).
            token = ""
            persisted = False

        label = body.get("label") if isinstance(body.get("label"), str) else None
        platform = body.get("platform") if isinstance(body.get("platform"), str) else None
        self.audit.record(
            "pairing_redeemed", device_id=device_id, label=label, platform=platform, persisted=persisted
        )
        return web.json_response({"ok": True, "token": token, "device_id": device_id, "persisted": persisted})

    async def _handle_pair_status(self, request: web.Request) -> web.Response:
        """`POST /pair/status` -- read-only redemption-status poll for a ticket
        (`pairing.py`'s `PairingStore.status`; see this module's `build_app`
        comment on this route for the full reasoning). Returns
        `{"ok": true, "status": "pending" | "redeemed" | "unknown"}`.

        Deliberately POST (ticket in the JSON body), not GET-with-query-string,
        even though this never mutates anything -- matching `/pair/redeem`'s own
        shape keeps the ticket out of the kind of URL/query-string logging a GET
        request commonly attracts, for a value that (while low-sensitivity and
        short-lived) is still a real bootstrap credential.
        """
        try:
            body = await request.json()
        except (json.JSONDecodeError, ContentTypeError, UnicodeDecodeError):
            return web.json_response({"ok": False, "error": "request body must be JSON"}, status=400)
        if not isinstance(body, dict):
            return web.json_response({"ok": False, "error": "request body must be a JSON object"}, status=400)

        raw_ticket = body.get("ticket")
        if not isinstance(raw_ticket, str) or not raw_ticket:
            return web.json_response({"ok": False, "error": "'ticket' is required"}, status=400)

        status = self.pairing.status(raw_ticket)
        return web.json_response({"ok": True, "status": status})

    def _maybe_seal_session(self, session_id: str | None) -> None:
        """Seal a session the first time any of its commands yields page
        content back to the caller (design doc section 11.2: "Hub calls
        seal() the first time a session receives page content"). Idempotent
        at the call site too (checks `sealed` first) purely to avoid a
        redundant audit event on every subsequent read -- `SessionScope.seal`
        itself is already safe to call repeatedly."""
        if session_id is None:
            return
        scope = self._sessions.get(session_id)
        if scope is None or scope.sealed:
            return
        scope.seal()
        self.audit.record("policy_scope_sealed", session_id=session_id)

    def _poll(self, device_id: str, command_id: str) -> dict[str, Any]:
        record = self.registry.get(device_id)
        if record is None:
            return {"ok": False, "error": f"unknown device: {device_id}"}
        if command_id in record.results:
            return record.results[command_id]
        if command_id in record.pending:
            return {"status": "pending"}
        position = record.queue.position(command_id)
        if position is not None:
            return {"status": "queued", "queue_position": position, "tier": record.tier.value}
        return {"ok": False, "error": f"unknown command_id: {command_id}"}

    # ------------------------------------------------------------------
    # Core dispatch logic -- used by both the agent route and tests directly
    # ------------------------------------------------------------------

    async def send_command(
        self,
        target: Target,
        command: str,
        args: dict[str, Any],
        *,
        skip_gate: bool = False,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """The single choke point: every command, from every caller (the agent
        route, tests driving the Hub directly, and the post-confirmation
        re-dispatch in `_handle_agent_confirm`), passes through here. Nothing
        else in this module constructs a `QueuedCommand` or calls
        `_dispatch_live`/`queue.enqueue` -- see the module docstring.

        `skip_gate` is set only by `_handle_agent_confirm`, after a human has
        already explicitly approved this exact (target, command, args) via a
        single-use confirmation token. It skips the *gate* check only -- the
        denylist/kill-switch checks in `PolicyEngine.evaluate` always run.

        `session_id`, if given, must name a session already created via
        `establish_session` -- an unknown id fails loud (`unknown_session`)
        rather than silently falling back to scope-free behavior, which would
        let a typo'd or expired session_id quietly defeat the caller's own
        declared scope. Omitting `session_id` entirely is the existing,
        fully-permissive default every call site that predates `scope.py`
        keeps getting (design doc section 8, "Migration").
        """
        if command not in COMMANDS:
            return {"ok": False, "error": f"unknown command: {command!r}. Valid: {sorted(COMMANDS)}"}

        scope: SessionScope | None = None
        if session_id is not None:
            scope = self._sessions.get(session_id)
            if scope is None:
                return {
                    "ok": False,
                    "error": f"unknown session_id: {session_id!r} (establish_session first)",
                    "reason_code": "unknown_session",
                }

        # F6 (review panel): serialize evaluate-through-dispatch per session_id
        # so two commands sharing a session can never both evaluate against
        # pre-seal scope (see `_session_lock` and this class's `__init__`
        # docstring for `_session_locks`). `nullcontext()` when there's no
        # session_id -- the existing scope-free path is unaffected and stays
        # fully concurrent, matching every pre-scope.py call site.
        lock_cm = self._session_lock(session_id) if session_id is not None else contextlib.nullcontext()
        async with lock_cm:
            return await self._send_command_locked(
                target, command, args, skip_gate=skip_gate, session_id=session_id, scope=scope
            )

    def _session_lock(self, session_id: str) -> asyncio.Lock:
        lock = self._session_locks.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            self._session_locks[session_id] = lock
        return lock

    async def _send_command_locked(
        self,
        target: Target,
        command: str,
        args: dict[str, Any],
        *,
        skip_gate: bool,
        session_id: str | None,
        scope: SessionScope | None,
    ) -> dict[str, Any]:
        """The body of `send_command`, run inside the per-session lock (see
        F6 above) when `session_id` is given. Holding the lock across the
        FULL span -- evaluate, then (if live) dispatch and await the device's
        result, whose ingestion is what seals the session -- is what
        guarantees a second command for the same session cannot begin
        `evaluate()` until the first one's seal (if any) has already applied.
        """
        # `_cdp` is a hub-internal wire signal (see cdp.py, `_dispatch_live`)
        # that authorizes the DEVICE to use CDP dispatch for this specific
        # command. It is set ONLY by the hub's own auto-escalation logic --
        # never accepted from a caller. Honoring a caller-supplied `_cdp`
        # would let an agent (or a prompt-injected one) request trusted-input
        # / hidden-capture dispatch without ever passing through the
        # capability check or attach bookkeeping in `_ensure_cdp_attached` --
        # the same capability-binding discipline policy.py applies to
        # denylisted targets applies here to CDP usage. `trusted` and
        # `capture_hidden` remain legitimate caller-facing intent args.
        if "_cdp" in args:
            args = {k: v for k, v in args.items() if k != "_cdp"}

        timeout_override, args, timeout_error = self._extract_timeout_override(args)
        if timeout_error is not None:
            return {"ok": False, "error": timeout_error}

        decision = self.policy.evaluate(target, command, args, skip_gate=skip_gate, scope=scope)
        if decision.status == "deny":
            deny_response: dict[str, Any] = {"ok": False, "error": decision.reason}
            if decision.reason_code:
                deny_response["reason_code"] = decision.reason_code
            return deny_response
        if decision.status == "gate":
            return {
                "status": "needs_confirmation",
                "confirmation_token": decision.token,
                "category": decision.category,
                "detected": decision.detected,
                "classification": decision.classification.to_wire() if decision.classification else None,
                "redeem": decision.redeem,
                "confirm_scope": decision.confirm_scope,
                "expires_at": (
                    datetime.fromtimestamp(decision.expires_at, UTC).isoformat()
                    if decision.expires_at is not None
                    else None
                ),
            }

        record = self.registry.get(target.device_id)
        if record is None:
            return {"ok": False, "error": f"unknown device: {target.device_id!r}"}

        # Tier 0 handshake fast-fail (skew.py): refuse a command this device
        # has POSITIVELY reported it cannot execute, before it is ever
        # dispatched -- instead of letting `background.js`'s own if-chain
        # fall through to a bare "unsupported command: X" with no hint the
        # extension is stale (the common, useless case this feature exists
        # to fix). Deliberately a no-op (returns `None`) when the device's
        # command set isn't known yet (pre-Tier-0 extension) -- see
        # skew.py's module docstring for why blocking dispatch on unknown
        # commands would also block `reload` itself for every
        # currently-connected browser the moment this ships.
        skew_error = _skew_capability_error(command, record.commands)
        if skew_error is not None:
            return skew_error

        cmd = QueuedCommand(
            id=new_id(),
            target=target,
            command=command,
            args=args,
            timeout=timeout_override,
            session_id=session_id,
        )

        # D1 (docs/designs/confirmation-gate.md section 7): attach the
        # classification to the returned envelope on EVERY state-changing
        # decision, gated or not -- "reported on every state-changing
        # result, whether or not it gated" (design doc section 5). Computed
        # once here from the already-evaluated `decision`, not round-tripped
        # through the device.
        classification_wire = decision.classification.to_wire() if decision.classification else None
        # A5 fix -- see SCOPE_UNSCOPED_WARNING's module-level comment.
        scope_warning = (
            SCOPE_UNSCOPED_WARNING if scope is None and command in STATE_CHANGING_COMMANDS else None
        )

        if record.tier is Tier.LIVE:
            result = await self._dispatch_live(record, cmd)
            if classification_wire is not None:
                result = {**result, "classification": classification_wire}
            if scope_warning is not None:
                result = {**result, "scope_warning": scope_warning}
            return result

        position = record.queue.enqueue(cmd)
        self.audit.record(
            "command_queued",
            device_id=target.device_id,
            command_id=cmd.id,
            command=command,
            tier=record.tier.value,
            queue_position=position,
        )
        queued_response: dict[str, Any] = {
            "status": "queued",
            "command_id": cmd.id,
            "tier": record.tier.value,
            "last_seen": record.last_seen.isoformat() if record.last_seen else None,
            "queue_position": position,
        }
        if classification_wire is not None:
            queued_response["classification"] = classification_wire
        if scope_warning is not None:
            queued_response["scope_warning"] = scope_warning
        return queued_response

    @staticmethod
    def _extract_timeout_override(args: dict[str, Any]) -> tuple[float | None, dict[str, Any], str | None]:
        """Pop and validate `args.timeout_s` (see protocol.py's HUB_ONLY_ARGS).

        Returns `(timeout_override, remaining_args, error)`. `timeout_override`
        is `None` if the caller didn't supply one (meaning: use the hub's
        configured `command_timeout`). `error` is a human-readable message if
        the caller supplied a value that isn't usable -- fail loud rather than
        silently clamping or ignoring a bad value (design doc \u00a78).

        This never forwards `timeout_s` to the device: it is a hub-only
        knob (how long the HUB waits for a device reply), not something
        `injected.js`/`background.js` have any use for.
        """
        if "timeout_s" not in args:
            return None, args, None
        raw = args["timeout_s"]
        remaining = {k: v for k, v in args.items() if k not in HUB_ONLY_ARGS}
        try:
            value = float(raw)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None, remaining, f"args.timeout_s must be a number (seconds), got: {raw!r}"
        if not math.isfinite(value):
            return None, remaining, f"args.timeout_s must be a finite number, got: {raw!r}"
        if not (MIN_COMMAND_TIMEOUT <= value <= MAX_COMMAND_TIMEOUT):
            error = (
                f"args.timeout_s must be between {MIN_COMMAND_TIMEOUT} and {MAX_COMMAND_TIMEOUT} "
                f"seconds, got: {value}"
            )
            return None, remaining, error
        return value, remaining, None

    @staticmethod
    def _timeout_hint(command: str, args: dict[str, Any]) -> str:
        """A trailing sentence naming a DIFFERENT mechanism the caller may choose
        when a command's own device round trip times out -- discoverability, not
        decision-making (design doc's "Mechanism, not policy" section): this never
        retries the command differently or substitutes one mechanism for another
        itself, it only names what exists so the caller can pick.

        Returns an empty string for commands with no relevant alternative to name.
        """
        hint = ""
        if command in ("read", "snapshot"):
            if truthy(args.get("all_frames")):
                hint += (
                    " This request already used args.all_frames=true (every frame is instrumented "
                    "before any result returns, which is slower) -- if you know which frame you need "
                    "from a prior result's `frames` entry, args.frame_id=<id> targets just that one "
                    "frame and skips the rest."
                )
            else:
                hint += (
                    " If the content you need lives inside an embedded frame (e.g. a SharePoint/M365 "
                    "document viewer) rather than the top frame, args.all_frames=true gathers every frame "
                    "-- slower, so raise args.timeout_s alongside it."
                )
        if command in ("click", "type", "key"):
            hint += (
                " If the page ignores untrusted synthetic events, args.trusted=true escalates to "
                "CDP-backed isTrusted input (requires the debugger capability on this device)."
            )
        # Bug 3 (real-profile hardening): DOM injection/traversal on a heavy SPA is
        # viable-when-foreground, dead-when-background (measured: a heavy enterprise
        # SPA timed out at 170s backgrounded, ~2s once activated). Name every real
        # option -- never pick one automatically (design doc's "Mechanism, not
        # policy" section) -- for every DOM-injecting command this timeout applies to.
        page_world_commands = (
            "snapshot",
            "read",
            "click",
            "type",
            "key",
            "scroll",
            "back",
            "forward",
            "wait_for",
            "wait_text",
        )
        if command in page_world_commands and not truthy(args.get("activate")):
            if command in ("read", "snapshot"):
                hint += (
                    " If the target tab is not the active tab, DOM injection/traversal can be slow or "
                    "hang on a heavy page while backgrounded -- args.activate=true activates the tab "
                    "first (fast, exact DOM, but steals the human's focus), the agent-surface-only "
                    "vision_read (not a wire command) captures a screenshot and extracts text via a "
                    "vision model instead (no focus steal, costs a model call, produces no element "
                    "refs), or raise args.timeout_s."
                )
            else:
                hint += (
                    " If the target tab is not the active tab, DOM injection can be slow or hang on a "
                    "heavy page while backgrounded -- args.activate=true activates the tab first (fast, "
                    "exact DOM, but steals the human's focus), or raise args.timeout_s."
                )
        return hint

    async def _dispatch_live(self, record: DeviceRecord, cmd: QueuedCommand) -> dict[str, Any]:
        """The single choke point for CDP escalation (see this module's
        docstring). Called for both the immediate-dispatch path
        (`send_command`, device already live) and the drained-later path
        (`_drain_queue`, once a queued device reconnects) -- CDP escalation
        must apply identically to both, since a caller has no way to know
        (or control) which path their command will take."""
        assert record.ws is not None  # only called when record.tier is LIVE
        if requires_cdp(cmd.command, cmd.args):
            cdp_error = await self._ensure_cdp_attached(record, cmd.target.tab_id)
            if cdp_error is not None:
                # Never silently fall back to the injection-only path the
                # caller didn't ask for (design doc §8). Persist the result
                # exactly as if a device `result` had arrived, so a command
                # that was queued and only reaches this branch on drain still
                # behaves correctly under `poll()` -- see docs/PROTOCOL.md.
                self._inflight.pop(cmd.id, None)
                record.results[cmd.id] = {"v": PROTOCOL_VERSION, "id": cmd.id, "type": "result", **cdp_error}
                return cdp_error
            cmd = replace(cmd, args={**cmd.args, "_cdp": True})
        return await self._send_and_await(record, cmd)

    async def _ensure_cdp_attached(self, record: DeviceRecord, tab_id: int | None) -> dict[str, Any] | None:
        """Pre-flight for a CDP-requiring command. Returns `None` if CDP is
        (now) attached and the real command may proceed; otherwise an
        `{"ok": False, "error": ...}` dict to return in its place. Attaches
        on demand (never speculatively) by sending a real `attach` command
        and waiting for the device's result -- exactly the same wire
        round-trip an agent's own explicit `attach` command would produce."""
        if tab_id is None:
            return {"ok": False, "error": "CDP-requiring command needs an explicit tab_id in target"}
        if not record.capabilities.get("debugger"):
            return {
                "ok": False,
                "error": (
                    f"capability unavailable on this device ({record.label}): chrome.debugger/CDP is "
                    "not present here (e.g. Edge Android) -- cannot satisfy trusted input or "
                    "hidden-tab capture; refusing rather than silently falling back to the "
                    "injection-only path the caller didn't ask for. This device can still run "
                    "untrusted injected click/type/key (drop args.trusted) and capture the ACTIVE "
                    "tab only via chrome.tabs.captureVisibleTab (drop args.capture_hidden) -- use "
                    "those directly if isTrusted input or hidden/background-tab capture isn't "
                    "strictly required."
                ),
            }
        if self.cdp.is_attached(record.device_id, tab_id):
            self.cdp.touch(record.device_id, tab_id)
            return None

        attach_cmd = QueuedCommand(
            id=new_id(), target=Target(device_id=record.device_id, tab_id=tab_id), command="attach", args={}
        )
        result = await self._send_and_await(record, attach_cmd)
        if not result.get("ok"):
            return {"ok": False, "error": f"CDP auto-attach failed: {result.get('error', 'unknown error')}"}
        self.cdp.touch(record.device_id, tab_id)
        return None

    async def _send_and_await(self, record: DeviceRecord, cmd: QueuedCommand) -> dict[str, Any]:
        """Raw wire send + await the device's result future. No policy, no
        CDP escalation -- `_dispatch_live` is the only caller for real
        commands; tests may call this directly to bypass escalation."""
        assert record.ws is not None  # only called when record.tier is LIVE
        effective_timeout = cmd.timeout if cmd.timeout is not None else self.command_timeout
        loop = asyncio.get_event_loop()
        fut: asyncio.Future = loop.create_future()
        record.pending[cmd.id] = fut
        # Remembered only so `_ingest_result` (called when the device's `result`
        # arrives) knows which command this was, for tabs-filtering / tab-host
        # cache updates -- see that method and the `_URL_BEARING_RESULT_COMMANDS`
        # note above. Popped there; also cleaned up below on every early-return
        # path so a dropped/timed-out command never leaks an entry.
        self._inflight[cmd.id] = cmd

        env = {
            "v": PROTOCOL_VERSION,
            "id": cmd.id,
            "type": "command",
            "command": cmd.command,
            "target": cmd.target.to_dict(),
            "args": cmd.args,
        }
        self.audit.record(
            "command_sent",
            device_id=record.device_id,
            command_id=cmd.id,
            command=cmd.command,
            target=env["target"],
        )

        try:
            await record.ws.send_json(env)
        except ConnectionResetError as e:
            record.pending.pop(cmd.id, None)
            self._inflight.pop(cmd.id, None)
            return {"ok": False, "error": f"device connection lost while sending command: {e}"}

        try:
            result_env = await asyncio.wait_for(fut, timeout=effective_timeout)
        except TimeoutError:
            record.pending.pop(cmd.id, None)
            self._inflight.pop(cmd.id, None)
            # Actionable, not just "timeout" (real-world finding: a heavy SPA's
            # `read` needed longer than the prior fixed 30s even at
            # status:"complete" -- see DEFAULT_COMMAND_TIMEOUT's comment and
            # docs/PROTOCOL.md's "Command timeout" section). Discoverability,
            # not decision-making (design doc's "Mechanism, not policy"
            # section): _timeout_hint NAMES an alternative mechanism the
            # caller may choose -- it never retries or substitutes one for
            # another itself.
            #
            # Bug fix (real-world finding: airplane mode killed a phone's radio
            # mid-command; the hub still believed the device was live and this
            # message unconditionally blamed "a heavy SPA hydrating" -- actively
            # misleading when the real cause was a dead connection). Diagnose
            # from the record's OWN silence at the moment of timeout instead of
            # assuming one cause. Compared against `LIVE_SILENCE_TIMEOUT_SECONDS`
            # (tiers.py) -- the SAME threshold that demotes a stale-but-open
            # socket out of Tier.LIVE -- rather than against this command's own
            # `effective_timeout`: a short caller-chosen timeout (1s, 5s) racing
            # a merely slow page is not evidence of connectivity loss, but total
            # silence past the tiering threshold is, regardless of how this
            # command's own timeout was configured.
            silence = record.seconds_since_last_seen
            if silence is not None and silence >= LIVE_SILENCE_TIMEOUT_SECONDS:
                cause_hint = (
                    f" The device has not been heard from (no heartbeat, result, or event) in "
                    f"{silence:.0f}s -- consistent with a lost connection (e.g. radio disabled, "
                    "network dropped) rather than a slow page; check `devices`/`doctor` for this "
                    "device's current tier."
                )
            else:
                cause_hint = " The page may still be loading or a heavy SPA may still be hydrating."
            return {
                "ok": False,
                "error": (
                    f"timeout waiting {effective_timeout}s for device result on command "
                    f"'{cmd.command}' (device={record.device_id}, tab_id={cmd.target.tab_id})."
                    f"{cause_hint} Raise "
                    f"the limit for just this command with args.timeout_s=<seconds> (CLI: "
                    f"--timeout <seconds>; MCP tools: timeout_s param), up to {MAX_COMMAND_TIMEOUT}s, "
                    "or raise the hub's own default with `amplifier-browser-bridge hub --command-timeout <seconds>`."
                    f"{self._timeout_hint(cmd.command, cmd.args)}"
                ),
            }
        except ConnectionError as e:
            self._inflight.pop(cmd.id, None)
            return {"ok": False, "error": str(e)}

        # "effects" (D3 attribution, effects.py) must survive this filter --
        # discovered by the incident-replay test (tests/test_incident_replay.py,
        # FIX 2): `_ingest_result` attaches it to `result_env` for every
        # STATE_CHANGING_COMMANDS result, but this method was dropping it again
        # on the immediate/live-dispatch response path (it survived only on the
        # `poll` path, via `record.results[cmd_id]` storing the full env before
        # this filter ever runs). Silently losing attribution on the path an
        # agent actually calls synchronously is exactly the "no indication
        # anything happened" defect docs/designs/confirmation-gate.md section
        # 1 exists to close -- it must not reappear one layer down.
        return {k: v for k, v in result_env.items() if k in ("ok", "result", "error", "effects")}

    # ------------------------------------------------------------------
    # Kill switch -- design doc §6.2: "Revocation: disable the extension, or a
    # hub-level stop-all. Both immediate." The flag itself lives on the policy
    # engine (checked first thing in `PolicyEngine.evaluate`, so it denies any
    # new dispatch); draining already-queued commands lives here because it
    # needs the DeviceRegistry, which the policy engine deliberately does not
    # depend on (see policy.py's module docstring on why PolicyEngine only ever
    # trusts its own observations, not registry/agent state).
    # ------------------------------------------------------------------

    def engage_kill_switch(self) -> int:
        """Halt all future dispatch immediately, and reject (not silently drop)
        every command currently sitting in a per-device queue -- `poll()` on a
        rejected command_id returns a clear `ok: False` rather than leaving the
        caller to wonder why it never drained. Returns the number rejected.

        Does NOT recall a command already in flight to a device (already sent
        over the wire, awaiting that device's `result`) -- physically
        impossible to un-send a frame that already left the hub. See
        docs/POLICY.md for the honest accounting of what "immediate" covers.
        """
        self.policy.engage_kill_switch()
        rejected = 0
        for record in self.registry.all():
            while True:
                cmd = record.queue.pop_next()
                if cmd is None:
                    break
                self._inflight.pop(cmd.id, None)
                record.results[cmd.id] = {
                    "v": PROTOCOL_VERSION,
                    "id": cmd.id,
                    "type": "result",
                    "ok": False,
                    "error": "kill switch engaged: queued command rejected",
                }
                self.audit.record(
                    "kill_switch_rejected",
                    device_id=record.device_id,
                    command_id=cmd.id,
                    command=cmd.command,
                )
                rejected += 1
        self.audit.record("kill_switch_engaged", rejected=rejected)
        return rejected

    def disengage_kill_switch(self) -> None:
        self.policy.disengage_kill_switch()
        self.audit.record("kill_switch_disengaged")

    # ------------------------------------------------------------------
    # CDP soft-detach -- design doc §6.3/§7: "so the banner clears while the
    # human is just browsing." Idle tracking lives entirely on the hub side
    # (`CdpRegistry`), so this is testable without real sleeps -- see
    # tests/test_cdp.py, which shortens `cdp_idle_seconds` and calls
    # `soft_detach_idle_tabs()` directly rather than running `soft_detach_loop`.
    # ------------------------------------------------------------------

    async def soft_detach_idle_tabs(self, *, now: Any = None) -> list[tuple[str, int]]:
        """One sweep: detach every CDP-attached tab idle past the configured
        threshold. Returns the (device_id, tab_id) pairs actually detached.
        Safe to call repeatedly and safe to call directly in tests (with an
        injected `now`) for a deterministic proof without waiting out a real
        idle window."""
        detached: list[tuple[str, int]] = []
        for device_id, tab_id in self.cdp.idle_tabs(now=now):
            record = self.registry.get(device_id)
            if record is None or not record.connected:
                continue
            detach_cmd = QueuedCommand(
                id=new_id(),
                target=Target(device_id=device_id, tab_id=tab_id),
                command="detach",
                args={"reason": "idle"},
            )
            result = await self._send_and_await(record, detach_cmd)
            if result.get("ok"):
                detached.append((device_id, tab_id))
        return detached

    async def soft_detach_loop(self, *, interval_seconds: float | None = None) -> None:
        """Background task started by `build_app`'s `on_startup` hook --
        periodically sweeps for idle CDP sessions. Not used by tests, which
        call `soft_detach_idle_tabs()` directly instead of running a real
        timer loop."""
        interval = interval_seconds if interval_seconds is not None else self.soft_detach_sweep_interval
        while True:
            await asyncio.sleep(interval)
            try:
                await self.soft_detach_idle_tabs()
            except Exception:
                logger.exception("soft-detach sweep failed")

    # ------------------------------------------------------------------
    # Keepalive sweep -- closes the structural gap this fix exists for: the
    # hub previously only ever INFERRED a dead connection (tiers.py's
    # `compute_tier` demoting a silent-but-open socket out of Tier.LIVE).
    # Nothing ever actively detected one -- the documented "hub pings every
    # 20s" keepalive (docs/PROTOCOL.md, docs/designs/browser-bridge.md \u00a74)
    # was never implemented; `_handle_device_ws` explicitly disabled
    # aiohttp's own transport-level heartbeat in favour of an app-level one
    # that didn't exist yet. See DEFAULT_KEEPALIVE_INTERVAL_SECONDS above for
    # why app-level (not aiohttp's `heartbeat=`) was the right call, and
    # `_handle_device_ws`'s exit-handler comment for the race this sweep
    # required fixing first: proactively closing connections makes the
    # "does this close belong to the connection the record still holds"
    # question come up far more often than waiting on the OS ever did.
    # ------------------------------------------------------------------

    async def keepalive_sweep(self) -> list[str]:
        """One sweep across every currently-connected device.

        For a device heard from recently: send a `ping` (docs/PROTOCOL.md's
        `ping` entry) -- the extension's `background.js` already replies to
        this with a `heartbeat`, the same message its own 15s timer sends,
        so this requires no extension-side change.

        For a device that has gone silent past `LIVE_SILENCE_TIMEOUT_SECONDS`
        (tiers.py) despite that ping: proactively close the connection --
        detection, not merely the existing distrust. Deliberately reuses
        `LIVE_SILENCE_TIMEOUT_SECONDS`, the SAME threshold `compute_tier`
        already demotes a stale socket at, rather than inventing a second
        number: below it, a connected socket is still trusted; above it, the
        hub now backs that distrust with an actual teardown instead of only
        an internal reclassification. That threshold is 4x the measured
        healthy heartbeat ceiling (15.1s) -- generous enough to absorb
        ordinary jitter, but well under the shortest interval a mobile
        device would need to notice and reconnect on its own.

        This method does NOT call `record.unbind()` itself. Closing a
        `WebSocketResponse` here makes the SAME `_handle_device_ws`
        coroutine that owns this connection's `async for msg in ws` loop
        observe the close and run its own (race-guarded, see that method's
        comment) post-loop cleanup -- unbind, `device_disconnected` audit
        entry, queue left untouched. Keeping unbind() to that single call
        site is what the race guard depends on: exactly one code path ever
        sets `record.ws = None`.

        Any command already queued for a device closed this way is
        unaffected -- `DeviceCommandQueue` lives on the record, survives
        `unbind()` by design (registry.py), and drains automatically the
        next time that device's `hello` rebinds the record
        (`_handle_device_message`'s `hello` branch already calls
        `_drain_queue`; proven end-to-end by the airplane-mode fix this PR
        builds on).

        Returns the device_ids proactively closed this sweep (tests and
        diagnostics; not part of the wire protocol).
        """
        closed: list[str] = []
        for record in self.registry.all():
            if not record.connected:
                continue
            assert record.ws is not None  # `connected` implies this
            silence = record.seconds_since_last_seen
            if silence is not None and silence >= LIVE_SILENCE_TIMEOUT_SECONDS:
                self.audit.record(
                    "keepalive_timeout_closing",
                    device_id=record.device_id,
                    silence_seconds=silence,
                )
                logger.info(
                    "closing silent device connection: %s (no traffic in %.0fs, threshold %.0fs)",
                    record.device_id,
                    silence,
                    LIVE_SILENCE_TIMEOUT_SECONDS,
                )
                try:
                    await record.ws.close()
                except Exception:
                    # The transport may already be dead in exactly the way
                    # that got us here -- closing an already-broken socket
                    # must not crash the sweep. `_handle_device_ws`'s own
                    # loop will still notice and clean up independently.
                    logger.exception("close() raised for already-silent device %s", record.device_id)
                closed.append(record.device_id)
                continue
            try:
                await record.ws.send_json({"v": PROTOCOL_VERSION, "id": new_id(), "type": "ping"})
            except ConnectionResetError:
                pass  # transport died between the `connected` check and this send;
                # next sweep's silence check (or the natural disconnect handler) will catch it.
        return closed

    async def keepalive_loop(self, *, interval_seconds: float | None = None) -> None:
        """Background task started by `build_app`'s `on_startup` hook --
        periodically runs `keepalive_sweep`. Not used by tests, which call
        `keepalive_sweep()` directly instead of running a real timer loop."""
        interval = interval_seconds if interval_seconds is not None else self.keepalive_interval
        while True:
            await asyncio.sleep(interval)
            try:
                await self.keepalive_sweep()
            except Exception:
                logger.exception("keepalive sweep failed")

    # ------------------------------------------------------------------
    # Devices snapshot -- enriches DeviceRegistry.snapshot() with per-tab CDP
    # attach state (design doc §7: "report CDP attach state per tab so an
    # agent can reason about it"). Separate from DeviceRecord.to_summary()
    # (registry.py) because CDP state is owned by Hub (via CdpRegistry), not
    # by the registry -- see cdp.py's module docstring on why.
    # ------------------------------------------------------------------

    def _devices_snapshot(self) -> list[dict[str, Any]]:
        # Build-freshness handshake (build_stamp.py): this hub's own currently
        # expected stamp is the SAME value for every device in this snapshot
        # (it depends only on this hub's own extension source, never on any
        # one device), so it's computed once per call rather than once per
        # device. Computed fresh each call (see `current_build_stamp`'s
        # docstring) -- cheap (a couple dozen small file reads), and never
        # allowed to take down the whole listing: a hub-side source problem
        # is named via `hub_error` on every entry instead of raising out of
        # `_devices_snapshot` (which previously had zero filesystem
        # dependency -- `browser_devices` must keep working even when this
        # hub's own extension source is broken).
        try:
            hub_stamp: str | None = current_build_stamp()
            hub_stamp_error: str | None = None
        except (ExtensionSourceNotFoundError, BuildStampError) as e:
            hub_stamp = None
            hub_stamp_error = str(e)

        summaries = self.registry.snapshot()
        for summary in summaries:
            summary["cdp"] = self.cdp.snapshot(summary["device_id"])
            # Tier 0 handshake (skew.py): compare THIS device's self-reported
            # `commands` against the hub's own vocabulary (`COMMANDS`) and
            # attach the result -- surfaced in every `devices`/
            # `browser_devices` listing so an agent (or `update_extension.py`,
            # over the same wire response) can see a stale device BEFORE
            # ever failing a command against it. Comparison lives here, not
            # on `DeviceRecord`, for the same reason `cdp` is enriched here:
            # both compare a device against HUB-owned state (registry.py's
            # module docstring pattern).
            raw_commands = summary["commands"]
            device_commands = frozenset(raw_commands) if raw_commands is not None else None
            summary["skew"] = _skew_compute(device_commands, COMMANDS).to_summary()
            # Build-freshness handshake (build_stamp.py) -- the sibling of
            # `skew` above: "is this device running the CURRENT build" is a
            # DIFFERENT question from "what can this device DO", and a
            # device can be command-complete (skew.in_sync) while still
            # running a stale build (a bug/UI/security fix that touched zero
            # commands -- see build_stamp.py's module docstring for the real
            # incident that motivated this).
            summary["build_freshness"] = _freshness_compute(
                summary["build_stamp"], hub_stamp, hub_error=hub_stamp_error
            ).to_summary()
        return summaries
