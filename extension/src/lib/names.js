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
