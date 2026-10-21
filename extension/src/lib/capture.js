// Media found on each tab, kept in storage.session (survives the background going to sleep).

import { api } from "./api.js";

const MAX_PER_TAB = 200;
const key = (tabId) => `tab:${tabId}`;

export async function listCaptures(tabId) {
  const stored = await api.storage.session.get(key(tabId));
  return stored[key(tabId)] || [];
}

export async function addCapture(tabId, entry) {
  const items = await listCaptures(tabId);
  const existing = items.find((item) => item.url === entry.url);
  if (existing) {
    if (entry.size && !existing.size) existing.size = entry.size;
  } else {
    items.push({ ...entry, seen: Date.now() });
    if (items.length > MAX_PER_TAB) items.shift();
  }
  await api.storage.session.set({ [key(tabId)]: items });
  await setBadge(tabId, items.length);
}

export async function clearCaptures(tabId) {
  await api.storage.session.remove(key(tabId));
  await setBadge(tabId, 0);
}

async function setBadge(tabId, count) {
  try {
    await api.action.setBadgeBackgroundColor({ tabId, color: "#3584e4" });
    await api.action.setBadgeText({ tabId, text: count ? String(count) : "" });
  } catch {
    /* tab already closed */
  }
}
