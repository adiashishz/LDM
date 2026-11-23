"""What the Preferences offer: choices, switches and number fields, with their labels (for any toolkit)."""

import sys

BACKUP_SCHEDULES = [("off", "Never"), ("daily", "Every day"), ("weekly", "Every week")]

QUALITIES = [("best", "Best available"), (2160, "2160p (4K)"), (1440, "1440p"), (1080, "1080p"), (720, "720p"),
             (480, "480p"), (360, "360p"), ("audio", "Audio only")]

CONTAINERS = [("mp4", "MP4 (plays everywhere)"), ("mkv", "MKV (keeps every codec)")]

ENCRYPTION = [("prefer", "Prefer encrypted connections"), ("require", "Encrypted connections only"),
              ("off", "No encryption")]

TORRENT_SWITCHES = {
    "torrent_dht": ("DHT", "Find peers without trackers"),
    "torrent_pex": ("Peer exchange", "Learn about peers from other peers"),
    "torrent_lsd": ("Local peer discovery", "Find peers on your local network"),
    "torrent_upnp": ("Port forwarding", "Open the port on your router with UPnP / NAT-PMP"),
    "torrent_utp": ("µTP", "Low-latency transport that yields to other traffic"),
    "torrent_fetch_metadata": ("Get file list before adding", "For magnet links"),
    "torrent_sequential": ("Download in order by default", "Lets you play videos while they download"),
    "torrent_seed": ("Seed after finishing", "Keep uploading to other peers"),
    "torrent_announce_all": ("Contact all trackers", "At once, instead of one at a time"),
    "torrent_merge_duplicates": ("Add trackers to torrents already in the list", "When you add one again"),
    "torrent_tracker_list": ("Add a public tracker list", "Fetched daily from the address below; more peers"),
}

TORRENT_SPINS = {  # key -> (title, subtitle, min, max, step, digits)
    "torrent_listen_port": ("Listening port", "0 picks a random port", 0, 65535, 1, 0),
    "torrent_max_peers": ("Peers per torrent", "", 1, 500, 1, 0),
    "torrent_max_connections": ("Connections in total", "", 1, 2000, 1, 0),
    "torrent_seed_ratio": ("Stop seeding at ratio", "0 means no limit", 0, 100, 0.1, 1),
    "torrent_seed_minutes": ("Stop seeding after (minutes)", "0 means no limit", 0, 100000, 10, 0),
}

SYSTEM_SWITCHES = {
    "pause_on_metered": ("Pause on metered connections", "Mobile data, hotspots and Wi-Fi marked as metered"),
    "prevent_sleep": ("Keep the computer awake", "While files are downloading"),
    "autostart": ("Start the download service at login", "Scheduled downloads run even if LDM isn't open"),
    "notify_when_closed": ("Notifications when LDM is closed", "For finished and failed downloads"),
    "tray_icon": ("Show progress in the tray", "While downloading, on desktops with a tray (GNOME needs the "
                                                "AppIndicator extension)"),
    "clipboard_watch": ("Offer to download copied links", "When LDM gets focus, and in the background "
                                                          "on desktops that allow it"),
}
if sys.platform == "win32":  # what these do on Windows; metered connections aren't detected there (yet)
    del SYSTEM_SWITCHES["pause_on_metered"]
    SYSTEM_SWITCHES["notify_when_closed"] = ("Notifications", "For finished and failed downloads (they need the "
                                                              "tray icon)")
    SYSTEM_SWITCHES["tray_icon"] = ("Show LDM in the tray", "Progress, Pause All, Resume All and Quit")
    SYSTEM_SWITCHES["clipboard_watch"] = ("Offer to download copied links", "When LDM gets focus")

PASSWORDS_WHERE = "Windows Credential Manager" if sys.platform == "win32" else "your keyring"

NETWORK_SWITCHES = {
    "always_retry": ("Always retry", "Keep retrying failed downloads instead of stopping after the retries above"),
    "restart_non_resumable": ("Start over when resuming isn't possible", "For servers that can't continue a "
                                                                          "download; off: ask first"),
}

ADDING_SWITCHES = {
    "skip_add_dialog": ("Add without asking", "Links from the browser, the clipboard and drag-and-drop start at "
                                               "once with the defaults"),
    "open_html_in_browser": ("Offer to open web pages in the browser", "When a link is a page, not a file"),
}

FINISHED_SWITCHES = {
    "remove_finished": ("Remove finished downloads from the list", "The files stay where they are"),
    "cleanup_finished_only": ("Clean up only finished downloads", "Otherwise unfinished ones are removed too, "
                                                                   "with their partial data"),
}

PROXY_MODES = [("system", "System settings"), ("none", "No proxy"), ("manual", "Manual")]

DNS_SERVERS = [("cloudflare", "Cloudflare (1.1.1.1)"), ("google", "Google (8.8.8.8)"), ("quad9", "Quad9 (9.9.9.9)"),
               ("custom", "Custom URL")]

USER_AGENTS = [("default", "LDM"), ("firefox", "Firefox"), ("chrome", "Chrome"), ("mobile", "Mobile (Android)"),
               ("custom", "Custom")]

SCHEDULE_SWITCHES = {
    "schedule_new_links": ("Put new downloads on the schedule", "You can still change it when adding"),
}

# setting key -> (title, subtitle, minimum, maximum)
SPIN_SETTINGS = {
    "max_parallel": ("Parallel downloads", "How many files download at the same time", 1, 20),
    "default_connections": ("Connections per download", "Default for new downloads", 1, 32),
    "retry_count": ("Automatic retries", "After a failed download", 0, 20),
    "retry_interval": ("Retry delay", "Seconds before retrying", 0, 3600),
}
