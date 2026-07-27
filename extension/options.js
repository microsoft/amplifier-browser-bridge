// options.js -- the extension's ONLY user-facing UI. Reads/writes the runtime hub
// configuration (Hub URL + token) directly to chrome.storage.local; background.js's
// storage.onChanged listener picks up a save immediately and reconnects. See
// config_validate.mjs for the shared validation logic and background.js's "Runtime
// configuration" section for why this replaced the old tracked extension/config.js.

import { validateHubUrl, validateHubToken } from "./config_validate.mjs";

const urlInput = document.getElementById("hub-url");
const tokenInput = document.getElementById("hub-token");
const toggleTokenLink = document.getElementById("toggle-token");
const errorEl = document.getElementById("error");
const statusEl = document.getElementById("status");
const saveButton = document.getElementById("save");

async function loadCurrentValues() {
  const stored = await chrome.storage.local.get(["amplifier_browser_bridge_hub_url", "amplifier_browser_bridge_hub_token"]);
  urlInput.value = stored.amplifier_browser_bridge_hub_url || "";
  tokenInput.value = stored.amplifier_browser_bridge_hub_token || "";
}

async function refreshStatus() {
  let response;
  try {
    response = await chrome.runtime.sendMessage({ type: "amplifier_browser_bridge_get_status" });
  } catch {
    // Service worker not woken up / no listener yet -- transient, not an error worth
    // showing the user; the next poll (or their own Save click) will pick it up.
    return;
  }
  if (!response) return;

  if (!response.configured) {
    if (response.legacyConfigDetected) {
      // Distinct from the generic "never configured" message: this install HAD a working
      // config under the old (pre-rename) storage keys, which are no longer read. See
      // background.js's loadConfig()/legacyConfigDetected and MIGRATION.md.
      statusEl.className = "warn";
      statusEl.textContent =
        "Configuration key names changed in this version -- your previous Hub URL/token are " +
        "no longer read. Re-enter them below and click Save.";
    } else {
      statusEl.className = "warn";
      statusEl.textContent = "Not configured -- enter a Hub URL below and click Save.";
    }
    return;
  }
  if (response.connected) {
    statusEl.className = "ok";
    statusEl.textContent = `Connected to ${response.hubUrl} as device ${response.deviceId || "(pending)"}.`;
  } else {
    statusEl.className = "warn";
    statusEl.textContent =
      `Configured for ${response.hubUrl}, but not currently connected -- ` +
      "is the hub running and reachable? Run `amplifier-browser-bridge doctor` from the CLI for a full check.";
  }
}

toggleTokenLink.addEventListener("click", () => {
  const showing = tokenInput.type === "text";
  tokenInput.type = showing ? "password" : "text";
  toggleTokenLink.textContent = showing ? "show" : "hide";
});

saveButton.addEventListener("click", async () => {
  errorEl.textContent = "";
  const urlValidation = validateHubUrl(urlInput.value);
  if (!urlValidation.valid) {
    errorEl.textContent = urlValidation.error;
    return;
  }
  const tokenValidation = validateHubToken(tokenInput.value);
  if (!tokenValidation.valid) {
    errorEl.textContent = tokenValidation.error;
    return;
  }

  await chrome.storage.local.set({
    amplifier_browser_bridge_hub_url: urlValidation.normalized,
    amplifier_browser_bridge_hub_token: tokenInput.value,
  });

  statusEl.className = "unknown";
  statusEl.textContent = "Saved. Connecting...";
  // Give background.js's storage.onChanged handler a moment to reconnect, then poll
  // status a few times -- a real hub round trip (device auth + hello) is not instant.
  setTimeout(refreshStatus, 500);
  setTimeout(refreshStatus, 2000);
  setTimeout(refreshStatus, 5000);
});

loadCurrentValues();
refreshStatus();
