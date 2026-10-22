// File names, sizes, cookie headers, and link filtering. Pure functions (unit-tested).

import { classify, groupOfExtension, extensionOf } from "./classify.js";

/** Filename from a Content-Disposition header, or "". Prefers RFC 5987 filename*. */
export function filenameFromDisposition(header) {
  if (!header) return "";
  const extended = /filename\*\s*=\s*([^']*)'[^']*'([^;]+)/i.exec(header);
  if (extended) {
    try {
      return sanitize(decodeURIComponent(extended[2].trim().replace(/^"|"$/g, "")));
    } catch {
      /* fall through */
    }
  }
  const plain = /filename\s*=\s*("([^"]*)"|[^;]+)/i.exec(header);
  return plain ? sanitize((plain[2] ?? plain[1]).trim()) : "";
}

export function filenameFromUrl(url) {
  try {
    const last = new URL(url).pathname.split("/").pop() || "";
    return sanitize(decodeURIComponent(last));
  } catch {
    return "";
  }
}

export function sanitize(name) {
  return name.replace(/[/\\\0]/g, "_").replace(/^\.+/, "").trim().slice(0, 240);
}

export function cookieHeader(cookies) {
  return cookies.map((c) => `${c.name}=${c.value}`).join("; ");
}

// Request headers that say who is asking. Signed stream links can be bound to them (one host answers 403
// unless User-Agent and Accept-Language match the browser's), so LDM replays them as the browser sent them.
// Transport headers (Range, Accept-Encoding, Connection, Host) are left to LDM.
const BROWSER_HEADERS = new Set([
  "accept", "accept-language", "cookie", "origin", "referer", "user-agent",
  "sec-ch-ua", "sec-ch-ua-mobile", "sec-ch-ua-platform", "sec-fetch-dest", "sec-fetch-mode", "sec-fetch-site",
]);

/** Lower-case name -> value for the identifying headers of a webRequest `requestHeaders` list. */
export function browserHeaders(requestHeaders) {
  const kept = {};
  for (const h of requestHeaders || []) {
    const name = h.name.toLowerCase();
    if (BROWSER_HEADERS.has(name)) kept[name] = h.value || "";
  }
  return kept;
}

export function humanSize(bytes) {
  if (bytes === null || bytes === undefined || bytes < 0) return "";
  const units = ["B", "KiB", "MiB", "GiB", "TiB"];
  let n = bytes;
  let unit = 0;
  while (n >= 1000 && unit < units.length - 1) {
    n /= 1024;
    unit += 1;
  }
  return unit === 0 ? `${n} B` : `${n.toFixed(1)} ${units[unit]}`;
}

/** Group used by the Links tab filter: a GROUPS key, or "page" for links without a file extension. */
export function linkGroup(url) {
  return classify(url) ?? (extensionOf(url) ? "other" : "page");
}

/**
 * Filter collected links. group: "files" (anything that looks like a file), a GROUPS key, or "all";
 * text: case-insensitive match on URL or link text.
 */
export function filterLinks(links, group = "files", text = "") {
  const needle = text.trim().toLowerCase();
  return links.filter((link) => {
    const g = linkGroup(link.url);
    if (group === "files" && (g === "page" || g === "other" && !groupOfExtension(extensionOf(link.url)))) return false;
    if (!["files", "all"].includes(group) && g !== group) return false;
    return !needle || link.url.toLowerCase().includes(needle) || (link.text || "").toLowerCase().includes(needle);
  });
}

const SIZE_UNITS = { kb: 1e3, mb: 1e6, gb: 1e9, tb: 1e12, kib: 1024, mib: 1024 ** 2, gib: 1024 ** 3, tib: 1024 ** 4 };

/** A file size written in a link's text ("90.55 MB", "1,5 GB", "700MiB", "1,234 MB"), in bytes, or null. */
export function sizeHint(text) {
  const plain = (text || "").replace(/(\d),(\d{3})(?!\d)/g, "$1$2"); // thousands separators
  const match = /(\d+(?:[.,]\d+)?)\s*(tib|gib|mib|kib|tb|gb|mb|kb)\b/i.exec(plain);
  if (!match) return null;
  return Math.round(parseFloat(match[1].replace(",", ".")) * SIZE_UNITS[match[2].toLowerCase()]);
}

/** Largest first: the size the server reported ("Check sizes"), else one written in the link text;
 *  links of unknown size after them, in page order. */
export function linksBySize(links) {
  const size = (link) => link.size ?? sizeHint(link.text) ?? -1;
  return [...links].sort((a, b) => size(b) - size(a));
}

/** Should a browser download be handed to ldm? */
export function shouldIntercept(item, settings) {
  const url = item.finalUrl || item.url || "";
  if (!settings.intercept || !/^https?:/i.test(url)) return false;
  const size = item.fileSize > 0 ? item.fileSize : item.totalBytes > 0 ? item.totalBytes : null;
  if (size !== null && size < settings.minSize) return false;
  const ext = extensionOf(url) || (item.filename || "").split(".").pop().toLowerCase();
  const excludedExt = splitList(settings.excludeExtensions);
  if (ext && excludedExt.includes(ext)) return false;
  let host = "";
  try {
    host = new URL(url).hostname;
  } catch {
    return false;
  }
  return !splitList(settings.excludeSites).some((site) => host === site || host.endsWith(`.${site}`));
}

export function splitList(text) {
  return (text || "").split(/[\s,]+/).map((s) => s.trim().toLowerCase().replace(/^\./, "")).filter(Boolean);
}
