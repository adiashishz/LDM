// 1DM's grabber recursion: follow links on the same site a few levels deep and collect file links.
// Pure functions plus a crawler that is given its fetch function (unit-tested with node --test).

import { extensionOf, groupOfExtension } from "./classify.js";

export const MAX_PAGES = 500;
const PAGE_EXTENSIONS = new Set(["", "html", "htm", "php", "asp", "aspx", "jsp", "shtml", "cgi", "pl"]);

/** The site a crawl stays on: the page's host without "www."; subdomains count as the same site. */
export function siteOf(url) {
  try {
    return new URL(url).hostname.replace(/^www\./, "").toLowerCase();
  } catch {
    return "";
  }
}

export function sameSite(site, url) {
  const host = siteOf(url);
  return host === site || host.endsWith(`.${site}`);
}

function textOf(html) {
  return html.replace(/<[^>]*>/g, " ").replace(/&nbsp;/g, " ").replace(/&amp;/g, "&").replace(/\s+/g, " ").trim();
}

/** Links in a page: <a href> with their text, plus other href/src attributes. */
export function extractLinks(html, baseUrl) {
  const found = new Map();
  const add = (raw, text) => {
    try {
      const url = new URL(raw.replace(/&amp;/g, "&"), baseUrl);
      url.hash = "";
      if (!/^https?:$/.test(url.protocol)) return;
      const existing = found.get(url.href);
      if (!existing || (!existing.text && text)) found.set(url.href, { url: url.href, text: text.slice(0, 200) });
    } catch {
      /* not a URL */
    }
  };
  for (const m of html.matchAll(/<a\b[^>]*?\bhref\s*=\s*["']([^"']+)["'][^>]*>([\s\S]*?)<\/a>/gi)) {
    add(m[1], textOf(m[2]));
  }
  for (const m of html.matchAll(/\b(?:href|src)\s*=\s*["']([^"']+)["']/gi)) add(m[1], "");
  return [...found.values()];
}

/** A link worth opening as a page (no file extension, or a page one like .html/.php). */
export function looksLikePage(url) {
  return PAGE_EXTENSIONS.has(extensionOf(url)) && !groupOfExtension(extensionOf(url));
}

/**
 * Breadth-first crawl from `start`, `depth` levels, same site only.
 * fetchPage(url) -> html string or null (not a page / failed). Returns the file links found
 * (each with `foundOn`), in the order found. `onProgress({pages, files, queued})` is called as it goes.
 */
export async function crawl({ start, depth, fetchPage, maxPages = MAX_PAGES, concurrency = 4,
  onProgress = () => {}, signal = null }) {
  const site = siteOf(start);
  const seen = new Set([start]);
  const files = new Map();
  let level = [start];
  let pages = 0;
  for (let d = 0; d <= depth && level.length && pages < maxPages; d++) {
    const next = [];
    const queue = level.slice(0, maxPages - pages);
    const worker = async () => {
      while (queue.length && !signal?.aborted) {
        const url = queue.shift();
        const html = await fetchPage(url);
        pages += 1;
        if (html) {
          for (const link of extractLinks(html, url)) {
            if (groupOfExtension(extensionOf(link.url))) {
              if (!files.has(link.url)) files.set(link.url, { ...link, foundOn: url });
            } else if (d < depth && looksLikePage(link.url) && sameSite(site, link.url) && !seen.has(link.url)) {
              seen.add(link.url);
              next.push(link.url);
            }
          }
        }
        onProgress({ pages, files: files.size, queued: queue.length + next.length });
      }
    };
    await Promise.all(Array.from({ length: Math.min(concurrency, queue.length) }, worker));
    level = next;
  }
  return [...files.values()];
}

/**
 * Ask servers for file sizes (sets link.size, null if unknown): HEAD, else a one-byte range request.
 * request(url, method, headers) -> {status, headers} (a fetch Response works).
 */
export async function checkSizes(links, request, { concurrency = 6, onProgress = () => {} } = {}) {
  const queue = [...links];
  let done = 0;
  const worker = async () => {
    while (queue.length) {
      const link = queue.shift();
      link.size = await sizeOf(link.url, request);
      done += 1;
      onProgress(done);
    }
  };
  await Promise.all(Array.from({ length: Math.min(concurrency, queue.length) }, worker));
  return links;
}

async function sizeOf(url, request) {
  try {
    const head = await request(url, "HEAD");
    const length = Number(head.headers.get("content-length"));
    if (head.status < 400 && length > 0) return length;
    const ranged = await request(url, "GET", { Range: "bytes=0-0" });
    const total = /\/(\d+)\s*$/.exec(ranged.headers.get("content-range") || "");
    return total ? Number(total[1]) : null;
  } catch {
    return null;
  }
}
