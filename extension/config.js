// Single, trivially editable place for the extension's hub connection settings.
//
// HUB_URL must be a tailnet IP LITERAL, never a MagicDNS name -- MagicDNS resolution
// was measured to work on one device and fail on another *in the same tailnet at the
// same moment* (design doc §4). IP literals are the only thing that worked everywhere.
//
// HUB_TOKEN here is a placeholder dev credential, not a secret: the value that
// actually gates access lives in the hub's token store (env var / gitignored token
// file -- see auth.py). Replace this placeholder to match whatever the hub operator
// configured, per deployment. Never put a real production credential in this file --
// it ships inside the unpacked extension directory.

export const HUB_URL = "ws://100.124.126.19:8900/device";
export const HUB_TOKEN = "dev-local-token-change-me";
