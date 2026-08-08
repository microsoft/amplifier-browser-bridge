#!/usr/bin/env python3
"""Serve the Android sideload artifact plus a phone-sized `/setup` page.

This is the source of record for the `/setup` page an operator points a phone at.
It previously existed only as an untracked scratch file on one dev machine, which
meant the page a real user saw was not reviewable and did not carry the same
experimental framing the docs do. It is tracked here so it does.

Two things it deliberately does:

1. **Serves the CRX as an opaque octet-stream under whatever name it has on disk.**
   `docs/ANDROID.md`'s "Two packaging traps" explains why the artifact must arrive
   as `.bin`: Chromium intercepts `.crx` downloads and Edge Android silently
   discards the file. This server does not rename anything for you -- point it at
   an already-`.bin`-named artifact.
2. **Never bakes a token into the page.** The page carries install instructions
   only. The hub URL and token travel inside the packed artifact itself (see
   "Zero-configuration builds" in `docs/ANDROID.md`), which is why the page says
   there is nothing to paste.

Usage:

    python3 scripts/serve-android-setup.py --root dist/android \\
        --artifact amplifier-browser-bridge-android-v0.4.0.bin \\
        --host "$(tailscale ip -4)" --port 8686

Then open `http://<that host>:<that port>/setup` on the phone.
"""

from __future__ import annotations

import argparse
import http.server
import pathlib
import socketserver

# Kept as one module-level template so the page is reviewable as a whole, the way
# a real HTML file would be. `{artifact}` is the only substitution.
PAGE = """<!doctype html><html><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>Set up Amplifier Browser Bridge (experimental)</title><style>
*{{box-sizing:border-box}}
body{{font:16px/1.55 system-ui,-apple-system,sans-serif;margin:0;padding:20px 16px 64px;
max-width:620px;margin-inline:auto;background:#0f1115;color:#e6e8ec}}
h1{{font-size:20px;margin:0 0 4px}} h2{{font-size:15px;margin:26px 0 8px;color:#9aa4b2;
text-transform:uppercase;letter-spacing:.07em}}
.sub{{color:#9aa4b2;margin:0 0 22px;font-size:14px}}
a.dl{{display:block;background:#2f6feb;color:#fff;text-decoration:none;padding:16px;
border-radius:10px;text-align:center;font-weight:600;font-size:17px}}
ol{{padding-left:22px;margin:8px 0}} li{{margin:10px 0}}
code{{background:#171a21;padding:2px 6px;border-radius:4px;font-size:13px}}
.warn{{background:#2a2213;border-left:3px solid #b4842a;padding:11px 13px;
border-radius:0 8px 8px 0;margin:10px 0;font-size:14px}}
.ok{{background:#13240f;border-left:3px solid #4a9a2a;padding:11px 13px;
border-radius:0 8px 8px 0;margin:14px 0;font-size:14px}}
.sec{{background:#241318;border-left:3px solid #b4402a;padding:11px 13px;
border-radius:0 8px 8px 0;margin:18px 0;font-size:13px;color:#d8c2c2}}
.exp{{background:#241a13;border-left:3px solid #d2691e;padding:13px 15px;
border-radius:0 8px 8px 0;margin:0 0 20px;font-size:14px}}
.exp b{{color:#ffb066}}
.exp ul{{padding-left:20px;margin:8px 0 0}} .exp li{{margin:6px 0}}
.step{{color:#9aa4b2;font-size:13px;margin-top:2px}}
</style></head><body>
<h1>Amplifier Browser Bridge</h1>
<p class=sub>Add this browser to the hub.</p>

<div class=exp><b>&#9888; Android support is EXPERIMENTAL.</b> Edge on the desktop is the
supported platform. This install is a sideload with sharp edges.
<ul>
<li><b>This will not work on Edge Android stable.</b> Stable supports only a small,
Microsoft-curated set of extensions &mdash; about two dozen. This extension is not on
that list, and there is no documented way to get onto it.</li>
<li><b>You need Edge Canary or Beta</b>, and the hidden developer-options flow in step 2.
Microsoft does not document that flow publicly.</li>
<li><b>This extension&rsquo;s own code has never been confirmed running on a real Android
device.</b> The platform behaviour was measured with a separate throwaway probe
extension, not with this one.</li>
</ul></div>

<div class=ok><b>This build is pre-configured.</b> The hub address and token are already
inside it. Install it and it connects &mdash; there is nothing to paste.</div>

<h2>1 &mdash; Download</h2>
<a class=dl href="/{artifact}" download>Download extension</a>
<div class=warn><b>Then rename it.</b> Open <b>My Files &rarr; Downloads</b> and rename
<code>{artifact}</code> so it ends in <code>.crx</code> instead of <code>.bin</code>.
<div class=step>It downloads as .bin on purpose &mdash; Chromium intercepts .crx downloads
and Edge Android silently throws the file away.</div></div>

<h2>2 &mdash; Install</h2>
<ol>
<li>Edge Canary &rarr; <b>Settings</b> &rarr; <b>About Microsoft Edge</b></li>
<li>Tap the <b>build number 5 times</b> &mdash; this unlocks Developer Options</li>
<li>Back &rarr; <b>Developer Options</b> &rarr; <b>Extension install by crx</b></li>
<li>Pick the renamed <code>.crx</code> file</li>
</ol>
<div class=step>No Developer Options entry after five taps means you are on stable, not
Canary or Beta &mdash; stable cannot install this.</div>

<h2>3 &mdash; Battery (required)</h2>
<p class=step style="margin-top:0">Settings &rarr; Apps &rarr; Edge Canary &rarr; Battery
&rarr; <b>Unrestricted</b>, and remove it from &ldquo;sleeping apps&rdquo;.
Without this the phone is unreachable whenever the screen is off &mdash; measured at
509s dark without it, ~85s with it.</p>

<h2>Done</h2>
<p class=step style="margin-top:0">It should connect on its own. Nothing else to do &mdash;
tell the agent and it will confirm the device appeared. If it never appears, that is a
known-unproven path &mdash; see docs/ANDROID.md, &ldquo;What remains unproven&rdquo;.</p>

<div class=sec><b>Note:</b> the downloaded file contains a live hub credential. Anyone who
gets it can connect to your hub as a device. Treat it like the token file itself &mdash;
don&rsquo;t forward it or leave it in shared storage. If it leaks, rotate with
<code>amplifier-browser-bridge init --force</code> and rebuild.</div>
</body></html>"""


def build_handler(root: pathlib.Path, artifact: str) -> type[http.server.BaseHTTPRequestHandler]:
    """Return a handler bound to this root/artifact pair (no globals)."""

    class Handler(http.server.BaseHTTPRequestHandler):
        def _send(
            self,
            code: int,
            body: bytes = b"",
            ctype: str = "text/plain",
            extra: dict[str, str] | None = None,
        ) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for key, value in (extra or {}).items():
                self.send_header(key, value)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def do_HEAD(self) -> None:  # HTTP verb naming is fixed by BaseHTTPRequestHandler
            self.do_GET()

        def do_GET(self) -> None:  # HTTP verb naming is fixed by BaseHTTPRequestHandler
            path = self.path.split("?")[0].rstrip("/") or "/"
            if path in ("/", "/setup"):
                return self._send(200, PAGE.format(artifact=artifact).encode(), "text/html; charset=utf-8")
            candidate = root / path.lstrip("/")
            # Refuse anything that escapes root -- a phone-facing listener on a
            # tailnet is still a listener.
            if not candidate.is_file() or root.resolve() not in candidate.resolve().parents:
                return self._send(404, b"not found\n")
            self._send(
                200,
                candidate.read_bytes(),
                "application/octet-stream",
                {"Content-Disposition": f'attachment; filename="{candidate.name}"'},
            )

        def log_message(self, format: str, *args: object) -> None:
            print(f"  {self.address_string()} - {format % args}", flush=True)

    return Handler


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--root", default="dist/android", help="directory holding the artifact")
    parser.add_argument("--artifact", required=True, help="artifact filename, e.g. ...-v0.4.0.bin")
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="bind address; use this machine's tailnet IP (`tailscale ip -4`) to reach it from a phone",
    )
    parser.add_argument("--port", type=int, default=8686)
    args = parser.parse_args()

    root = pathlib.Path(args.root).resolve()
    if not (root / args.artifact).is_file():
        parser.error(f"artifact not found: {root / args.artifact}")

    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.TCPServer((args.host, args.port), build_handler(root, args.artifact)) as httpd:
        print(f"serving http://{args.host}:{args.port}/setup", flush=True)
        httpd.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
