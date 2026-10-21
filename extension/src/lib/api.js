// Firefox exposes `browser` (promises); Chrome MV3 exposes `chrome` (promises for most APIs).
export const api = globalThis.browser ?? globalThis.chrome;

export const DEFAULT_SETTINGS = {
  intercept: true,           // hand browser downloads to ldm
  minSize: 1024 * 1024,      // only intercept files at least this big (unknown size: always)
  excludeExtensions: "",     // e.g. "pdf, torrent"
  excludeSites: "",          // e.g. "example.org" (also matches subdomains)
  mode: "dialog",            // "dialog": open ldm's add dialog; "direct": queue immediately
  sniff: true,               // list media/playlists/subtitles found on pages
};

export async function getSettings() {
  const stored = await api.storage.local.get("settings");
  return { ...DEFAULT_SETTINGS, ...(stored.settings || {}) };
}

export async function saveSettings(settings) {
  await api.storage.local.set({ settings });
}
