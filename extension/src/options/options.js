import { getSettings, saveSettings } from "../lib/api.js";

const $ = (id) => document.getElementById(id);
const MIB = 1024 * 1024;

async function load() {
  const s = await getSettings();
  $("intercept").checked = s.intercept;
  $("minSize").value = s.minSize / MIB;
  $("excludeExtensions").value = s.excludeExtensions;
  $("excludeSites").value = s.excludeSites;
  $("sniff").checked = s.sniff;
  document.querySelector(`input[name=mode][value=${s.mode}]`).checked = true;
}

async function save() {
  await saveSettings({
    intercept: $("intercept").checked,
    minSize: Math.max(0, Number($("minSize").value) || 0) * MIB,
    excludeExtensions: $("excludeExtensions").value,
    excludeSites: $("excludeSites").value,
    mode: document.querySelector("input[name=mode]:checked").value,
    sniff: $("sniff").checked,
  });
  $("saved").hidden = false;
  setTimeout(() => { $("saved").hidden = true; }, 1500);
}

$("save").addEventListener("click", save);
load();
