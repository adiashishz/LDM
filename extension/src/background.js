// Background: download interception, context menus, media sniffing, hand-off to ldm.

import { api, getSettings } from "./lib/api.js";
import { addCapture, clearCaptures, listCaptures } from "./lib/capture.js";
import { sniff, STREAM_KINDS } from "./lib/classify.js";
import { HostError, request } from "./lib/host.js";
import { cookieHeader, filenameFromDisposition, filenameFromUrl, shouldIntercept } from "./lib/names.js";

// --- hand-off ------------------------------------------------------------------------------

async function cookiesFor(url) {
  try {
    return cookieHeader(await api.cookies.getAll({ url }));
  } catch {
    return "";
  }
}

/** Build the bridge item for one URL: filename, referer, cookies, user agent. */
async function itemFor({ url, filename = "", referer = "", size = null, pageUrl = "" }) {
  return {
    url,
    filename: filename || "",
    referer: referer || pageUrl || "",
    cookies: await cookiesFor(url),
    user_agent: navigator.userAgent,
    size,
    page_url: pageUrl || "",
  };
}

async function sendOne(target) {
  const settings = await getSettings();
  const reply = await request({ cmd: "add", mode: settings.mode, ...(await itemFor(target)) });
  if (settings.mode === "direct") notify("Sent to ldm", target.filename || filenameFromUrl(target.url) || target.url);
  return reply;
}

async function sendMany(targets) {
  const items = await Promise.all(targets.map(itemFor));
  const reply = await request({ cmd: "add_many", items });
  const failed = reply.results.filter((r) => r.error);
  notify("Sent to ldm", `${reply.results.length - failed.length} of ${reply.results.length} added`
    + (failed.length ? ` — ${failed[0].error}` : ""));
  return reply;
}

function notify(title, message) {
  api.notifications.create({ type: "basic", iconUrl: api.runtime.getURL("icons/icon-128.png"), title, message })
    .catch(() => {});
}

function reportError(error) {
  notify("ldm", error instanceof HostError ? error.message : `Could not send to ldm: ${error}`);
}

// --- download interception -----------------------------------------------------------------

const ownDownloads = new Set(); // downloads we restarted in the browser after a failed hand-off

api.downloads.onCreated.addListener(async (item) => {
  if (ownDownloads.has(item.url) || item.byExtensionId === api.runtime.id) return;
  const settings = await getSettings();
  if (!shouldIntercept(item, settings)) return;
  const url = item.finalUrl || item.url;
  try {
    await api.downloads.cancel(item.id);
    await api.downloads.erase({ id: item.id });
  } catch {
    return; // already finished or gone: leave it to the browser
  }
  const filename = (item.filename || "").split("/").pop();
  try {
    await sendOne({ url, filename, referer: item.referrer || "", size: item.fileSize > 0 ? item.fileSize : null });
  } catch (error) {
    reportError(error);
    ownDownloads.add(item.url);
    api.downloads.download({ url: item.url }).finally(() => setTimeout(() => ownDownloads.delete(item.url), 60000));
  }
});

// --- context menus -------------------------------------------------------------------------

async function createMenus() {
  await api.contextMenus.removeAll(); // both browsers return a promise in MV3
  api.contextMenus.create({ id: "ldm-link", title: "Download link with ldm", contexts: ["link"] });
  api.contextMenus.create({ id: "ldm-media", title: "Download with ldm", contexts: ["image", "video", "audio"] });
  api.contextMenus.create({ id: "ldm-page", title: "Download links on this page with ldm…", contexts: ["page"] });
}

api.runtime.onInstalled.addListener(createMenus);
api.runtime.onStartup.addListener(createMenus);

api.contextMenus.onClicked.addListener((info, tab) => {
  const pageUrl = info.pageUrl || tab?.url || "";
  if (info.menuItemId === "ldm-link") {
    sendOne({ url: info.linkUrl, pageUrl }).catch(reportError);
  } else if (info.menuItemId === "ldm-media") {
    sendOne({ url: info.srcUrl, pageUrl }).catch(reportError);
  } else if (info.menuItemId === "ldm-page" && tab) {
    api.windows.create({
      url: api.runtime.getURL(`popup/popup.html?tab=${tab.id}&view=links&window=1`),
      type: "popup", width: 520, height: 640,
    });
  }
});

// --- media sniffer -------------------------------------------------------------------------

api.webRequest.onHeadersReceived.addListener(
  (details) => {
    if (details.tabId < 0) return;
    const headers = {};
    for (const h of details.responseHeaders || []) headers[h.name.toLowerCase()] = h.value || "";
    const found = sniff(details.url, details.statusCode, headers);
    if (!found) return;
    getSettings().then((settings) => {
      if (!settings.sniff) return;
      addCapture(details.tabId, {
        url: details.url,
        kind: found.kind,
        size: found.size,
        mime: (headers["content-type"] || "").split(";")[0],
        filename: filenameFromDisposition(headers["content-disposition"]) || filenameFromUrl(details.url),
        referer: details.documentUrl || details.initiator || details.originUrl || "",
        stream: STREAM_KINDS.has(found.kind),
      });
    });
  },
  { urls: ["<all_urls>"], types: ["media", "xmlhttprequest", "object", "other"] },
  ["responseHeaders"],
);

api.tabs.onUpdated.addListener((tabId, change) => {
  if (change.status === "loading" && change.url) clearCaptures(tabId);
});
api.tabs.onRemoved.addListener((tabId) => clearCaptures(tabId));

// --- messages from the popup ---------------------------------------------------------------

async function collectLinks(tabId) {
  const results = await api.scripting.executeScript({ target: { tabId, allFrames: true }, files: ["content/links.js"] });
  const seen = new Set();
  const links = [];
  for (const frame of results || []) {
    for (const link of frame.result || []) {
      if (!seen.has(link.url)) {
        seen.add(link.url);
        links.push(link);
      }
    }
  }
  return links;
}

async function onPopupMessage(message) {
  switch (message.type) {
    case "ping":
      return request({ cmd: "ping" });
    case "captures":
      return listCaptures(message.tabId);
    case "links":
      return collectLinks(message.tabId);
    case "send":
      return message.items.length === 1 ? sendOne(message.items[0]) : sendMany(message.items);
    default:
      throw new Error(`unknown message ${message.type}`);
  }
}

api.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  onPopupMessage(message).then(
    (result) => sendResponse({ ok: true, result }),
    (error) => sendResponse({ ok: false, error: error.message || String(error), notInstalled: !!error.notInstalled }),
  );
  return true; // async response
});
