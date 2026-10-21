// Injected on demand by the Links tab: every http(s) URL on the page with its text.
// The value of the last expression is returned to scripting.executeScript.
(() => {
  const out = [];
  const seen = new Set();
  const add = (url, text, tag) => {
    try {
      const u = new URL(url, document.baseURI);
      if (!/^https?:$/.test(u.protocol) || seen.has(u.href)) return;
      seen.add(u.href);
      out.push({ url: u.href, text: (text || "").replace(/\s+/g, " ").trim().slice(0, 200), tag });
    } catch {
      /* not a URL */
    }
  };
  for (const a of document.querySelectorAll("a[href]")) add(a.href, a.textContent || a.title, "a");
  for (const img of document.querySelectorAll("img[src]")) add(img.currentSrc || img.src, img.alt, "img");
  for (const el of document.querySelectorAll("video[src], audio[src], source[src], track[src]")) {
    add(el.src, el.title || el.label || "", el.tagName.toLowerCase());
  }
  return out;
})();
