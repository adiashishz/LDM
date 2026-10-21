"""Build the extension for each browser family: dist/firefox and dist/chromium (+ .zip files).

manifest.base.json holds the shared manifest plus "firefox"/"chromium" sections that are merged in.
"""

import json
import shutil
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TARGETS = ("firefox", "chromium")


def build(target: str) -> Path:
    base = json.loads((ROOT / "manifest.base.json").read_text())
    manifest = {k: v for k, v in base.items() if k not in TARGETS}
    manifest.update(base[target])
    out = ROOT / "dist" / target
    shutil.rmtree(out, ignore_errors=True)
    shutil.copytree(ROOT / "src", out)
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
    archive = ROOT / "dist" / f"ldm-{target}-{manifest['version']}.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as z:
        for path in sorted(out.rglob("*")):
            if path.is_file():
                z.write(path, path.relative_to(out))
    return out


if __name__ == "__main__":
    for name in sys.argv[1:] or TARGETS:
        print("built", build(name))
