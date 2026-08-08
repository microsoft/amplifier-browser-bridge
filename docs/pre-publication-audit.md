# Pre-publication PII and trademark audit procedure

A checklist to run before publishing **any** screenshot, video, GIF, or
recorded demo of this project -- to a store listing, the landing page,
`README.md`, a GitHub issue, a blog post, or a conference talk. Run it again
before every subsequent update that adds or replaces a visual asset, not just
the first time.

## Why this project's hazard is worse than a typical extension's

The sibling projects this release kit is modeled on
(`teams-transcript-md`, `loop-page-md`) capture a single, narrow surface
(a transcript pane, a Loop page) and their maintainer still found and fixed
real PII in a published screenshot after the fact (see "Precedent" below).
**This project's normal, correctly-working screenshot is a picture of an AI
agent operating someone's real, logged-in browser.** By design, that means:

- Any screenshot showing the extension in actual use is, definitionally, a
  screenshot of a **real authenticated session** -- there is no
  synthetic/demo mode that produces a realistic-looking screenshot without
  real content behind it.
- A single screenshot can incidentally capture far more than the one feature
  being demonstrated: other open tabs, the bookmarks bar, browser history
  suggestions in the address bar, notification badges, a tenant name in a
  SharePoint/M365 URL, an account avatar or email in a profile switcher, or
  content in a background window visible through the "co-working" framing
  this project's whole pitch depends on showing.
- Because the product's own selling point is "acts on tabs you're not
  looking at," a natural demo screenshot may deliberately show **multiple
  tabs or windows at once** -- multiplying the chance of an incidental leak
  compared to a single-purpose extension's single-popup screenshot.
- Video/GIF demos carry everything a still screenshot does, continuously,
  across a longer capture window, with no single frame to review in
  isolation -- every frame is a potential leak, not just the one you meant to
  capture.

## Precedent (read both before running this checklist)

- **`teams-transcript-md` commit `c2d82a747fd1c0bd6d17f5e5fe9bc5e967e42057`**
  (`docs(site): replace real names with neutral placeholders`) -- found and
  fixed, in a published landing page, real coworkers' names in a sample
  transcript code block, a real corporate SharePoint tenant hostname in a
  stylized UI mockup, and a real person's name in the footer attribution and
  the `LICENSE` copyright line. This was caught **after** initial publication,
  not before -- the lesson is not "this maintainer is careless," it's "this
  category of leak is easy to miss even when you're looking," which is
  exactly why a checklist run before publication (not a one-time cleanup
  after) is the point of this document.
- **`loop-page-md/store-assets/ASSETS-TODO.md`** -- a written screenshot
  audit performed **before** submission, on a genuine, undoctored capture:
  confirmed no faces/avatars, no real names or emails, no tenant-specific
  URL, no other tabs or bookmarks, no OS chrome, and that the only visible
  third-party mark (Microsoft's Loop logo) appeared only where it
  legitimately belongs (the site's own favicon and interface), never as this
  extension's own mark. That is the shape of audit this document formalizes
  for a harder case.

## The procedure

Run every step below on **every** image/video/GIF before it is committed to
a repo, uploaded to a store listing, or posted anywhere public. Do not
publish, then fix -- both precedent cases above show why "review after" is
too late to reliably catch this.

### Step 1 -- Capture deliberately, not incidentally

- Use a browser profile created **specifically for this capture**, with no
  real personal accounts signed in, no real bookmarks, no real browsing
  history, and no other extensions installed. If a specific site login is
  needed to show the feature, use a throwaway or explicitly-sanctioned test
  account -- never a real personal or corporate account, even briefly.
- Close every tab except the one(s) intentionally being demonstrated, before
  capturing -- this project's own "multiple tabs/windows at once" selling
  point (see above) is exactly the scenario that makes an incidental extra
  tab likely; don't let a demo of that feature be the vector for a leak.
- Hide or clear the bookmarks bar, and clear the address bar's autocomplete
  history for the session, before capturing.

### Step 2 -- Review every frame for personally identifiable information

Check each of the following explicitly -- do not rely on "I would have
noticed" during the general course of capturing:

- [ ] No real person's name, in any visible text (page content, browser UI,
      avatar tooltip, window title, filename in a "Save as" dialog, or
      terminal/CLI output visible in the same frame).
- [ ] No real email address, phone number, or physical address.
- [ ] No real account avatar/photo (a profile picture in a browser corner
      account switcher is a common miss).
- [ ] No real organization/tenant identifier -- a SharePoint/M365 URL
      (`*.sharepoint.com/sites/<tenant-specific-name>`), a company-internal
      hostname, an internal tool's URL, or a Tailscale MagicDNS name
      (`*.tailnet-name.ts.net`) that reveals a real tailnet's identity.
- [ ] No real financial, healthcare, or authentication-related content, even
      if it's exactly the kind of content this project's own denylist
      (`docs/POLICY.md` section 2) is designed to keep the agent from
      reading in the first place -- a screenshot showing the agent looking
      at such a page (even to demonstrate that it's correctly hidden) risks
      capturing real content behind the demonstration.
- [ ] No other open tabs, in a tab strip or a window switcher, that weren't
      deliberately included and individually checked against every item
      above.
- [ ] No terminal/CLI output in the same frame containing a real hostname,
      real IP address (a Tailscale `100.x.y.z` address identifies a specific
      device on a specific tailnet), a real file path containing a home
      directory username, or a real token/credential value (even a
      truncated or partially-redacted one -- redact by replacing with
      placeholder text, not by cropping, which can leave recoverable pixels
      at the crop boundary).
- [ ] No internal-only project/document names that aren't meant to be
      public (this project's own history includes exactly this kind of
      leak being scrubbed once already -- see `KNOWN` items in this
      project's own goal-tracking history for the class of thing to check
      for).

### Step 3 -- Trademark and affiliation review

- [ ] Third-party marks (Microsoft Edge's logo, a site's own branding
      visible in a demonstrated page, etc.) appear **only** where they
      legitimately belong -- as that site's own UI, never repurposed as
      this project's own icon, mark, or branding.
- [ ] No visual implies Microsoft or any third-party's endorsement,
      partnership, or official sanction of this project beyond what is
      factually true. If this project is not an official Microsoft product
      at time of publication, no asset should visually suggest otherwise
      (e.g. do not crop out a real Edge browser chrome in a way that reads
      as "this is a built-in Edge feature").
- [ ] Any written copy accompanying the asset (landing page text, store
      listing description, alt text) carries a plain-language "not an
      official product of / not affiliated with" disclaimer if that is the
      actual relationship, matching this repo's own `README.md`/`LICENSE`
      framing at time of publication.

### Step 4 -- Redaction, if step 2 or 3 finds something

- Redact by generating a **new** capture with the offending content
  replaced (fake data, a placeholder account, a neutral hostname) rather
  than pixel-editing a real capture -- editing tools can leave recoverable
  data in metadata (EXIF), unredacted layers, or un-flattened image regions.
- If a code sample or mockup (not a real screenshot) is used instead of a
  live capture, use clearly fictional placeholders (see `teams-transcript-md`
  commit `c2d82a7`'s pattern: real names -> `Alex`/`Sam`/`Jordan`, a real
  tenant hostname -> `contoso.sharepoint.com`) -- `contoso.com` and its
  subdomains are Microsoft's own reserved fictional-example domain,
  specifically intended for this purpose.
- Re-run Step 2 and Step 3 in full on the redacted/regenerated version --
  redaction is itself a step that can introduce a new leak (e.g. blurring a
  name but leaving its length/shape guessable, or fixing one tab while
  leaving a second one unchecked).

### Step 5 -- Independent second look

- Have a second person (or, at minimum, a second pass after a break, not
  immediately after capturing) review the final asset against this
  checklist before it is published. The precedent above shows the person who
  captured the asset does not reliably catch every leak on their own pass --
  that is a property of how easy this category of mistake is to make, not a
  reflection on any one person's carefulness.

## When this procedure applies

- Before adding any screenshot/GIF/video to `README.md`, `index.html`, a
  store listing, or `store-assets/`-equivalent files.
- Before posting any screenshot in a public GitHub issue, PR description, or
  external write-up.
- Before any conference talk, blog post, or public demo recording.
- Again, in full, before every subsequent replacement of an existing visual
  asset -- a new capture is a new opportunity for a new leak, not a
  incremental touch-up of an already-audited one.
