"""File-type groups for the list filter (same groups as the browser extension's classify.js)."""

from __future__ import annotations

from pathlib import PurePosixPath
from urllib.parse import unquote, urlsplit

GROUPS = {
    "video": {"mp4", "webm", "mkv", "mov", "avi", "flv", "m4v", "3gp", "ogv", "wmv", "mpg", "mpeg", "ts"},
    "audio": {"mp3", "m4a", "aac", "ogg", "oga", "opus", "flac", "wav", "wma"},
    "archive": {"zip", "rar", "7z", "tar", "gz", "tgz", "bz2", "xz", "zst", "iso", "img", "dmg"},
    "document": {"pdf", "epub", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "odt", "ods", "odp", "txt", "csv"},
    "program": {"exe", "msi", "deb", "rpm", "apk", "appimage", "flatpak", "flatpakref", "jar", "bin", "run"},
    "image": {"jpg", "jpeg", "png", "gif", "webp", "svg", "bmp", "avif", "heic", "tif", "tiff"},
}
LABELS = {"video": "Videos", "audio": "Music", "archive": "Archives", "document": "Documents",
          "program": "Programs", "image": "Pictures", "torrent": "Torrents", "other": "Other"}


def group_of(kind: str, name: str, url: str = "") -> str:
    """Group of a download: torrents by kind, videos from yt-dlp, else by file extension."""
    if kind == "torrent":
        return "torrent"
    suffix = PurePosixPath(name or unquote(urlsplit(url).path)).suffix.lstrip(".").lower()
    for group, extensions in GROUPS.items():
        if suffix in extensions:
            return group
    return "video" if kind == "media" else "other"
