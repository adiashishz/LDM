// What kind of file a URL / response is. Pure functions (unit-tested with node --test).

export const GROUPS = {
  video: ["mp4", "webm", "mkv", "mov", "avi", "flv", "m4v", "3gp", "ogv", "wmv", "mpg", "mpeg"],
  audio: ["mp3", "m4a", "aac", "ogg", "oga", "opus", "flac", "wav", "wma"],
  hls: ["m3u8"],
  dash: ["mpd"],
  subtitle: ["vtt", "srt", "ass", "ssa"],
  archive: ["zip", "rar", "7z", "tar", "gz", "tgz", "bz2", "xz", "zst", "iso", "img", "dmg"],
  document: ["pdf", "epub", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "odt", "ods", "odp", "txt", "csv"],
  program: ["exe", "msi", "deb", "rpm", "apk", "appimage", "flatpak", "flatpakref", "jar", "bin", "run", "torrent"],
  image: ["jpg", "jpeg", "png", "gif", "webp", "svg", "bmp", "avif", "heic", "tif", "tiff"],
};

// Kinds the sniffer reports (other kinds come from links or download interception).
export const MEDIA_KINDS = new Set(["video", "audio", "hls", "dash", "subtitle"]);
export const STREAM_KINDS = new Set(["hls", "dash"]);

/** Largest first; items without a known size (stream playlists) after them, in the order caught. */
export function bySize(items) {
  return [...items].sort((a, b) => (b.size ?? -1) - (a.size ?? -1));
}

/**
 * Media caught on a tab, for "download video from this page" (1DM's grabber): video, audio and
 * stream playlists, largest first, so LDM can offer them when yt-dlp doesn't know the site.
 */
export function pageMedia(captures, limit = 20) {
  return bySize(captures.filter((c) => ["video", "audio", "hls", "dash"].includes(c.kind)))
    .slice(0, limit)
    .map(({ url, kind, size, mime, filename, referer, headers }) => ({
      url, kind, size: size || null, mime, filename, referer, headers: headers || {},
    }));
}

// Pieces of adaptive streams: never useful on their own.
const SEGMENT_EXT = new Set(["ts", "m4s", "m4f", "cmfv", "cmfa", "m4v_seg"]);
const MIN_MEDIA_BYTES = 50 * 1024;

const MIME_KINDS = [
  [/^(application|audio)\/(vnd\.apple\.mpegurl|x-mpegurl|mpegurl)$/, "hls"],
  [/^application\/dash\+xml$/, "dash"],
  [/^(text\/vtt|application\/x-subrip|text\/x-ssa)$/, "subtitle"],
  [/^video\/mp2t$/, null],
  [/^video\//, "video"],
  [/^audio\//, "audio"],
];

export function extensionOf(url) {
  try {
    const name = new URL(url).pathname.split("/").pop() || "";
    const dot = name.lastIndexOf(".");
    return dot > 0 ? name.slice(dot + 1).toLowerCase() : "";
  } catch {
    return "";
  }
}

export function groupOfExtension(ext) {
  for (const [group, exts] of Object.entries(GROUPS)) {
    if (exts.includes(ext)) return group;
  }
  return null;
}

function bareMime(mime) {
  return (mime || "").split(";")[0].trim().toLowerCase();
}

export function isSegment(url, mime) {
  return SEGMENT_EXT.has(extensionOf(url)) || bareMime(mime) === "video/mp2t" || isSubtitleSegment(url);
}

/** Pieces of a stream's subtitle track (seg-3.vtt, segment12.webvtt, 0004.vtt): not files on their own. */
export function isSubtitleSegment(url) {
  let name = "";
  try {
    name = new URL(url).pathname.split("/").pop().toLowerCase();
  } catch {
    return false;
  }
  return /^(seg|segment|chunk|frag|fragment|part|sub)?[-_]?\d+\.(vtt|webvtt)$/.test(name);
}

/** Kind of a URL with optional Content-Type: one of GROUPS' keys, or null. */
export function classify(url, mime = "") {
  if (isSegment(url, mime)) return null;
  const type = bareMime(mime);
  for (const [pattern, kind] of MIME_KINDS) {
    if (pattern.test(type)) return kind;
  }
  return groupOfExtension(extensionOf(url));
}

/**
 * Sniffer decision for one response. Returns {kind, size} or null.
 * status: HTTP status; headers: lower-case name -> value.
 */
export function sniff(url, status, headers) {
  if (![200, 206].includes(status)) return null;
  const kind = classify(url, headers["content-type"]);
  if (!kind || !MEDIA_KINDS.has(kind)) return null;
  let size = null;
  const range = /bytes\s+(\d+)-\d+\/(\d+)/i.exec(headers["content-range"] || "");
  if (range) {
    if (range[1] !== "0") return null; // a later fragment of a file we already saw
    size = Number(range[2]);
  } else if (headers["content-length"]) {
    size = Number(headers["content-length"]);
  }
  if ((kind === "video" || kind === "audio") && size !== null && size < MIN_MEDIA_BYTES) return null;
  return { kind, size };
}
