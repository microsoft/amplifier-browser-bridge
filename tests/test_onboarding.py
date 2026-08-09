"""Unit tests for onboarding.py -- pure presentation logic, no filesystem/network."""

from __future__ import annotations

from amplifier_browser_bridge.onboarding import detect_platform, render_setup_page


def test_detect_platform_android() -> None:
    assert detect_platform("Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36") == "android"


def test_detect_platform_defaults_to_desktop() -> None:
    assert detect_platform("") == "desktop"
    assert detect_platform("Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5)") == "desktop"
    assert detect_platform("Mozilla/5.0 (Windows NT 10.0; Win64; x64)") == "desktop"


def test_render_setup_page_desktop_opens_desktop_section() -> None:
    html = render_setup_page(
        platform="desktop", host="100.1.2.3", port=8900, android_artifact_available=False
    )
    assert '<details data-platform="desktop" open>' in html
    assert '<details data-platform="android">' in html
    assert "/setup/extension.zip" in html


def test_render_setup_page_android_opens_android_section() -> None:
    html = render_setup_page(
        platform="android", host="100.1.2.3", port=8900, android_artifact_available=False
    )
    assert '<details data-platform="android" open>' in html
    assert '<details data-platform="desktop">' in html


def test_render_setup_page_android_download_link_only_when_artifact_available() -> None:
    unavailable = render_setup_page(platform="android", host="h", port=1, android_artifact_available=False)
    assert "No build available on this hub yet" in unavailable
    assert 'href="/setup/android-extension.bin"' not in unavailable

    available = render_setup_page(platform="android", host="h", port=1, android_artifact_available=True)
    assert 'href="/setup/android-extension.bin"' in available
    assert "No build available on this hub yet" not in available


def test_render_setup_page_never_echoes_a_pairing_code_server_side() -> None:
    """The pairing code must NEVER be a parameter to this function at all --
    it only ever reaches the page via the URL fragment, read client-side.
    This test asserts the function signature has no such parameter (a
    regression here would mean the code started flowing through the
    server, and therefore through its access logs)."""
    import inspect

    params = inspect.signature(render_setup_page).parameters
    assert "pair" not in params
    assert "code" not in params
    assert "ticket" not in params


def test_render_setup_page_reads_pairing_code_from_location_hash_client_side() -> None:
    html = render_setup_page(platform="desktop", host="h", port=1, android_artifact_available=False)
    assert "location.hash" in html
    assert "pair-code-value" in html


def test_render_setup_page_unrecognized_platform_falls_back_to_desktop() -> None:
    html = render_setup_page(platform="ios", host="h", port=1, android_artifact_available=False)
    assert '<details data-platform="desktop" open>' in html


def test_render_setup_page_explains_auth_reasoning() -> None:
    html = render_setup_page(platform="desktop", host="h", port=1, android_artifact_available=False)
    assert "locked down" in html
    assert "long-lived hub token" in html


def test_render_setup_page_leads_with_purpose_not_defensive_copy() -> None:
    """purpose-keeper review finding: the product's actual differentiator -- your
    own already-logged-in browser, your own network, no third-party relay --
    appeared NOWHERE the user reads during onboarding. It must be readable
    up-front, not buried in a security disclosure delivered after pairing."""
    html = render_setup_page(platform="desktop", host="h", port=1, android_artifact_available=False)
    assert "your own network" in html
    assert "no third-party relay" in html


def test_render_setup_page_auto_copies_and_offers_a_copy_button() -> None:
    """Maintainer feedback: "if we HAVE to copy and paste the pairing code,
    can't we auto put it into the user's clipboard, and as a fallback have a
    copy button next to it?" -- both must be present. The page is served over
    plain http (see hub.py/pairing.py), so `navigator.clipboard` is undefined
    (secure-context requirement) -- `execCommand("copy")` must be the PRIMARY
    mechanism here, not merely mentioned as a fallback."""
    html = render_setup_page(platform="desktop", host="h", port=1, android_artifact_available=False)
    assert "copyText(code)" in html  # auto-copy fires the moment a code is shown
    assert 'id="pair-copy-btn"' in html  # the visible fallback button
    assert 'document.execCommand("copy")' in html
    assert "secure context" in html  # the reasoning is documented, not assumed


def test_render_setup_page_desktop_section_is_concise() -> None:
    """Real-run maintainer feedback: the desktop section opened with a
    four-sentence "what this does and does not do" paragraph on the path,
    then six steps. The explanatory reasoning now lives behind its own
    disclosure; the numbered steps a non-technical user must follow are down
    to three."""
    html = render_setup_page(platform="desktop", host="h", port=1, android_artifact_available=False)
    assert "What this does and does not do" not in html
    # The "why" is still present, but behind a nested <details>, not on the path.
    assert "Why not one click?" in html
    # Three steps, not five/six.
    desktop_section = html.split("Desktop Edge (Windows")[1].split("Android (experimental)")[0]
    assert desktop_section.count("<li>") == 3


def test_render_setup_page_countdown_script_present() -> None:
    """human-advocate review finding: the ticket's real, short TTL had no visible
    countdown anywhere. The setup page now ticks one down from an `exp` fragment
    param (never sent to the server -- see this module's docstring)."""
    html = render_setup_page(platform="desktop", host="h", port=1, android_artifact_available=False)
    assert "pair-countdown" in html
    assert "pair-expired" in html
    assert 'params.get("exp")' in html
