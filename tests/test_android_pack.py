"""Tests for android_pack.py -- on-demand Android CRX packing from the hub's
own currently-running token.

CI has no real Chromium/Chrome/Edge binary available (see .github/workflows/ci.yml),
so `build_android_crx`'s pipeline is exercised end-to-end against a FAKE packer
script (a tiny Python script that mimics `--pack-extension`'s observable output
shape: it writes `<stage_dir>.crx` -- and `<stage_dir>.pem` when no key was
supplied -- next to the staged directory it's given). This tests this module's
own plumbing (staging, baking, integrity-checking, invoking the packer,
locating its output, reusing/generating the signing key) without needing a
real, cryptographically valid CRX3 -- that property is `scripts/verify_crx.py`'s
job, exercised separately (and manually, against a real Chromium) as documented
in docs/ANDROID.md.
"""

from __future__ import annotations

import json
import os
import stat
import textwrap
from pathlib import Path

import pytest

from amplifier_browser_bridge.android_pack import (
    ANDROID_STAGE_FILES,
    AndroidPackError,
    PackerUnavailableError,
    build_android_crx,
    find_packer_binary,
    stage_android_extension,
)
from amplifier_browser_bridge.setup import _EXTENSION_FILES

FAKE_PACKER_SCRIPT = textwrap.dedent(
    """\
    #!/usr/bin/env python3
    # Fake --pack-extension: writes <dir>.crx (Cr24 + version + 0-length header +
    # zip bytes of the staged dir) and, if no --pack-extension-key was given,
    # <dir>.pem too -- mirroring real Chromium's observable output shape closely
    # enough to test this module's plumbing (see test_android_pack.py's module
    # docstring for why a real CRX3 signature isn't needed here).
    import io
    import shutil
    import struct
    import sys
    import zipfile
    from pathlib import Path

    stage_dir = None
    has_key = False
    for arg in sys.argv[1:]:
        if arg.startswith("--pack-extension="):
            stage_dir = Path(arg.split("=", 1)[1])
        if arg.startswith("--pack-extension-key="):
            has_key = True

    if stage_dir is None:
        print("no --pack-extension given", file=sys.stderr)
        sys.exit(1)

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

    if not has_key:
        pem_path = stage_dir.parent / (stage_dir.name + ".pem")
        pem_path.write_bytes(b"-----BEGIN FAKE KEY-----\\nnot a real key\\n-----END FAKE KEY-----\\n")

    sys.exit(0)
    """
)


@pytest.fixture
def fake_packer(tmp_path: Path) -> Path:
    script = tmp_path / "fake-chrome.py"
    script.write_text(FAKE_PACKER_SCRIPT, encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    # Re-exec through the current interpreter so this works regardless of what
    # `python3` resolves to on the test host.
    wrapper = tmp_path / "fake-chrome"
    wrapper.write_text(f"#!/usr/bin/env bash\nexec python3 '{script}' \"$@\"\n", encoding="utf-8")
    wrapper.chmod(wrapper.stat().st_mode | stat.S_IEXEC)
    return wrapper


# --- ANDROID_STAGE_FILES -- single-source-of-truth reuse -----------------------


def test_android_stage_files_derived_from_desktop_list_with_manifest_swapped() -> None:
    """The exact bug this derivation prevents: `scripts/package-android.sh`'s
    own hand-maintained copy list drifted out of sync with the desktop list
    (missing pairing_code.mjs/pair_discovery.mjs, both imported by options.js
    since the zero-copy-paste pairing feature landed -- the 87ce68d failure-mode
    class). Deriving from _EXTENSION_FILES structurally prevents that drift."""
    assert "manifest.json" not in ANDROID_STAGE_FILES
    assert "manifest.android.json" in ANDROID_STAGE_FILES
    # Everything else present in the desktop list must also be present here.
    for name in _EXTENSION_FILES:
        if name == "manifest.json":
            continue
        assert name in ANDROID_STAGE_FILES, (
            f"{name} present in desktop list but missing from Android stage list"
        )
    # And specifically: the pairing modules that were the real, shipped bug.
    assert "pairing_code.mjs" in ANDROID_STAGE_FILES
    assert "pair_discovery.mjs" in ANDROID_STAGE_FILES
    assert "connection_error.mjs" in ANDROID_STAGE_FILES


# --- stage_android_extension ----------------------------------------------------


def test_stage_android_extension_renames_manifest_and_passes_integrity(tmp_path: Path) -> None:
    from amplifier_browser_bridge.extension_integrity import verify_extension_integrity

    dest = tmp_path / "staged"
    stage_android_extension(dest)
    assert (dest / "manifest.json").is_file()
    assert not (dest / "manifest.android.json").exists()
    manifest = json.loads((dest / "manifest.json").read_text(encoding="utf-8"))
    assert "debugger" not in manifest.get("permissions", [])
    # The real regression: this must NOT raise (the pre-existing shell script's
    # list would have failed this, missing pairing_code.mjs/pair_discovery.mjs).
    verify_extension_integrity(dest)


# --- find_packer_binary ---------------------------------------------------------


def test_find_packer_binary_prefers_chrome_bin_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = tmp_path / "my-chrome"
    fake.write_text("#!/bin/sh\n", encoding="utf-8")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("CHROME_BIN", str(fake))
    assert find_packer_binary() == fake


def test_find_packer_binary_falls_back_to_playwright_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CHROME_BIN", raising=False)
    monkeypatch.setattr("shutil.which", lambda name: None)
    cache = tmp_path / "ms-playwright"
    older = cache / "chromium-1000" / "chrome-linux" / "chrome"
    newer = cache / "chromium-1234" / "chrome-linux" / "chrome"
    for p in (older, newer):
        p.parent.mkdir(parents=True)
        p.write_text("", encoding="utf-8")
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(cache))
    assert find_packer_binary() == newer  # highest-numbered build wins


def test_find_packer_binary_returns_none_when_nothing_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CHROME_BIN", raising=False)
    monkeypatch.setattr("shutil.which", lambda name: None)
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path / "does-not-exist"))
    assert find_packer_binary() is None


# --- build_android_crx (full pipeline, fake packer) -----------------------------


@pytest.mark.asyncio
async def test_build_android_crx_produces_bytes_with_a_fake_packer(fake_packer: Path, tmp_path: Path) -> None:
    key_path = tmp_path / "signing-key.pem"
    data = await build_android_crx(
        hub_url="ws://100.1.2.3:8900/device",
        hub_token="real-token-value",
        chrome_bin=fake_packer,
        signing_key_path=key_path,
    )
    assert data[:4] == b"Cr24"
    assert key_path.is_file()  # first build: no key existed, packer "generated" one, we saved it


@pytest.mark.asyncio
async def test_build_android_crx_reuses_an_existing_signing_key(fake_packer: Path, tmp_path: Path) -> None:
    key_path = tmp_path / "signing-key.pem"
    key_path.write_bytes(b"-----BEGIN FAKE KEY-----\nexisting\n-----END FAKE KEY-----\n")
    before = key_path.read_bytes()

    await build_android_crx(
        hub_url="ws://100.1.2.3:8900/device",
        hub_token="tok",
        chrome_bin=fake_packer,
        signing_key_path=key_path,
    )

    assert key_path.read_bytes() == before  # never overwritten by a "generated" key


@pytest.mark.asyncio
async def test_build_android_crx_bakes_the_given_url_and_token(fake_packer: Path, tmp_path: Path) -> None:
    """The load-bearing property: the artifact reflects the CALLER's supplied
    hub_url/hub_token (the hub's OWN live token, per android_pack.py's module
    docstring) -- not a value re-derived from disk/environment, which would
    reintroduce exactly the stale-artifact problem this module exists to avoid."""
    import io
    import struct
    import zipfile

    data = await build_android_crx(
        hub_url="ws://100.9.9.9:8900/device",
        hub_token="THE-LIVE-TOKEN",
        chrome_bin=fake_packer,
        signing_key_path=tmp_path / "key.pem",
    )
    header_length = struct.unpack("<I", data[8:12])[0]
    zip_bytes = data[12 + header_length :]
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        config = json.loads(zf.read("bundled_config.json").decode("utf-8"))
    assert config["hubUrl"] == "ws://100.9.9.9:8900/device"
    assert config["hubToken"] == "THE-LIVE-TOKEN"


@pytest.mark.asyncio
async def test_build_android_crx_raises_packer_unavailable_when_nothing_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CHROME_BIN", raising=False)
    monkeypatch.setattr("shutil.which", lambda name: None)
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path / "does-not-exist"))
    with pytest.raises(PackerUnavailableError, match="no Chromium/Chrome/Edge binary"):
        await build_android_crx(hub_url="ws://h:1/device", hub_token="t")


@pytest.mark.asyncio
async def test_build_android_crx_raises_pack_error_when_packer_binary_fails(tmp_path: Path) -> None:
    failing = tmp_path / "always-fails"
    failing.write_text("#!/bin/sh\necho boom >&2\nexit 1\n", encoding="utf-8")
    failing.chmod(failing.stat().st_mode | stat.S_IEXEC)
    with pytest.raises(AndroidPackError, match="exited 1"):
        await build_android_crx(
            hub_url="ws://h:1/device",
            hub_token="t",
            chrome_bin=failing,
            signing_key_path=tmp_path / "key.pem",
        )


def test_default_signing_key_path_matches_package_android_sh_default() -> None:
    """Must stay in sync with scripts/package-android.sh's own KEY_PATH default
    -- see android_pack.py's module docstring: an artifact built by either path
    must get the SAME extension ID, or Android treats a rebuild via the other
    path as a different extension."""
    from amplifier_browser_bridge.android_pack import DEFAULT_SIGNING_KEY_PATH

    assert str(DEFAULT_SIGNING_KEY_PATH) == "~/.config/amplifier-browser-bridge/android-signing-key.pem"


def test_find_packer_binary_env_var_ignores_nonexecutable_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    not_exec = tmp_path / "not-executable"
    not_exec.write_text("", encoding="utf-8")
    os.chmod(not_exec, 0o644)
    monkeypatch.setenv("CHROME_BIN", str(not_exec))
    monkeypatch.setattr("shutil.which", lambda name: None)
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path / "does-not-exist"))
    assert find_packer_binary() is None
