import { test } from "node:test";
import assert from "node:assert/strict";

let counter = 0;
const alarmName = "amplifier-browser-bridge-revive";

async function loadWorker(initialAlarm, configured = false) {
  let alarm = initialAlarm;
  const creates = [];
  const writes = [];
  const connections = [];
  const stored = configured ? {
    amplifier_browser_bridge_hub_url: "ws://100.64.1.2:8900/device",
    amplifier_browser_bridge_hub_token: "existing-device-token",
    amplifier_browser_bridge_device_id: "existing-device",
    amplifier_browser_bridge_profile_id: "existing-profile",
    amplifier_browser_bridge_setup_completed: true,
  } : {};
  const listeners = {};
  const event = (name) => ({ addListener(fn) { listeners[name] = fn; } });
  globalThis.WebSocket = class {
    static CONNECTING = 0;
    static OPEN = 1;
    readyState = 0;
    constructor(url) { connections.push(url); }
    addEventListener() {}
  };
  globalThis.__AMPLIFIER_BROWSER_BRIDGE_BACKGROUND_TEST__ = false;
  globalThis.fetch = async () => ({ ok: false });
  globalThis.chrome = {
    runtime: {
      getURL: (name) => `https://extension.invalid/${name}`,
      onInstalled: event("installed"), onStartup: event("startup"),
      onMessage: event("message"), openOptionsPage() {},
    },
    debugger: { onDetach: event("detach") },
    action: {
      onClicked: event("clicked"), setBadgeText() {},
      setBadgeBackgroundColor() {}, setTitle() {},
    },
    storage: {
      onChanged: event("storage"),
      local: { get: async () => stored, set: async (value) => writes.push(value) },
    },
    alarms: {
      onAlarm: event("alarm"), get: async () => alarm,
      create: async (name, info) => {
        creates.push({ name, ...info }); alarm = { name, ...info };
      },
    },
    tabs: { onActivated: event("activated"), onUpdated: event("updated") },
  };
  await import(new URL(`./background.js?lifecycle=${++counter}`, import.meta.url));
  await new Promise((resolve) => setImmediate(resolve));
  return { creates, writes, listeners, stored, connections, clearAlarm() { alarm = undefined; } };
}

test("a worker revived after restart restores a cleared alarm without re-pairing", async () => {
  const worker = await loadWorker(undefined);
  assert.deepEqual(worker.creates, [{ name: alarmName, periodInMinutes: 0.5 }]);
  assert.deepEqual(worker.writes, [], "recovery must preserve pairing storage");
});

test("startup repairs a lost alarm even if the worker was already running", async () => {
  const worker = await loadWorker({ name: alarmName, periodInMinutes: 0.5 });
  worker.clearAlarm();
  await worker.listeners.startup();
  await new Promise((resolve) => setImmediate(resolve));
  assert.deepEqual(worker.creates, [{ name: alarmName, periodInMinutes: 0.5 }]);
  assert.deepEqual(worker.writes, []);
});

test("worker load and startup leave an existing repeating alarm scheduled", async () => {
  const worker = await loadWorker({ name: alarmName, periodInMinutes: 0.5 });
  await worker.listeners.startup();
  await new Promise((resolve) => setImmediate(resolve));
  assert.deepEqual(worker.creates, [], "do not postpone an existing recovery alarm");
  assert.deepEqual(worker.writes, []);
});

test("restoring the alarm reconnects with existing configuration without replacing token or identity", async () => {
  const worker = await loadWorker(undefined, true);
  assert.deepEqual(worker.creates, [{ name: alarmName, periodInMinutes: 0.5 }]);
  assert.deepEqual(worker.connections, ["ws://100.64.1.2:8900/device"]);
  assert.equal(worker.stored.amplifier_browser_bridge_hub_token, "existing-device-token");
  assert.equal(worker.stored.amplifier_browser_bridge_device_id, "existing-device");
  assert.deepEqual(worker.writes, []);
});
