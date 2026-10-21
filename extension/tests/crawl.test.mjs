import { test } from "node:test";
import assert from "node:assert/strict";

import { checkSizes, crawl, extractLinks, looksLikePage, sameSite, siteOf } from "../src/lib/crawl.js";
import { linksBySize } from "../src/lib/names.js";

// A small website: index -> two section pages -> deeper pages, plus links to files and other sites.
const SITE = {
  "https://www.example.com/": `<a href="/music/">Music</a> <a href="docs/index.html">Docs</a>
    <a href="https://other.org/page">elsewhere</a> <a href="/top.zip">Top archive (1.2 GB)</a>
    <img src="/logo.png"> <a href="mailto:x@example.com">mail</a>`,
  "https://www.example.com/music/": `<a href="song1.mp3">Song <b>one</b></a> <a href="/music/more/">More</a>
    <a href="https://cdn.example.com/song2.mp3">Song two</a> <a href="/top.zip#x">again</a>`,
  "https://www.example.com/docs/index.html": `<a href="manual.pdf?v=2">Manual</a>`,
  "https://www.example.com/music/more/": `<a href="deep.flac">Deep</a> <a href="/music/more/deeper/">Deeper</a>`,
  "https://www.example.com/music/more/deeper/": `<a href="deepest.mp3">Deepest</a>`,
};

const fetchPage = async (url) => SITE[url] ?? null;

test("links and their text from a page", () => {
  const links = extractLinks(SITE["https://www.example.com/music/"], "https://www.example.com/music/");
  assert.deepEqual(links.map((l) => [l.url, l.text]), [
    ["https://www.example.com/music/song1.mp3", "Song one"],
    ["https://www.example.com/music/more/", "More"],
    ["https://cdn.example.com/song2.mp3", "Song two"],
    ["https://www.example.com/top.zip", "again"],
  ]);
});

test("same site and page detection", () => {
  const site = siteOf("https://www.example.com/a");
  assert.equal(site, "example.com");
  assert.equal(sameSite(site, "https://cdn.example.com/x"), true);
  assert.equal(sameSite(site, "https://notexample.com/x"), false);
  assert.equal(looksLikePage("https://e.com/a/"), true);
  assert.equal(looksLikePage("https://e.com/a.php?x=1"), true);
  assert.equal(looksLikePage("https://e.com/a.zip"), false);
});

test("crawl collects files level by level, on the same site only", async () => {
  const level1 = await crawl({ start: "https://www.example.com/", depth: 1, fetchPage });
  assert.deepEqual(level1.map((f) => f.url).sort(), [
    "https://cdn.example.com/song2.mp3", "https://www.example.com/docs/manual.pdf?v=2",
    "https://www.example.com/logo.png", "https://www.example.com/music/song1.mp3", "https://www.example.com/top.zip",
  ]);
  assert.equal(level1.find((f) => f.url.endsWith("song1.mp3")).foundOn, "https://www.example.com/music/");
  const level3 = await crawl({ start: "https://www.example.com/", depth: 3, fetchPage });
  assert.ok(level3.some((f) => f.url.endsWith("deepest.mp3")));
  const asked = [];
  await crawl({ start: "https://www.example.com/", depth: 3, maxPages: 2,
    fetchPage: async (url) => { asked.push(url); return SITE[url]; } });
  assert.equal(asked.length, 2);
  assert.ok(!asked.some((u) => u.includes("other.org")));
});

test("a crawl can be stopped", async () => {
  const controller = new AbortController();
  let pages = 0;
  await crawl({ start: "https://www.example.com/", depth: 3, concurrency: 1, signal: controller.signal,
    fetchPage: async (url) => { pages += 1; controller.abort(); return SITE[url]; } });
  assert.equal(pages, 1);
});

test("sizes from the server, then sorted largest first", async () => {
  const headers = (h) => ({ get: (name) => h[name.toLowerCase()] ?? null });
  const request = async (url, method) => {
    if (url.endsWith("big.iso")) return { status: 200, headers: headers({ "content-length": "5000" }) };
    if (url.endsWith("ranged.bin")) {
      return method === "HEAD" ? { status: 405, headers: headers({}) }
        : { status: 206, headers: headers({ "content-range": "bytes 0-0/9000" }) };
    }
    throw new Error("offline");
  };
  const links = [{ url: "https://e/big.iso" }, { url: "https://e/gone.bin" }, { url: "https://e/ranged.bin" },
    { url: "https://e/text.zip", text: "(1 KB)" }];
  await checkSizes(links.slice(0, 3), request);
  assert.deepEqual(links.slice(0, 3).map((l) => l.size), [5000, null, 9000]);
  assert.deepEqual(linksBySize(links).map((l) => l.url),
    ["https://e/ranged.bin", "https://e/big.iso", "https://e/text.zip", "https://e/gone.bin"]);
});
