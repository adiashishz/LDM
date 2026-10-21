// Connection to the ldm native messaging host (ldm_nmhost.py).

import { api } from "./api.js";

const HOST_NAME = "io.github.ldm";
const TIMEOUT_MS = 40000;  // the host may have to start the daemon/GUI first
const IDLE_MS = 30000;     // drop the port when idle so the background can sleep

let port = null;
let nextId = 0;
let idleTimer = null;
const pending = new Map();

export class HostError extends Error {
  constructor(message, notInstalled = false) {
    super(message);
    this.notInstalled = notInstalled;
  }
}

function connect() {
  port = api.runtime.connectNative(HOST_NAME);
  port.onMessage.addListener((message) => {
    const waiter = pending.get(message.id);
    if (!waiter) return;
    pending.delete(message.id);
    clearTimeout(waiter.timer);
    if (message.ok) waiter.resolve(message);
    else waiter.reject(new HostError(message.error || "LDM reported an error"));
    scheduleIdle();
  });
  port.onDisconnect.addListener((p) => {
    const reason = (p && p.error && p.error.message) || api.runtime.lastError?.message || "bridge disconnected";
    const notInstalled = /not found|no such native application|access to the specified native messaging host is forbidden/i.test(reason);
    const error = new HostError(notInstalled ? "LDM browser bridge is not installed — run: ldm install-browser-integration" : reason,
      notInstalled);
    for (const waiter of pending.values()) {
      clearTimeout(waiter.timer);
      waiter.reject(error);
    }
    pending.clear();
    port = null;
  });
}

function scheduleIdle() {
  clearTimeout(idleTimer);
  idleTimer = setTimeout(() => {
    if (port && pending.size === 0) {
      port.disconnect();
      port = null;
    }
  }, IDLE_MS);
}

/** Send one command to the host; resolves with its reply or rejects with HostError. */
export function request(command) {
  if (!port) connect();
  const id = ++nextId;
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      pending.delete(id);
      reject(new HostError("LDM did not answer in time"));
    }, TIMEOUT_MS);
    pending.set(id, { resolve, reject, timer });
    try {
      port.postMessage({ id, ...command });
    } catch (error) {
      clearTimeout(timer);
      pending.delete(id);
      reject(new HostError(String(error)));
    }
  });
}
