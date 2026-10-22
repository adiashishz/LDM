import { test } from "node:test";
import assert from "node:assert/strict";

import { bySize, classify, extensionOf, isSubtitleSegment, pageMedia, sniff } from "../src/lib/classify.js";
import {
  browserHeaders, cookieHeader, filenameFromDisposition, filenameFromUrl, filterLinks, humanSize, linksBySize, shouldIntercept,
  sizeHint,
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

test("page media for sites yt-dlp doesn't know: biggest files first, streams (no size) after", () => {
  const caught = [
    { url: "https://cdn/x/small.mp4", kind: "video", size: 1000, seen: 1 },
    { url: "https://cdn/x/sub.vtt", kind: "subtitle", size: 10 },
    { url: "https://cdn/x/big.mp4", kind: "video", size: 9000, mime: "video/mp4", filename: "big.mp4", referer: "https://site/v" },
    { url: "https://cdn/x/master.m3u8", kind: "hls", size: null },
    { url: "https://cdn/x/track.m4a", kind: "audio", size: 500 },
  ];
  const media = pageMedia(caught);
  assert.deepEqual(media.map((m) => m.url), ["https://cdn/x/big.mp4", "https://cdn/x/small.mp4",
    "https://cdn/x/track.m4a", "https://cdn/x/master.m3u8"]);
  assert.deepEqual(media[0], { url: "https://cdn/x/big.mp4", kind: "video", size: 9000, mime: "video/mp4",
    filename: "big.mp4", referer: "https://site/v", headers: {} });
  assert.equal(pageMedia(caught, 2).length, 2);
});

test("subtitle pieces of a stream are not offered as files", () => {
  for (const url of ["https://h/x/seg-2.vtt", "https://h/x/segment12.webvtt", "https://h/x/0004.vtt",
    "https://h/x/chunk_7.vtt?token=a"]) {
    assert.equal(isSubtitleSegment(url), true, url);
    assert.equal(sniff(url, 200, { "content-type": "text/vtt", "content-length": "300" }), null, url);
  }
  assert.equal(isSubtitleSegment("https://h/x/english.vtt"), false);
  assert.deepEqual(sniff("https://h/x/english.vtt", 200, { "content-type": "text/vtt" }), { kind: "subtitle", size: null });
});

test("the Media tab lists the largest files first", () => {
  const caught = [{ url: "a", size: 10 }, { url: "b", size: null }, { url: "c", size: 300 }, { url: "d" },
    { url: "e", size: 300 }];
  assert.deepEqual(bySize(caught).map((c) => c.url), ["c", "e", "a", "b", "d"]);
  assert.deepEqual(caught.map((c) => c.url), ["a", "b", "c", "d", "e"]);  // the original list is untouched
});

test("sizes written in link text", () => {
  assert.equal(sizeHint("MP4 (360p, h264, 170.54 MB)"), 170540000);
  assert.equal(sizeHint("Download (1,5 GB)"), 1500000000);
  assert.equal(sizeHint("ISO 1,234 MB"), 1234000000);
  assert.equal(sizeHint("700MiB"), 700 * 1024 ** 2);
  assert.equal(sizeHint("Episode 4"), null);
  assert.equal(sizeHint(""), null);
});

test("the Links tab lists the largest files first", () => {
  const links = [{ url: "a", text: "240p (69.7 MB)" }, { url: "b", text: "picture" },
    { url: "c", text: "1080p (1.2 GB)" }, { url: "d", text: "360p (170.54 MB)" }, { url: "e" }];
  assert.deepEqual(linksBySize(links).map((l) => l.url), ["c", "d", "a", "b", "e"]);
});

test("browser headers kept for grabbed files: who is asking, not transport", () => {
  const sent = [
    { name: "User-Agent", value: "Brave UA" }, { name: "Accept-Language", value: "en-US,en;q=0.8" },
    { name: "Accept", value: "*/*" }, { name: "Origin", value: "https://site" }, { name: "Referer", value: "https://site/" },
    { name: "Cookie", value: "a=1" }, { name: "sec-ch-ua-platform", value: '"Linux"' }, { name: "Sec-Fetch-Mode", value: "cors" },
    { name: "Range", value: "bytes=0-" }, { name: "Accept-Encoding", value: "gzip, br" }, { name: "Connection", value: "keep-alive" },
  ];
  assert.deepEqual(browserHeaders(sent), {
    "user-agent": "Brave UA", "accept-language": "en-US,en;q=0.8", accept: "*/*", origin: "https://site",
    referer: "https://site/", cookie: "a=1", "sec-ch-ua-platform": '"Linux"', "sec-fetch-mode": "cors",
  });
  assert.deepEqual(browserHeaders(undefined), {});
});
