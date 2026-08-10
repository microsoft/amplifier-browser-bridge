"""Run the hub as a system service (systemd --user on Linux, launchd on macOS) so it
survives logout and reboot instead of living in a terminal the user has to keep open.

Modeled on a sibling project's working, shipped implementation of exactly this
(muxplex/service.py) -- see that module's docstring and its own test suite for the
traps a service-management module of this shape has already hit in production. Kept
from it, deliberately:

  - systemd **--user** (never a system-wide unit, never sudo). A browser-remote-control
    hub running as root would be indefensible -- this stays scoped to the invoking
    user's own systemd/launchd instance, same as everything else this project does.
  - launchd's ``ProgramArguments`` needs each argv token as its OWN ``<string>``;
    launchd does not shell-split inside one, so an element like
    ``"amplifier-browser-bridge hub"`` is treated as a literal (nonexistent) executable
    name and the job silently fails to start. See `_resolve_hub_bin_tokens`.
  - Gate every systemd operation on `systemctl` actually being on PATH -- never assume
    a Linux box uses systemd (containers, WSL without systemd enabled, other init
    systems all exist).
  - bootout-and-wait before bootstrap on macOS, so a restart/reinstall can't race
    launchd's asynchronous teardown and silently leave the OLD process serving one of
    the exact production bugs the sibling project's test suite documents.

Deliberately DIFFERENT from that sibling project, because this hub's install-time
inputs differ from its ``TMUX_TMPDIR`` problem:

  - That project's core gap was propagating an *ambient* environment variable that has
    no CLI flag of its own -- so it had no choice but to bake `Environment=`/
    `EnvironmentVariables` lines into the unit/plist and hope the service manager's
    "don't inherit the installer's shell" behavior didn't silently drop it.
    This hub's three install-time choices -- host, port, and the token file's PATH --
    each already have first-class CLI flags (`hub --host/--port/--token-file`).
    Baking them in as explicit ExecStart/ProgramArguments ARGUMENTS, not environment
    variables, sidesteps "services don't inherit the installer's shell environment"
    entirely for these values, rather than replicating env-var propagation for
    something that doesn't need it. (`PATH` is still propagated as an environment
    line -- the hub binary itself must be found on it.)
  - The token FILE PATH is baked in, but its CONTENTS are not: rotating a token
    (`amplifier-browser-bridge init --force`) rewrites the SAME file in place, so the
    running service only needs a restart (`service restart`) to pick up the new
    value -- never a reinstall. See `service_install`'s docstring for the one case
    that DOES need a reinstall (host/port changing).
  - Adds `describe_service`, a read-only structured probe `doctor.py` uses to tell
    "service installed but not running" apart from "hub genuinely misconfigured"
    instead of a bare connection-refused failure with no explanation.
  - Names Windows as explicitly, honestly unsupported in this release
    (`_WINDOWS_UNSUPPORTED_DETAIL`) rather than leaving a silent gap or quietly
    falling back to a foreground process that isn't actually a service.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from xml.sax.saxutils import escape as _xml_escape

SERVICE_NAME = "amplifier-browser-bridge"

# Default audit log location when installing as a service and no --audit-log is
# given. Deliberately NOT the hub CLI's own default (`./amplifier-browser-bridge-audit.jsonl`,
# a path relative to whatever the current directory happens to be) -- a long-running
# service has no meaningful "current directory," and relying on one is exactly the
# kind of ambient-environment assumption this module exists to avoid. Always resolved
# to an absolute path and baked into the unit/plist explicitly (see `service_install`).
DEFAULT_SERVICE_AUDIT_LOG = Path("~/.local/share/amplifier-browser-bridge/hub-audit.jsonl")

_SYSTEMD_UNIT_DIR: Path = Path.home() / ".config" / "systemd" / "user"
_SYSTEMD_UNIT_PATH: Path = _SYSTEMD_UNIT_DIR / f"{SERVICE_NAME}.service"

_LAUNCHD_LABEL: str = f"com.{SERVICE_NAME}"
_LAUNCHD_PLIST_DIR: Path = Path.home() / "Library" / "LaunchAgents"
_LAUNCHD_PLIST_PATH: Path = _LAUNCHD_PLIST_DIR / f"{_LAUNCHD_LABEL}.plist"
_LAUNCHD_LOG_DIR: Path = Path.home() / "Library" / "Logs" / SERVICE_NAME

_SYSTEMD_UNIT_TEMPLATE = """\
[Unit]
Description=Amplifier Browser Bridge hub
After=network.target

[Service]
Type=simple
ExecStart={exec_start}
Restart=on-failure
RestartSec=5s
TimeoutStopSec=10
KillMode=process
Environment=PATH={safe_path}
# Every path this hub needs (token file, audit log) is passed as an explicit
# absolute-path ARGUMENT above, never a relative one -- so this WorkingDirectory
# is belt-and-suspenders, not load-bearing. Set anyway so a future relative-path
# argument fails predictably (the user's home) instead of wherever systemd's own
# default happens to be.
WorkingDirectory=%h

[Install]
WantedBy=default.target
"""

_LAUNCHD_PLIST_TEMPLATE = """\
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{label}</string>
    <key>ProgramArguments</key>
    <array>
{program_arguments_xml}
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>{safe_path}</string>
    </dict>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>{log_path}</string>
    <key>StandardErrorPath</key>
    <string>{err_path}</string>
</dict>
</plist>
"""

_WINDOWS_UNSUPPORTED_DETAIL = (
    "service management is not implemented for Windows in this release -- there is no "
    "systemd/launchd equivalent this module drives there yet. Run `amplifier-browser-bridge "
    "hub ...` directly instead, or wrap it yourself as a real Windows service (Task "
    "Scheduler set to run at log on, or NSSM/WinSW). See INSTALL.md's Windows section."
)


class ServiceUnsupportedError(RuntimeError):
    """Raised when a service operation is requested on a platform/configuration this
    module cannot drive (Windows, or Linux without a USABLE `systemctl --user`
    session -- see `_systemctl_user_probe`). Never a silent no-op or a quiet
    fallback to a foreground process -- see module docstring.
    """


class ServiceInstallError(RuntimeError):
    """Raised when the platform/configuration DOES look like it can run a service
    (a usable `systemctl --user` session, or launchd on macOS), but the install
    itself failed for some other reason -- a `systemctl`/`launchctl` command
    rejected by the service manager, a malformed unit, etc.

    Deliberately distinct from `ServiceUnsupportedError`: that one means "this
    machine cannot run a service at all, don't bother retrying the same way";
    this one means "the environment looked capable, this specific attempt
    failed, and the underlying `subprocess.CalledProcessError`/`RuntimeError`
    is attached via `__cause__` for anyone who wants the raw detail." Both are
    caught the same way by callers that only care about "service didn't get
    installed, degrade honestly" (see `auto_setup.run_auto_setup`) -- the
    split exists for anyone who wants to tell the two situations apart, not to
    force every caller to.

    Constructed at the `service_install()` boundary (never inside
    `_systemd_install`/`_launchd_install` themselves) so this is the ONE place
    a raw `subprocess.CalledProcessError` or `launchctl` `RuntimeError` gets
    translated -- every caller of `service_install()` (this CLI's own `service
    install` command, `init`, and `auto_setup.run_auto_setup`) is protected by
    this single conversion point, not by each caller separately remembering to
    catch a lower-level exception type.
    """


@dataclass
class ServiceInfo:
    """Structured, read-only description of the current service state -- what
    `amplifier-browser-bridge service status` prints for a human, and what `doctor.py`
    consumes to tell "installed but not running" apart from "never installed" or
    "this platform can't run a service at all."
    """

    platform: str  # "linux", "darwin", "windows", or "other"
    supported: bool
    installed: bool
    active: bool | None  # None when not installed, or state genuinely unknown
    unit_path: Path | None
    detail: str


# ---------------------------------------------------------------------------
# Platform detection
# ---------------------------------------------------------------------------


def _is_darwin() -> bool:
    return sys.platform == "darwin"


def _is_windows() -> bool:
    return sys.platform == "win32"


def _systemctl_user_probe() -> tuple[bool, str]:
    """Whether `systemctl --user` is actually USABLE, not merely present on PATH.

    Presence of the binary is not evidence a user service can be installed.
    Measured, not theoretical: a container can ship `systemctl` on PATH with no
    user D-Bus session at all -- every `--user` operation then fails with
    ``Failed to connect to bus: No medium found``, which `shutil.which` alone
    cannot see (containers, WSL1, minimal init environments are all real cases
    of this, not edge cases).

    Probed with a read-only, side-effect-free call (`--user list-units`) rather
    than trusting binary presence -- this is the ONE place that decides whether
    every other systemd operation in this module is attempted at all, so a
    false positive here is what let a raw `CalledProcessError` (from
    `daemon-reload` failing inside `_systemd_install`) escape as an unhandled
    exception instead of the honest `ServiceUnsupportedError` a caller like
    `auto_setup.run_auto_setup` already knows how to degrade from.

    Returns `(usable, detail)` -- `detail` explains what was actually observed
    either way, so `_no_systemctl_detail()` can report the REAL reason (binary
    missing vs. binary present but no usable session) instead of a message
    that's only accurate for the first case.
    """
    which = shutil.which("systemctl")
    if which is None:
        return False, "the `systemctl` binary was not found on PATH"
    try:
        result = subprocess.run(
            ["systemctl", "--user", "list-units", "--no-legend", "--no-pager"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, f"`systemctl --user` could not be run: {e}"
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        return False, (
            "`systemctl` is on PATH, but `systemctl --user` failed"
            + (f": {stderr}" if stderr else f" (exit {result.returncode})")
            + " -- there is likely no user D-Bus session available here (common in containers, "
            "WSL1, or other minimal init environments)."
        )
    return True, ""


def _have_systemctl() -> bool:
    """Gates every systemd operation -- never assume a Linux box uses systemd, and
    never assume the `systemctl` binary being on PATH means a usable user service
    manager. See `_systemctl_user_probe`."""
    return _systemctl_user_probe()[0]


# ---------------------------------------------------------------------------
# Binary resolution
# ---------------------------------------------------------------------------


def _resolve_hub_bin() -> str:
    """The `amplifier-browser-bridge` executable to invoke from a systemd unit.

    Prefers the executable on PATH (what `uv tool install` puts on it); falls back to
    an explicit `<python> -m amplifier_browser_bridge.cli` invocation. Returned as a
    single string -- safe for systemd's ExecStart, which does its own whitespace
    splitting (unlike launchd, see `_resolve_hub_bin_tokens`).
    """
    which = shutil.which(SERVICE_NAME)
    if which:
        return which
    return f"{sys.executable} -m amplifier_browser_bridge.cli"


def _resolve_hub_bin_tokens() -> list[str]:
    """The `amplifier-browser-bridge` executable as separate argv tokens, for launchd.

    launchd's ProgramArguments does NOT shell-split inside a single ``<string>`` --
    an element like ``"python3 -m amplifier_browser_bridge.cli"`` is a literal
    (nonexistent) executable name, and the job silently fails to start. Each element
    returned here must become its own ``<string>``.

    Prefers the stable `~/.local/bin/amplifier-browser-bridge` console-script symlink
    a `uv tool install` creates (survives `uv tool install --reinstall`); falls back to
    a PATH lookup, then to an explicit, correctly-split `[python, -m, ...]` invocation.
    """
    local_bin = Path.home() / ".local" / "bin" / SERVICE_NAME
    if local_bin.exists() and os.access(str(local_bin), os.X_OK):
        return [str(local_bin)]
    which = shutil.which(SERVICE_NAME)
    if which:
        return [which]
    return [sys.executable, "-m", "amplifier_browser_bridge.cli"]


def _hub_argv_tail(
    host: str,
    port: int,
    token_file: Path,
    audit_log: Path | None,
    command_timeout: float | None,
    android_artifact: Path | None = None,
) -> list[str]:
    """The `hub` subcommand and its arguments, explicit and absolute -- never an
    environment variable a service manager might not propagate. Shared by both the
    systemd (joined into one ExecStart string) and launchd (kept as separate argv
    tokens) install paths so the two can never drift apart on what gets baked in."""
    argv = ["hub", "--host", host, "--port", str(port), "--token-file", str(token_file)]
    if audit_log is not None:
        argv += ["--audit-log", str(audit_log)]
    if command_timeout is not None:
        argv += ["--command-timeout", str(command_timeout)]
    if android_artifact is not None:
        argv += ["--android-artifact", str(android_artifact)]
    return argv


def _resolve_service_audit_log(audit_log: str | Path | None) -> Path:
    """Absolute audit-log path to bake into the unit/plist. See
    `DEFAULT_SERVICE_AUDIT_LOG`'s docstring for why this is never left to default to
    the hub CLI's own cwd-relative default when running as a service."""
    if audit_log is not None:
        return Path(audit_log).expanduser().resolve()
    return DEFAULT_SERVICE_AUDIT_LOG.expanduser()


# ---------------------------------------------------------------------------
# systemd (Linux)
# ---------------------------------------------------------------------------


def _systemd_install(
    host: str,
    port: int,
    token_file: Path,
    audit_log: Path | None,
    command_timeout: float | None,
    android_artifact: Path | None = None,
) -> None:
    exec_argv = [
        _resolve_hub_bin(),
        *_hub_argv_tail(host, port, token_file, audit_log, command_timeout, android_artifact),
    ]
    exec_start = shlex.join(exec_argv)
    safe_path = os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")
    unit_content = _SYSTEMD_UNIT_TEMPLATE.format(exec_start=exec_start, safe_path=safe_path)

    _SYSTEMD_UNIT_DIR.mkdir(parents=True, exist_ok=True)
    _SYSTEMD_UNIT_PATH.write_text(unit_content, encoding="utf-8")
    try:
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as e:
        # systemd never actually loaded what we just wrote -- `daemon-reload`
        # failing means this unit file has zero effect on the running system. Left
        # on disk, it would make every later `describe_service()`/`doctor` call
        # report "installed but NOT active" forever after, even though this
        # install attempt never got as far as systemd's knowledge of it at all.
        # That is precisely the misleading state a real (measured, not
        # theoretical) DTU run surfaced: a stale unit file outliving a failed
        # install, contradicting the hub's actual (unrelated) reachability.
        # Roll back so the on-disk state matches reality: never installed.
        _SYSTEMD_UNIT_PATH.unlink(missing_ok=True)
        raise ServiceInstallError(_describe_systemctl_failure(e, step="daemon-reload")) from e

    try:
        subprocess.run(
            ["systemctl", "--user", "enable", "--now", SERVICE_NAME],
            check=True,
            capture_output=True,
            text=True,
        )
        # `enable --now` is a no-op on an already-running unit, so a re-install (new
        # host/port, rotated audit-log path, ...) would silently keep serving the STALE
        # arguments without this. `restart` also starts a stopped unit, so it is safe on
        # both first install and re-install.
        subprocess.run(
            ["systemctl", "--user", "restart", SERVICE_NAME], check=True, capture_output=True, text=True
        )
    except subprocess.CalledProcessError as e:
        # Past this point systemd DOES know about the new unit -- daemon-reload
        # above already succeeded. No rollback here: whatever `describe_service()`
        # reports from here on (installed, not-yet-active, failed) reflects what
        # systemd actually has loaded, so it is accurate information rather than a
        # stale artifact -- unlike the daemon-reload failure above.
        raise ServiceInstallError(_describe_systemctl_failure(e, step="enable/restart")) from e


def _systemd_uninstall() -> None:
    # stop/disable are intentionally NOT check=True -- uninstalling an already-stopped
    # or never-enabled unit is a normal, successful uninstall, not an error.
    subprocess.run(["systemctl", "--user", "stop", SERVICE_NAME], check=False)
    subprocess.run(["systemctl", "--user", "disable", SERVICE_NAME], check=False)
    _SYSTEMD_UNIT_PATH.unlink(missing_ok=True)
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)


def _systemd_start() -> None:
    subprocess.run(["systemctl", "--user", "start", SERVICE_NAME], check=True)


def _systemd_stop() -> None:
    # Not check=True -- stopping an already-stopped service is a normal no-op.
    subprocess.run(["systemctl", "--user", "stop", SERVICE_NAME], check=False)


def _systemd_restart() -> None:
    subprocess.run(["systemctl", "--user", "restart", SERVICE_NAME], check=True)


def _systemd_status() -> None:
    # Not check=True -- a stopped/failed unit is a normal `status` outcome (nonzero
    # exit), not a reason to raise.
    subprocess.run(["systemctl", "--user", "status", SERVICE_NAME, "--no-pager"], check=False)


def _systemd_logs() -> None:
    try:
        subprocess.run(["journalctl", "--user", "-u", SERVICE_NAME, "-f"], check=False)
    except KeyboardInterrupt:
        pass


def _systemd_describe() -> ServiceInfo:
    installed = _SYSTEMD_UNIT_PATH.is_file()
    active: bool | None = None
    if installed:
        result = subprocess.run(
            ["systemctl", "--user", "is-active", SERVICE_NAME], capture_output=True, text=True, check=False
        )
        active = result.stdout.strip() == "active"
    if not installed:
        detail = f"not installed (would install at {_SYSTEMD_UNIT_PATH})"
    elif active:
        detail = f"installed and active (unit: {_SYSTEMD_UNIT_PATH})"
    else:
        detail = f"installed but NOT active (unit: {_SYSTEMD_UNIT_PATH})"
    return ServiceInfo(
        platform="linux",
        supported=True,
        installed=installed,
        active=active,
        unit_path=_SYSTEMD_UNIT_PATH if installed else None,
        detail=detail,
    )


# ---------------------------------------------------------------------------
# launchd (macOS)
# ---------------------------------------------------------------------------


def _launchd_is_loaded(uid: int) -> bool:
    """True if launchd currently knows about our label."""
    return (
        subprocess.run(
            ["launchctl", "print", f"gui/{uid}/{_LAUNCHD_LABEL}"], capture_output=True, check=False
        ).returncode
        == 0
    )


def _launchd_bootout_and_wait(uid: int, *, timeout: float = 10.0) -> bool:
    """bootout the job and WAIT for launchd to actually finish tearing it down.

    `launchctl bootout` returns before the job is gone. Not waiting is the exact bug
    that made a sibling project's `service restart` a silent no-op: bootstrap raced
    the teardown, saw the OLD job still loaded, and reported success while the old
    process kept serving. Stop must mean stopped before start can mean started.

    Returns True if the job is confirmed gone, False if it outlived the timeout.
    """
    subprocess.run(["launchctl", "bootout", f"gui/{uid}/{_LAUNCHD_LABEL}"], capture_output=True, check=False)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _launchd_is_loaded(uid):
            return True
        time.sleep(0.25)
    return not _launchd_is_loaded(uid)


def _launchd_bootstrap(uid: int, *, attempts: int = 6, accept_already_loaded: bool = False) -> None:
    """bootstrap the plist, retrying through launchd's asynchronous teardown.

    Exit 5 ("Input/output error") right after a bootout is the teardown race, not a
    real failure, so it is retried.

    `accept_already_loaded` is the load-bearing distinction. For `start` -- "make sure
    it is running" -- finding it already loaded IS the desired state. For `install`
    and `restart` the caller has just booted the job out and is replacing it, so an
    already-loaded job means the OLD one survived; reporting success there is a lie
    that hides a failed upgrade. Only `start` opts in.

    Real failures fail LOUDLY, with launchd's own stderr and an actionable hint rather
    than a raw CalledProcessError traceback.
    """
    last = None
    for attempt in range(attempts):
        if attempt:
            time.sleep(0.5)
        last = subprocess.run(
            ["launchctl", "bootstrap", f"gui/{uid}", str(_LAUNCHD_PLIST_PATH)],
            capture_output=True,
            text=True,
            check=False,
        )
        if last.returncode == 0:
            return
        # Exit 5 (EIO) and 37 are the teardown race; retry. Anything else is a
        # genuine error and retrying only delays the report.
        if last.returncode not in (5, 37):
            break

    if accept_already_loaded and _launchd_is_loaded(uid):
        return

    detail = (last.stderr or last.stdout or "").strip() if last else ""
    code = last.returncode if last else "unknown"
    raise RuntimeError(
        f"launchctl bootstrap failed (exit {code})"
        + (f": {detail}" if detail else "")
        + f"\n  The service plist is at {_LAUNCHD_PLIST_PATH}."
        + f"\n  Try: launchctl bootout gui/{uid}/{_LAUNCHD_LABEL} && "
        + f"launchctl bootstrap gui/{uid} {_LAUNCHD_PLIST_PATH}"
        + "\n  Or run 'amplifier-browser-bridge hub' directly to start without a service manager."
    )


def _launchd_install(
    host: str,
    port: int,
    token_file: Path,
    audit_log: Path | None,
    command_timeout: float | None,
    android_artifact: Path | None = None,
) -> None:
    bin_tokens = _resolve_hub_bin_tokens()
    argv = bin_tokens + _hub_argv_tail(host, port, token_file, audit_log, command_timeout, android_artifact)
    # Each argv token is its own <string> element. launchd does NOT shell-split
    # inside a <string>, so the whole command must NEVER be put into one element.
    program_arguments_xml = "\n".join(f"        <string>{_xml_escape(arg)}</string>" for arg in argv)
    base_path = os.environ.get("PATH", "/usr/bin:/bin")
    safe_path = f"/opt/homebrew/bin:/usr/local/bin:{base_path}"

    _LAUNCHD_LOG_DIR.mkdir(parents=True, exist_ok=True)
    plist_content = _LAUNCHD_PLIST_TEMPLATE.format(
        label=_LAUNCHD_LABEL,
        program_arguments_xml=program_arguments_xml,
        safe_path=safe_path,
        log_path=str(_LAUNCHD_LOG_DIR / "hub.log"),
        err_path=str(_LAUNCHD_LOG_DIR / "hub.err"),
    )
    _LAUNCHD_PLIST_DIR.mkdir(parents=True, exist_ok=True)
    _LAUNCHD_PLIST_PATH.write_text(plist_content, encoding="utf-8")

    uid = os.getuid()
    # bootstrap on an already-loaded label fails with EEXIST-style errors, so bootout
    # first (ignore failure if it wasn't loaded) to force the new plist's arguments
    # (e.g. an updated host/port) to actually apply on re-install, not just on first
    # install.
    _launchd_bootout_and_wait(uid)
    _launchd_bootstrap(uid)


def _launchd_uninstall() -> None:
    uid = os.getuid()
    # Not check=True -- bootout on an already-unloaded (or never-loaded) label is a
    # normal, successful uninstall, not an error.
    subprocess.run(["launchctl", "bootout", f"gui/{uid}/{_LAUNCHD_LABEL}"], check=False)
    _LAUNCHD_PLIST_PATH.unlink(missing_ok=True)


def _launchd_start() -> None:
    _launchd_bootstrap(os.getuid(), accept_already_loaded=True)


def _launchd_stop() -> None:
    _launchd_bootout_and_wait(os.getuid())


def _launchd_restart() -> None:
    uid = os.getuid()
    if not _launchd_bootout_and_wait(uid):
        raise RuntimeError(
            f"launchctl bootout did not release {_LAUNCHD_LABEL} within the timeout, "
            f"so restarting would leave the OLD process running.\n"
            f"  Check it: launchctl print gui/{uid}/{_LAUNCHD_LABEL}\n"
            f"  Then:     launchctl bootout gui/{uid}/{_LAUNCHD_LABEL}"
        )
    _launchd_bootstrap(uid)


def _launchd_status() -> None:
    subprocess.run(["launchctl", "print", f"gui/{os.getuid()}/{_LAUNCHD_LABEL}"], check=False)


def _launchd_logs() -> None:
    try:
        subprocess.run(["tail", "-f", str(_LAUNCHD_LOG_DIR / "hub.log")], check=False)
    except KeyboardInterrupt:
        pass


def _launchd_describe() -> ServiceInfo:
    installed = _LAUNCHD_PLIST_PATH.is_file()
    active = _launchd_is_loaded(os.getuid()) if installed else None
    if not installed:
        detail = f"not installed (would install at {_LAUNCHD_PLIST_PATH})"
    elif active:
        detail = f"installed and loaded (plist: {_LAUNCHD_PLIST_PATH})"
    else:
        detail = f"installed but NOT loaded (plist: {_LAUNCHD_PLIST_PATH})"
    return ServiceInfo(
        platform="darwin",
        supported=True,
        installed=installed,
        active=active,
        unit_path=_LAUNCHD_PLIST_PATH if installed else None,
        detail=detail,
    )


# ---------------------------------------------------------------------------
# Public API -- platform-dispatching wrappers
# ---------------------------------------------------------------------------


def _no_systemctl_detail() -> str:
    """Explain why `systemctl --user` isn't usable -- accurately either way: the
    binary missing entirely, or present but with no usable user D-Bus session
    (see `_systemctl_user_probe`). Never assumes the former just because that
    used to be the only case this checked."""
    _, reason = _systemctl_user_probe()
    reason = reason or "systemd --user is not usable on this machine"
    return (
        f"service management requires a usable `systemctl --user` session: {reason} Run "
        "`amplifier-browser-bridge hub` directly to start the server without a service manager."
    )


def _describe_systemctl_failure(e: subprocess.CalledProcessError, *, step: str) -> str:
    """Format a `systemctl --user` command failure with its actual stderr, not just
    a bare exit code -- this is what a caller sees inside `ServiceInstallError`."""
    stderr = (e.stderr or "").strip() if isinstance(e.stderr, str) else ""
    detail = f": {stderr}" if stderr else f" (exit {e.returncode})"
    return f"`systemctl --user` failed during {step} ({shlex.join(e.cmd)}){detail}"


def service_install(
    host: str,
    port: int,
    token_file: str | Path,
    *,
    audit_log: str | Path | None = None,
    command_timeout: float | None = None,
    android_artifact: str | Path | None = None,
) -> ServiceInfo:
    """Install (or re-install) the hub service unit for the current user and start it.

    Safe to re-run -- e.g. after this machine's Tailscale IP changes, re-run with the
    new `host` (or let the caller re-detect one) to rebake and restart the unit under
    the new address. A stale IP baked into an old unit does not fail silently: if the
    address is no longer assigned to any interface on this machine, the bind itself
    fails and the service manager reports it as a failed unit (`Restart=on-failure`
    keeps retrying, loudly, rather than pretending to be up) -- see
    `amplifier-browser-bridge service status` / `amplifier-browser-bridge doctor`.

    Does NOT need to be re-run after a token ROTATION (`amplifier-browser-bridge init
    --force`): the token FILE PATH is what gets baked in here, not its contents, so
    `service restart` alone (or the unit's own `Restart=on-failure`) is enough to pick
    up a rotated token.
    """
    token_file_path = Path(token_file).expanduser().resolve()
    resolved_audit_log = _resolve_service_audit_log(audit_log)
    resolved_audit_log.parent.mkdir(parents=True, exist_ok=True)
    android_artifact_path = Path(android_artifact).expanduser().resolve() if android_artifact else None

    if _is_windows():
        raise ServiceUnsupportedError(_WINDOWS_UNSUPPORTED_DETAIL)
    if _is_darwin():
        try:
            _launchd_install(
                host, port, token_file_path, resolved_audit_log, command_timeout, android_artifact_path
            )
        except RuntimeError as e:
            # `_launchd_bootstrap` raises a plain RuntimeError for a genuine
            # launchctl failure (see its own docstring) -- converted to
            # ServiceInstallError HERE, at the one call site every caller of
            # `service_install()` goes through, so this CLI's own `service
            # install`/`init` commands and `auto_setup.run_auto_setup` are all
            # protected by the same conversion rather than each needing to
            # remember `except RuntimeError` (too broad to catch safely on its
            # own) alongside `ServiceUnsupportedError`.
            raise ServiceInstallError(str(e)) from e
        return _launchd_describe()
    if _have_systemctl():
        _systemd_install(
            host, port, token_file_path, resolved_audit_log, command_timeout, android_artifact_path
        )
        return _systemd_describe()
    raise ServiceUnsupportedError(_no_systemctl_detail())


def service_uninstall() -> None:
    """Stop and remove the hub service unit for the current user."""
    if _is_windows():
        raise ServiceUnsupportedError(_WINDOWS_UNSUPPORTED_DETAIL)
    if _is_darwin():
        _launchd_uninstall()
    elif _have_systemctl():
        _systemd_uninstall()
    else:
        raise ServiceUnsupportedError(_no_systemctl_detail())


def service_start() -> None:
    """Start the installed hub service."""
    if _is_windows():
        raise ServiceUnsupportedError(_WINDOWS_UNSUPPORTED_DETAIL)
    if _is_darwin():
        _launchd_start()
    elif _have_systemctl():
        _systemd_start()
    else:
        raise ServiceUnsupportedError(_no_systemctl_detail())


def service_stop() -> None:
    """Stop the hub service without uninstalling it."""
    if _is_windows():
        raise ServiceUnsupportedError(_WINDOWS_UNSUPPORTED_DETAIL)
    if _is_darwin():
        _launchd_stop()
    elif _have_systemctl():
        _systemd_stop()
    else:
        raise ServiceUnsupportedError(_no_systemctl_detail())


def service_restart() -> None:
    """Restart the hub service -- e.g. after rotating the token file's contents."""
    if _is_windows():
        raise ServiceUnsupportedError(_WINDOWS_UNSUPPORTED_DETAIL)
    if _is_darwin():
        _launchd_restart()
    elif _have_systemctl():
        _systemd_restart()
    else:
        raise ServiceUnsupportedError(_no_systemctl_detail())


def service_status() -> None:
    """Print the service manager's own raw status output (for a human at a terminal).

    See `describe_service` for a structured version other code (doctor.py) can
    consume without scraping this text.
    """
    if _is_windows():
        raise ServiceUnsupportedError(_WINDOWS_UNSUPPORTED_DETAIL)
    if _is_darwin():
        _launchd_status()
    elif _have_systemctl():
        _systemd_status()
    else:
        raise ServiceUnsupportedError(_no_systemctl_detail())


def service_logs() -> None:
    """Stream or print the hub service's logs."""
    if _is_windows():
        raise ServiceUnsupportedError(_WINDOWS_UNSUPPORTED_DETAIL)
    if _is_darwin():
        _launchd_logs()
    elif _have_systemctl():
        _systemd_logs()
    else:
        raise ServiceUnsupportedError(_no_systemctl_detail())


def describe_service() -> ServiceInfo:
    """Read-only, side-effect-free (beyond a couple of status subprocess calls)
    description of the current service state. Never raises -- an unsupported
    platform is reported as `supported=False` with `detail` explaining why, not an
    exception, so callers like `doctor.py` can always show something rather than
    needing a try/except around this specific call.
    """
    if _is_windows():
        return ServiceInfo(
            platform="windows",
            supported=False,
            installed=False,
            active=None,
            unit_path=None,
            detail=_WINDOWS_UNSUPPORTED_DETAIL,
        )
    if _is_darwin():
        return _launchd_describe()
    if _have_systemctl():
        return _systemd_describe()
    return ServiceInfo(
        platform="linux" if sys.platform.startswith("linux") else "other",
        supported=False,
        installed=False,
        active=None,
        unit_path=None,
        detail=_no_systemctl_detail(),
    )


__all__ = [
    "DEFAULT_SERVICE_AUDIT_LOG",
    "SERVICE_NAME",
    "ServiceInfo",
    "ServiceInstallError",
    "ServiceUnsupportedError",
    "describe_service",
    "service_install",
    "service_logs",
    "service_restart",
    "service_start",
    "service_status",
    "service_stop",
    "service_uninstall",
]
