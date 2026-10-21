#!/usr/bin/env python3
"""ldm native messaging host: bridges the browser extension to ldm over D-Bus.

Standalone on purpose: Python standard library + the `gdbus` command only, so the same file runs
for RPM browsers and inside a Flatpak browser's sandbox (copied there by
`ldm install-browser-integration`). It makes no network requests and never logs cookies.

Protocol (browser native messaging): each message is a 4-byte native-endian length followed by
UTF-8 JSON. Requests carry an "id"; replies echo it with "ok": true/false.
"""

import json
import struct
import subprocess
import sys

VERSION = "0.1.0"
# Keep in sync with ldm/busnames.py (checked by tests/test_nmhost.py).
BUS_NAME = "io.github.adiashishz.LDM.Service"
OBJECT_PATH = "/io/github/adiashishz/LDM/Manager1"
INTERFACE = "io.github.adiashishz.LDM.Manager1"
GUI_NAME = "io.github.adiashishz.LDM"
GUI_PATH = "/io/github/adiashishz/LDM"
MAX_MESSAGE = 1024 * 1024
GDBUS_TIMEOUT = 30


class HostError(Exception):
    pass


# --- framing -------------------------------------------------------------------------------

def read_message(stream):
    header = stream.read(4)
    if len(header) < 4:
        return None
    (length,) = struct.unpack("=I", header)
    if length > MAX_MESSAGE:
        raise HostError(f"message too large: {length} bytes")
    return json.loads(stream.read(length).decode("utf-8"))


def write_message(stream, message) -> None:
    data = json.dumps(message).encode("utf-8")
    stream.write(struct.pack("=I", len(data)))
    stream.write(data)
    stream.flush()


# --- GVariant text for gdbus ---------------------------------------------------------------

def gv_str(value: str) -> str:
    # JSON string escapes (\" \\ \n \t \uXXXX) are valid GVariant text escapes.
    return json.dumps(str(value), ensure_ascii=False)


def gv_value(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return f"int64 {value}"
    if isinstance(value, dict):
        if not value:
            return "@a{ss} {}"
        return "{" + ", ".join(f"{gv_str(k)}: {gv_str(v)}" for k, v in value.items()) + "}"
    return gv_str(value)


def gv_vardict(values: dict) -> str:
    items = [f"{gv_str(k)}: <{gv_value(v)}>" for k, v in values.items() if v not in (None, "", {})]
    return "{" + ", ".join(items) + "}" if items else "@a{sv} {}"


def gdbus(dest: str, path: str, method: str, *args: str) -> str:
    """Call a method with `gdbus call` (D-Bus activation starts the service if needed)."""
    command = ["gdbus", "call", "--session", "--dest", dest, "--object-path", path,
               "--method", method, *args]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=GDBUS_TIMEOUT)
    except FileNotFoundError as e:
        raise HostError("gdbus is not installed") from e
    except subprocess.TimeoutExpired as e:
        raise HostError("ldm did not answer in time") from e
    if result.returncode != 0:
        raise HostError(_dbus_error(result.stderr))
    return result.stdout.strip()


def _dbus_error(stderr: str) -> str:
    line = (stderr.strip().splitlines() or ["unknown error"])[0].removeprefix("Error: ")
    if line.startswith("GDBus.Error:"):
        name, _, message = line.removeprefix("GDBus.Error:").partition(": ")
        if name in ("org.freedesktop.DBus.Error.ServiceUnknown", "org.freedesktop.DBus.Error.NameHasNoOwner"):
            return "ldm is not installed for D-Bus activation (run: ldm install-browser-integration)"
        return message or name
    return line


def _uint64(reply: str) -> int:
    # "(uint64 7,)"
    return int(reply.strip("(),").split()[-1])


# --- commands ------------------------------------------------------------------------------

def has_owner(name: str) -> bool:
    reply = gdbus("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus.NameHasOwner",
                  gv_str(name))
    return reply.startswith("(true")


def headers_for(item: dict) -> dict:
    headers = {}
    for key, header in (("referer", "Referer"), ("origin", "Origin"), ("cookies", "Cookie"),
                        ("user_agent", "User-Agent")):
        if item.get(key):
            headers[header] = item[key]
    return headers


def add_direct(item: dict) -> int:
    options = {"name": item.get("filename"), "headers": headers_for(item), "kind": item.get("kind")}
    return _uint64(gdbus(BUS_NAME, OBJECT_PATH, f"{INTERFACE}.Add", gv_str(item["url"]), gv_vardict(options)))


def add_dialog(item: dict) -> None:
    gdbus(BUS_NAME, OBJECT_PATH, f"{INTERFACE}.GetSettings")  # starts the daemon first
    request = {"url": item["url"], "filename": item.get("filename"), "headers": headers_for(item),
               "size": item.get("size"), "page_url": item.get("page_url"), "kind": item.get("kind"),
               "captures": item.get("captures") or []}  # media the extension caught on the page
    gdbus(GUI_NAME, GUI_PATH, "org.gtk.Actions.Activate", gv_str("add-request"),
          f"[<{gv_str(json.dumps(request))}>]", "@a{sv} {}")


def handle(message: dict) -> dict:
    command = message.get("cmd")
    if command == "ping":
        return {"daemon": has_owner(BUS_NAME), "gui": has_owner(GUI_NAME), "host_version": VERSION}
    if command == "add":
        if not message.get("url"):
            raise HostError("missing url")
        if message.get("mode") == "direct":
            return {"download_id": add_direct(message)}
        add_dialog(message)
        return {"dialog": True}
    if command == "add_many":
        results = []
        for item in message.get("items", []):
            try:
                results.append({"url": item.get("url"), "download_id": add_direct(item)})
            except (HostError, KeyError) as e:
                results.append({"url": item.get("url"), "error": str(e)})
        return {"results": results}
    raise HostError(f"unknown command: {command}")


def main() -> int:
    stdin, stdout = sys.stdin.buffer, sys.stdout.buffer
    while True:
        try:
            message = read_message(stdin)
        except (HostError, ValueError) as e:
            write_message(stdout, {"ok": False, "error": str(e)})
            return 1
        if message is None:
            return 0
        reply = {"id": message.get("id")}
        try:
            reply.update(handle(message), ok=True)
        except HostError as e:
            reply.update(ok=False, error=str(e))
        write_message(stdout, reply)


if __name__ == "__main__":
    sys.exit(main())
