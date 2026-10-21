import { api } from "../lib/api.js";
import { bySize } from "../lib/classify.js";
import { checkSizes, crawl } from "../lib/crawl.js";
import { filterLinks, filenameFromUrl, humanSize, linkGroup, linksBySize } from "../lib/names.js";

const params = new URLSearchParams(location.search);
const $ = (id) => document.getElementById(id);
let tabId = Number(params.get("tab")) || null;
let pageUrl = "";
let links = [];
const selected = new Set();

async function call(message) {
  const reply = await api.runtime.sendMessage(message);
  if (!reply.ok) {
    const error = new Error(reply.error);
    error.notInstalled = reply.notInstalled;
    throw error;
  }
  return reply.result;
}

function setStatus(text, isError = false) {
  $("status").textContent = text;
  $("status").classList.toggle("error", isError);
}

async function checkStatus() {
  try {
    const state = await call({ type: "ping" });
    setStatus(state.daemon ? "LDM is running" : "LDM will start when you download something");
  } catch (error) {
    setStatus(error.message, true);
  }
}

async function send(items, button) {
  if (button) button.disabled = true;
  try {
    await call({ type: "send", items });
    setStatus(items.length === 1 ? "Sent to LDM" : `Sent ${items.length} files to LDM`);
  } catch (error) {
    setStatus(error.message, true);
  } finally {
    if (button) button.disabled = false;
  }
}

function row(name, meta, kind, action) {
  const li = document.createElement("li");
  const info = document.createElement("div");
  info.className = "info";
  const title = document.createElement("div");
  title.className = "name";
  title.textContent = name;
  title.title = name;
  const sub = document.createElement("div");
  sub.className = "meta";
  sub.textContent = meta;
  sub.title = meta;
  info.append(title, sub);
  const badge = document.createElement("span");
  badge.className = "kind";
  badge.textContent = kind;
  li.append(info, badge, action);
  return li;
}

// --- media tab -----------------------------------------------------------------------------

async function showMedia() {
  const items = bySize(await call({ type: "captures", tabId }));  // largest first
  $("media-count").textContent = items.length ? `(${items.length})` : "";
  $("media-empty").hidden = items.length > 0;
  $("media").replaceChildren(...items.map((item) => {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = "Download";
    button.title = item.stream ? "Stream playlist: LDM downloads all pieces and joins them" : "";
    button.addEventListener("click", () => send([{ url: item.url, filename: item.stream ? "" : item.filename,
      referer: item.referer, size: item.size, pageUrl, kind: item.stream ? "media" : "",
      headers: item.headers || {} }], button));
    const meta = [humanSize(item.size), item.mime, new URL(item.url).hostname].filter(Boolean).join(" · ");
    return row(item.filename || item.url, meta, item.kind, button);
  }));
}

// --- links tab -----------------------------------------------------------------------------

function visibleLinks() {
  return linksBySize(filterLinks(links, $("group").value, $("search").value));  // largest first
}

function renderLinks() {
  const visible = visibleLinks();
  $("links-empty").hidden = visible.length > 0;
  $("links-count").textContent = `(${visible.length})`;
  $("links").replaceChildren(...visible.map((link) => {
    const box = document.createElement("input");
    box.type = "checkbox";
    box.checked = selected.has(link.url);
    box.addEventListener("change", () => {
      if (box.checked) selected.add(link.url);
      else selected.delete(link.url);
      updateSendButton();
    });
    const meta = [humanSize(link.size), link.text || link.url].filter(Boolean).join(" · ");
    const li = row(filenameFromUrl(link.url) || link.url, meta, linkGroup(link.url), box);
    li.querySelector(".info").addEventListener("click", () => box.click());
    return li;
  }));
  $("select-all").checked = visible.length > 0 && visible.every((l) => selected.has(l.url));
  updateSendButton();
}

function updateSendButton() {
  const count = links.filter((l) => selected.has(l.url)).length;
  $("send-selected").disabled = count === 0;
  $("send-selected").textContent = count ? `Download selected (${count})` : "Download selected";
}

async function loadLinks() {
  try {
    links = await call({ type: "links", tabId });
  } catch (error) {
    setStatus(`Cannot read links on this page: ${error.message}`, true);
    links = [];
  }
  renderLinks();
}

// --- go deeper (crawl the site) and check sizes ---------------------------------------------

const PAGE_LIMIT = 3 * 1024 * 1024;
let crawling = null; // AbortController while a crawl runs

function crawlStatus(text) {
  $("crawl-status").hidden = !text;
  $("crawl-status").textContent = text;
}

async function timed(url, init = {}, seconds = 15) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), seconds * 1000);
  try {
    return await fetch(url, { credentials: "include", redirect: "follow", ...init, signal: controller.signal });
  } finally {
    clearTimeout(timer);
  }
}

async function fetchPage(url) {
  try {
    const response = await timed(url);
    const type = response.headers.get("content-type") || "";
    if (!response.ok || !/html|xml/i.test(type) || Number(response.headers.get("content-length")) > PAGE_LIMIT) {
      response.body?.cancel();
      return null;
    }
    return (await response.text()).slice(0, PAGE_LIMIT);
  } catch {
    return null;
  }
}

function goDeeper() {
  const depth = Number($("depth").value);
  if (!document.body.classList.contains("window")) {
    // The toolbar popup closes when it loses focus: crawl in LDM's own window instead.
    api.windows.create({
      url: api.runtime.getURL(`popup/popup.html?tab=${tabId}&view=links&window=1&crawl=${depth}`),
      type: "popup", width: 560, height: 680,
    });
    window.close();
    return;
  }
  startCrawl(depth);
}

async function startCrawl(depth) {
  if (crawling) {
    crawling.abort();
    return;
  }
  crawling = new AbortController();
  $("go-deeper").textContent = "Stop";
  if (!links.length) await loadLinks();
  const known = new Set(links.map((l) => l.url));
  try {
    const found = await crawl({
      start: pageUrl, depth, fetchPage, signal: crawling.signal,
      onProgress: ({ pages, files }) => crawlStatus(`Looking through the site… ${pages} pages, ${files} files`),
    });
    const fresh = found.filter((l) => !known.has(l.url));
    links = links.concat(fresh);
    crawlStatus(`${crawling.signal.aborted ? "Stopped" : "Done"}: ${fresh.length} more files found`);
    renderLinks();
  } finally {
    crawling = null;
    $("go-deeper").textContent = "Go deeper";
  }
}

async function checkVisibleSizes() {
  const pending = visibleLinks().filter((l) => l.size === undefined).slice(0, 300);
  if (!pending.length) return;
  $("check-sizes").disabled = true;
  await checkSizes(pending, async (url, method, headers = {}) => {
    const response = await timed(url, { method, headers });
    response.body?.cancel(); // only the headers are needed
    return response;
  }, { onProgress: (done) => crawlStatus(`Checking sizes… ${done} of ${pending.length}`) });
  crawlStatus(`Sizes checked for ${pending.length} files`);
  $("check-sizes").disabled = false;
  renderLinks();
}

// --- setup ---------------------------------------------------------------------------------

function showView(name) {
  for (const view of ["media", "links"]) {
    $(`view-${view}`).hidden = view !== name;
    $(`tab-${view}`).classList.toggle("active", view === name);
  }
  if (name === "links" && links.length === 0) loadLinks();
}

async function main() {
  if (params.get("window")) document.body.classList.add("window");
  if (!tabId) {
    const [tab] = await api.tabs.query({ active: true, currentWindow: true });
    tabId = tab?.id;
    pageUrl = tab?.url || "";
  } else {
    pageUrl = (await api.tabs.get(tabId)).url || "";
  }
  $("tab-media").addEventListener("click", () => showView("media"));
  $("tab-links").addEventListener("click", () => showView("links"));
  $("options").addEventListener("click", () => api.runtime.openOptionsPage());
  $("page-video").addEventListener("click", () => send([{ url: pageUrl, pageUrl, kind: "media", tabId }], $("page-video")));
  $("page-video").disabled = !/^https?:/i.test(pageUrl);
  $("go-deeper").addEventListener("click", goDeeper);
  $("check-sizes").addEventListener("click", checkVisibleSizes);
  $("group").addEventListener("change", renderLinks);
  $("search").addEventListener("input", renderLinks);
  $("select-all").addEventListener("change", () => {
    for (const link of visibleLinks()) {
      if ($("select-all").checked) selected.add(link.url);
      else selected.delete(link.url);
    }
    renderLinks();
  });
  $("send-selected").addEventListener("click", () => {
    const items = links.filter((l) => selected.has(l.url)).map((l) => ({ url: l.url, pageUrl, referer: pageUrl }));
    send(items, $("send-selected"));
  });
  checkStatus();
  await showMedia();
  showView(params.get("view") === "links" ? "links" : "media");
  if (params.get("crawl")) {
    $("depth").value = params.get("crawl");
    startCrawl(Number(params.get("crawl")));
  }
}

main();
