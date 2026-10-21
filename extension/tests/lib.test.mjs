import { test } from "node:test";
import assert from "node:assert/strict";

import { classify, extensionOf, sniff } from "../src/lib/classify.js";
import {
  cookieHeader, filenameFromDisposition, filenameFromUrl, filterLinks, humanSize, shouldIntercept,
} from "../src/lib/names.js";

test("classify by mime and extension", () => {
  assert.equal(classify("https://x/a.m3u8"), "hls");
  assert.equal(classify("https://x/play", "application/vnd.apple.mpegurl; charset=utf-8"), "hls");
  assert.equal(classify("https://x/manifest", "application/dash+xml"), "dash");
  assert.equal(classify("https://x/v", "video/mp4"), "video");
  assert.equal(classify("https://x/seg-12.ts", "video/mp2t"), null);
  assert.equal(classify("https://x/chunk.m4s"), null);
  assert.equal(classify("https://x/sub.vtt"), "subtitle");
  assert.equal(classify("https://x/setup.EXE"), "program");
  assert.equal(classify("https://x/page"), null);
  assert.equal(extensionOf("https://x/a/b.tar.gz?x=1"), "gz");
});

test("sniff keeps whole media and playlists, skips fragments and tiny files", () => {
  assert.deepEqual(sniff("https://x/v.mp4", 200, { "content-type": "video/mp4", "content-length": "5000000" }),
    { kind: "video", size: 5000000 });
  assert.deepEqual(sniff("https://x/v.mp4", 206, { "content-type": "video/mp4", "content-range": "bytes 0-999/8000000" }),
    { kind: "video", size: 8000000 });
  assert.equal(sniff("https://x/v.mp4", 206, { "content-type": "video/mp4", "content-range": "bytes 1000-1999/8000000" }), null);
  assert.equal(sniff("https://x/v.mp4", 200, { "content-type": "video/mp4", "content-length": "1000" }), null);
  assert.deepEqual(sniff("https://x/m.m3u8", 200, { "content-type": "application/x-mpegurl" }), { kind: "hls", size: null });
  assert.equal(sniff("https://x/a.zip", 200, { "content-type": "application/zip" }), null);
  assert.equal(sniff("https://x/v.mp4", 404, {}), null);
});

test("file names", () => {
  assert.equal(filenameFromDisposition('attachment; filename="report final.pdf"'), "report final.pdf");
  assert.equal(filenameFromDisposition("attachment; filename*=UTF-8''r%C3%A9sum%C3%A9.pdf; filename=\"resume.pdf\""), "résumé.pdf");
  assert.equal(filenameFromDisposition('inline; filename="../../etc/passwd"'), "_.._etc_passwd");
  assert.equal(filenameFromDisposition(""), "");
  assert.equal(filenameFromUrl("https://x/dir/My%20File.iso?y=2"), "My File.iso");
});

test("cookie header and sizes", () => {
  assert.equal(cookieHeader([{ name: "a", value: "1" }, { name: "b", value: "2" }]), "a=1; b=2");
  assert.equal(humanSize(512), "512 B");
  assert.equal(humanSize(1024 * 1024 * 3), "3.0 MiB");
  assert.equal(humanSize(null), "");
});

test("link filtering", () => {
  const links = [
    { url: "https://x/a.zip", text: "Archive" },
    { url: "https://x/b.mp4", text: "Clip" },
    { url: "https://x/about", text: "About us" },
    { url: "https://x/c.pdf", text: "Manual" },
  ];
  assert.deepEqual(filterLinks(links).map((l) => l.url), ["https://x/a.zip", "https://x/b.mp4", "https://x/c.pdf"]);
  assert.deepEqual(filterLinks(links, "video").map((l) => l.url), ["https://x/b.mp4"]);
  assert.deepEqual(filterLinks(links, "all", "manual").map((l) => l.url), ["https://x/c.pdf"]);
  assert.equal(filterLinks(links, "all").length, 4);
});

test("download interception rules", () => {
  const settings = { intercept: true, minSize: 1024 * 1024, excludeExtensions: "pdf, .torrent", excludeSites: "example.org" };
  assert.equal(shouldIntercept({ url: "https://x/a.iso", fileSize: 5e9 }, settings), true);
  assert.equal(shouldIntercept({ url: "https://x/a.iso", fileSize: -1, totalBytes: 0 }, settings), true); // unknown size
  assert.equal(shouldIntercept({ url: "https://x/small.zip", fileSize: 10 }, settings), false);
  assert.equal(shouldIntercept({ url: "https://x/doc.pdf", fileSize: 5e7 }, settings), false);
  assert.equal(shouldIntercept({ url: "https://cdn.example.org/a.iso", fileSize: 5e9 }, settings), false);
  assert.equal(shouldIntercept({ url: "blob:https://x/123", fileSize: 5e9 }, settings), false);
  assert.equal(shouldIntercept({ url: "https://x/a.iso" }, { ...settings, intercept: false }), false);
});
