"""Unit tests for onboarding.py -- pure presentation logic, no filesystem/network.

Rewritten for docs/designs/onboarding-ux.md: `/setup` is now a two-step ladder
(desktop steps 1-2 only; Android moved to its own `/setup/android` page -- see
onboarding.py's module docstring, "The Android page split" section), and the
Android disclosure/section tests from before the redesign are replaced by tests
against `render_android_setup_page`.
"""

from __future__ import annotations

from amplifier_browser_bridge.onboarding import detect_platform, render_android_setup_page, render_setup_page


def test_detect_platform_android() -> None:
    assert detect_platform("Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36") == "android"


def test_detect_platform_defaults_to_desktop() -> None:
    assert detect_platform("") == "desktop"
    assert detect_platform("Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5)") == "desktop"
    assert detect_platform("Mozilla/5.0 (Windows NT 10.0; Win64; x64)") == "desktop"


# --- render_setup_page: the two-step ladder -------------------------------------


def test_render_setup_page_renders_both_steps() -> None:
    html = render_setup_page(platform="desktop", host="100.1.2.3", port=8900, android_available=False)
    assert 'id="step-1"' in html
    assert 'id="step-2"' in html
    assert "/setup/extension.zip" in html


def test_render_setup_page_step1_is_now_step2_is_next() -> None:
    html = render_setup_page(platform="desktop", host="h", port=1, android_available=False)
    assert '<section class="step" data-state="now" id="step-1">' in html
    assert '<section class="step" data-state="next" id="step-2">' in html


def test_render_setup_page_links_to_the_standalone_android_page() -> None:
    """The design deviation this project made: Android is its own page, linked
    from step 1, not a <details> disclosure on this page (see onboarding.py's
    module docstring for why)."""
    html = render_setup_page(platform="desktop", host="h", port=1, android_available=False)
    assert 'href="/setup/android"' in html
    assert "Android (experimental)" in html
    # And it must NOT re-embed the Android section's own heavy content (the
    # credential warning, the 5 install steps, etc.) -- that's the whole point
    # of the split.
    assert "live password to this browser" not in html
    assert "Extension install by crx" not in html


def test_render_setup_page_shows_an_android_cue_when_platform_is_android() -> None:
    html = render_setup_page(platform="android", host="h", port=1, android_available=True)
    assert "Android needs different steps" in html
    assert 'href="/setup/android"' in html


def test_render_setup_page_unrecognized_platform_falls_back_to_desktop() -> None:
    html = render_setup_page(platform="ios", host="h", port=1, android_available=False)
    # No crash, no android-specific cue leaking in for an unrecognized platform.
    assert "Android needs different steps" not in html


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
    html = render_setup_page(platform="desktop", host="h", port=1, android_available=False)
    assert "location.hash" in html
    assert "pair-code-value" in html


def test_render_setup_page_explains_auth_reasoning() -> None:
    html = render_setup_page(platform="desktop", host="h", port=1, android_available=False)
    assert "no hub token yet" in html
    assert "long-lived hub token" in html


def test_render_setup_page_carries_the_differentiator_inside_the_why_safe_disclosure() -> None:
    """purpose-keeper review finding, carried forward: the product's actual
    differentiator -- your own network, no third-party relay -- must still be
    reachable, one click away, at the very top of the page (inside the first
    disclosure) -- not deleted, and not buried behind pairing."""
    html = render_setup_page(platform="desktop", host="h", port=1, android_available=False)
    assert "Why is this safe?" in html
    why_safe_block = html.split("Why is this safe?")[1].split("</details>")[0]
    assert "your own network" in why_safe_block
    assert "no third-party relay" in why_safe_block


def test_render_setup_page_subtitle_is_one_short_line() -> None:
    """Copy rule (docs/designs/onboarding-ux.md section 4): an instruction is
    one line, <=10 words. No paragraph is ever on the path."""
    html = render_setup_page(platform="desktop", host="h", port=1, android_available=False)
    assert "Let your agent use this browser." in html


def test_render_setup_page_auto_copies_and_offers_a_copy_button() -> None:
    """Maintainer feedback: "if we HAVE to copy and paste the pairing code,
    can't we auto put it into the user's clipboard, and as a fallback have a
    copy button next to it?" -- both must be present. The page is served over
    plain http (see hub.py/pairing.py), so `navigator.clipboard` is undefined
    (secure-context requirement) -- `execCommand("copy")` must be the PRIMARY
    mechanism here, not merely mentioned as a fallback."""
    html = render_setup_page(platform="desktop", host="h", port=1, android_available=False)
    assert "copyText(code)" in html  # auto-copy fires the moment a code is shown
    assert 'id="pair-copy-btn"' in html  # the visible fallback button
    assert 'document.execCommand("copy")' in html


def test_render_setup_page_desktop_steps_are_concise() -> None:
    """Real-run maintainer feedback: instructions must be short, numbered
    steps, not paragraphs."""
    html = render_setup_page(platform="desktop", host="h", port=1, android_available=False)
    step1 = html.split('id="step-1"')[1].split('id="step-2"')[0]
    assert step1.count("<li>") == 3


def test_render_setup_page_countdown_script_present() -> None:
    """human-advocate review finding: the ticket's real, short TTL had no visible
    countdown anywhere. The setup page ticks one down from an `exp` fragment
    param (never sent to the server -- see this module's docstring)."""
    html = render_setup_page(platform="desktop", host="h", port=1, android_available=False)
    assert "pair-countdown" in html
    assert "pair-expired-note" in html
    assert 'params.get("exp")' in html


def test_render_setup_page_deletes_the_old_clipboard_narration_line() -> None:
    """docs/designs/onboarding-ux.md section 5.2: the old "Copied to your
    clipboard. Open the extension's Settings..." paragraph is deleted -- it
    narrated a clipboard write the user did not ask for, then pre-explained a
    failure that usually doesn't happen."""
    html = render_setup_page(platform="desktop", host="h", port=1, android_available=False)
    assert "Copied to your clipboard" not in html
    assert "should pair itself" not in html


def test_render_setup_page_manual_paste_instructions_are_behind_a_disclosure() -> None:
    """The moved-not-deleted counterpart of the above: paste instructions now
    live behind "It didn't connect on its own", not on the path."""
    html = render_setup_page(platform="desktop", host="h", port=1, android_available=False)
    assert "It didn't connect on its own" in html
    disclosure_block = html.split("It didn't connect on its own")[1].split("</details>")[0]
    assert "Enter a code by hand" in disclosure_block


def test_render_setup_page_polls_pair_status_on_the_same_interval_as_the_countdown() -> None:
    """The real bug this closes (docs/designs/onboarding-ux.md section 5.2's
    "One behavioral requirement"): the page must poll the hub for redemption
    of its own code and flip waiting -> paired within ~2s, on the SAME
    interval as the pre-existing countdown tick -- both live inside the one
    `setInterval(..., 1000)` call."""
    html = render_setup_page(platform="desktop", host="h", port=1, android_available=False)
    assert "/pair/status" in html
    assert "setInterval(function () {" in html
    interval_block = html.split("var timer = setInterval(")[1]
    assert "checkStatus()" in interval_block
    assert ", 1000)" in interval_block


def test_render_setup_page_no_paragraph_style_hint_narrates_the_polling_mechanism() -> None:
    """Copy rule: never explain a mechanism the user did not experience --
    the polling itself must be invisible; only its RESULT (a state change)
    is visible."""
    html = render_setup_page(platform="desktop", host="h", port=1, android_available=False)
    assert "polling" not in html.lower().replace("<!-- polling -->", "")


def test_render_setup_page_no_code_in_link_state_unchanged_behavior() -> None:
    html = render_setup_page(platform="desktop", host="100.1.2.3", port=8900, android_available=False)
    assert "amplifier-browser-bridge pair" in html
    assert "pair-none" in html


# --- setup-done: the page's final, collapsed state (maintainer finding) --------


def test_render_setup_page_has_a_hidden_final_done_state() -> None:
    """The final state exists in the markup, hidden by default -- showPaired()
    reveals it client-side once /pair/status reports redeemed."""
    html = render_setup_page(platform="desktop", host="h", port=1, android_available=False)
    assert 'id="setup-done"' in html
    # The opening tag itself must start hidden.
    opening_tag = html[html.index('<section class="step" data-state="done" id="setup-done"') :].split(">")[0]
    assert 'style="display:none;"' in opening_tag


def test_render_setup_page_final_state_keeps_the_android_link() -> None:
    """Maintainer instruction: everything about the finished install ladder
    goes away, EXCEPT the Android link -- a paired desktop user may still
    want to add a phone."""
    html = render_setup_page(platform="desktop", host="h", port=1, android_available=False)
    done_block = html.split('id="setup-done"')[1].split("</section>")[0]
    assert 'href="/setup/android"' in done_block


def test_render_setup_page_final_state_is_brief() -> None:
    """Copy rule (docs/designs/onboarding-ux.md section 4): brief, no walls of
    text -- the done block is a title + one context line + the Android link,
    nothing else."""
    html = render_setup_page(platform="desktop", host="h", port=1, android_available=False)
    done_block = html.split('id="setup-done"')[1].split("</section>")[0]
    assert done_block.count("<p>") <= 1
    assert "<ol" not in done_block
    assert "Download extension" not in done_block
    assert "edge://extensions" not in done_block


def test_pair_script_collapses_the_whole_ladder_not_just_step_2() -> None:
    """Real bug this closes: showPaired() used to only flip step 2's own
    data-state to "done" -- step 1's full install-ladder body (download
    button, unzip steps, edge://extensions instructions, the manual `pair`
    fallback) stayed on the page forever, since the CSS rule that hides a
    step's body only fires for data-state="next", never "done"."""
    html = render_setup_page(platform="desktop", host="h", port=1, android_available=False)
    show_paired_block = html.split("function showPaired() {")[1].split("\n  }")[0]
    assert 'getElementById("setup-ladder")' in show_paired_block
    assert 'getElementById("setup-done")' in show_paired_block
    assert 'ladder.style.display = "none"' in show_paired_block


# --- render_android_setup_page ---------------------------------------------------


def test_render_android_setup_page_shows_credential_warning_always() -> None:
    """Design doc section 5.3: "The credential warning cannot be nested. It
    must be visible the moment that section opens" -- here, the moment the
    page loads (it has no disclosure gating it at all)."""
    html = render_android_setup_page(host="h", port=1, download_available=False)
    assert "live password to this browser" in html
    assert "note-caution" in html


def test_render_android_setup_page_links_back_to_setup() -> None:
    html = render_android_setup_page(host="h", port=1, download_available=False)
    assert 'href="/setup"' in html
    assert "Back to setup" in html


def test_render_android_setup_page_download_available() -> None:
    html = render_android_setup_page(host="h", port=1, download_available=True)
    assert 'href="/setup/android-extension.bin"' in html
    assert "No build available on this hub yet" not in html


def test_render_android_setup_page_download_unavailable_shows_the_honest_reason() -> None:
    html = render_android_setup_page(
        host="h", port=1, download_available=False, unavailable_reason="no packer found -- set CHROME_BIN"
    )
    assert "No build available on this hub yet" in html
    assert "no packer found -- set CHROME_BIN" in html
    assert 'href="/setup/android-extension.bin"' not in html


def test_render_android_setup_page_known_limits_disclosure_present() -> None:
    html = render_android_setup_page(host="h", port=1, download_available=False)
    assert "Known limits" in html
    known_limits_block = html.split("Known limits")[1]
    assert "Edge Android stable" in known_limits_block
    assert "never been confirmed running on a real" in known_limits_block


def test_render_android_setup_page_install_steps_present() -> None:
    html = render_android_setup_page(host="h", port=1, download_available=True)
    assert "Extension install by crx" in html
    assert "build number 5 times" in html


def test_render_android_setup_page_battery_note_present_and_condensed() -> None:
    html = render_android_setup_page(host="h", port=1, download_available=True)
    assert "Unrestricted" in html
    assert "disconnects when the screen is off" in html
