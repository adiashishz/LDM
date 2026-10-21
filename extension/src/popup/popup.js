import { api } from "../lib/api.js";
import { filterLinks, filenameFromUrl, humanSize, linkGroup } from "../lib/names.js";

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
  const items = await call({ type: "captures", tabId });
  $("media-count").textContent = items.length ? `(${items.length})` : "";
  $("media-empty").hidden = items.length > 0;
  $("media").replaceChildren(...items.map((item) => {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = "Download";
    button.title = item.stream ? "Stream playlist: LDM downloads all pieces and joins them" : "";
    button.addEventListener("click", () => send([{ url: item.url, filename: item.stream ? "" : item.filename,
      referer: item.referer, size: item.size, pageUrl, kind: item.stream ? "media" : "" }], button));
    const meta = [humanSize(item.size), item.mime, new URL(item.url).hostname].filter(Boolean).join(" · ");
    return row(item.filename || item.url, meta, item.kind, button);
  }));
}

// --- links tab -----------------------------------------------------------------------------

function visibleLinks() {
  return filterLinks(links, $("group").value, $("search").value);
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
    const li = row(filenameFromUrl(link.url) || link.url, link.text || link.url, linkGroup(link.url), box);
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
  $("page-video").addEventListener("click", () => send([{ url: pageUrl, pageUrl, kind: "media" }], $("page-video")));
  $("page-video").disabled = !/^https?:/i.test(pageUrl);
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
}

main();
