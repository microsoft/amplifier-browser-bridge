#!/usr/bin/env python3
"""Verify a packaged Android CRX3 -- everything that CAN be checked from Linux.

This is deliberately NOT a substitute for on-device verification. It confirms the
file is structurally a real CRX3 (not a renamed .zip -- see docs/ANDROID.md's
"packaging traps" section) and that its payload matches what we intended to ship.
It cannot confirm Edge Canary on Android actually accepts and installs the file --
see docs/ANDROID.md for the honest list of what remains unproven until confirmed
on-device.

Usage:
    python3 scripts/verify_crx.py path/to/extension.crx --key path/to/signing-key.pem
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import struct
import subprocess
import sys
import zipfile
from pathlib import Path

CRX3_MAGIC = b"Cr24"


def parse_crx3_header(data: bytes) -> tuple[int, int, bytes]:
    """Returns (version, header_length, zip_bytes). Raises ValueError if this
    isn't a real CRX3 file (magic/version mismatch, truncated header) -- exactly
    the failure mode a renamed .zip produces."""
    if len(data) < 12:
        raise ValueError(f"file too short to be CRX3 ({len(data)} bytes)")
    magic = data[0:4]
    if magic != CRX3_MAGIC:
        raise ValueError(f"bad magic: {magic!r} (expected {CRX3_MAGIC!r} -- this is not a real CRX file)")
    version = struct.unpack("<I", data[4:8])[0]
    if version != 3:
        raise ValueError(f"unsupported CRX version: {version} (expected 3)")
    header_length = struct.unpack("<I", data[8:12])[0]
    zip_start = 12 + header_length
    if zip_start > len(data):
        raise ValueError(
            f"header_length ({header_length}) extends past end of file "
            f"({len(data)} bytes total) -- truncated or corrupt CRX"
        )
    zip_bytes = data[zip_start:]
    if zip_bytes[0:4] != b"PK\x03\x04":
        raise ValueError("payload after CRX header is not a valid zip (missing PK\\x03\\x04 magic)")
    return version, header_length, zip_bytes


def compute_extension_id(pem_path: Path) -> str:
    """Chrome/Edge extension ID: SHA-256 of the DER-encoded public key,
    first 16 bytes, each nibble mapped 0-9a-f -> a-p. Uses openssl (no extra
    Python dependency) to derive the public key from the private key PEM."""
    result = subprocess.run(
        ["openssl", "pkey", "-in", str(pem_path), "-pubout", "-outform", "DER"],
        capture_output=True,
        check=True,
    )
    digest = hashlib.sha256(result.stdout).digest()[:16]
    hex_str = digest.hex()
    mapping = str.maketrans("0123456789abcdef", "abcdefghijklmnop")
    return hex_str.translate(mapping)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("crx_path", type=Path)
    parser.add_argument("--key", type=Path, help="Path to the signing key .pem, for extension ID computation")
    args = parser.parse_args()

    data = args.crx_path.read_bytes()
    sha256 = hashlib.sha256(data).hexdigest()
    size = len(data)

    print(f"File: {args.crx_path}")
    print(f"Size: {size} bytes")
    print(f"SHA-256: {sha256}")
    print()

    try:
        version, header_length, zip_bytes = parse_crx3_header(data)
    except ValueError as e:
        print(f"CRX3 STRUCTURE: INVALID -- {e}")
        return 1

    print("CRX3 STRUCTURE: valid")
    print("  magic: Cr24")
    print(f"  version: {version}")
    print(f"  header_length: {header_length} bytes")
    print(f"  zip payload: {len(zip_bytes)} bytes, starts with PK\\x03\\x04")
    print()

    zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
    names = zf.namelist()
    print(f"Zip contents ({len(names)} entries): {names}")
    if "manifest.json" not in names:
        print("MANIFEST CHECK: FAILED -- no manifest.json at zip root")
        return 1

    manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
    print()
    print("manifest.json contents:")
    print(json.dumps(manifest, indent=2))

    problems = []
    if manifest.get("manifest_version") != 3:
        problems.append("manifest_version is not 3")
    if "debugger" in manifest.get("permissions", []):
        problems.append(
            "'debugger' permission is present -- this is genuinely absent on Edge Android "
            "and should not be requested by the Android build (see manifest.android.json)"
        )
    if manifest.get("background", {}).get("service_worker") != "background.js":
        problems.append("background.service_worker is not background.js")

    print()
    if problems:
        print("MANIFEST CHECK: ISSUES FOUND")
        for p in problems:
            print(f"  - {p}")
    else:
        print("MANIFEST CHECK: OK (manifest_version=3, no debugger permission, service_worker present)")

    if args.key and args.key.is_file():
        ext_id = compute_extension_id(args.key)
        print()
        print(f"Computed extension ID: {ext_id}")
        print("(stable across rebuilds as long as the same signing key is reused)")
    else:
        print()
        print("No signing key provided/found -- skipping extension ID computation.")

    print()
    print("--- What this verification does NOT prove ---")
    print("This confirms the file is a structurally valid CRX3 with the intended manifest.")
    print("It does NOT confirm Edge Canary on a real Android device will accept, install,")
    print("or run this extension. See docs/ANDROID.md for what remains unproven on-device.")

    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
