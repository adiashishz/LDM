#!/usr/bin/env python3
"""ldm native messaging host: bridges the browser extension to ldm, over D-Bus (Linux) or the service's
local channel (Windows, macOS; on Linux with LDM_TRANSPORT=socket).

Standalone on purpose: Python standard library + the `gdbus` command only, so the same file runs
for RPM browsers and inside a Flatpak browser's sandbox (copied there by
`ldm install-browser-integration`). It makes no network requests and never logs cookies.

Protocol (browser native messaging): each message is a 4-byte native-endian length followed by
UTF-8 JSON. Requests carry an "id"; replies echo it with "ok": true/false.
"""

import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time

VERSION = "0.4.0"
# Keep in sync with ldm/busnames.py (checked by tests/test_nmhost.py).
BUS_NAME = "io.github.adiashishz.LDM.Service"
OBJECT_PATH = "/io/github/adiashishz/LDM/Manager1"
INTERFACE = "io.github.adiashishz.LDM.Manager1"
GUI_NAME = "io.github.adiashishz.LDM"
GUI_PATH = "/io/github/adiashishz/LDM"
MAX_MESSAGE = 1024 * 1024
GDBUS_TIMEOUT = 30
START_WAIT = 15  # seconds for the service or the window to come up (local channel)


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


# --- the local channel (keep in sync with ldm/channel.py) ----------------------------------------

def use_socket() -> bool:
    return os.environ.get("LDM_TRANSPORT") == "socket" or not sys.platform.startswith("linux")


def channel_address() -> str:
    if os.environ.get("LDM_SOCKET"):
        return os.environ["LDM_SOCKET"]
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Local")
        return os.path.join(base, "ldm", "service.json")
    return os.path.join(os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir(), "ldm", "service.sock")


def channel_open() -> socket.socket:
    """Connected to the service (raises OSError when it isn't running)."""
    address = channel_address()
    if sys.platform == "win32":
        try:
            with open(address) as f:
                data = json.load(f)
        except (OSError, ValueError) as e:
            raise ConnectionRefusedError(str(e)) from e
        sock = socket.create_connection(("127.0.0.1", int(data["port"])), timeout=GDBUS_TIMEOUT)
        sock.sendall(json.dumps({"hello": data["token"]}).encode() + b"\n")
        return sock
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(GDBUS_TIMEOUT)
    sock.connect(address)
    return sock


def channel_call(method: str, *params):
    """One call on the local channel; signals that arrive meanwhile are skipped."""
    with channel_open() as sock, sock.makefile("rb") as replies:
        sock.sendall(json.dumps({"id": 1, "method": method, "params": list(params)}).encode() + b"\n")
        for line in replies:
            reply = json.loads(line)
            if reply.get("id") == 1:
                if "error" in reply:
                    raise HostError(reply["error"].get("message") or reply["error"].get("name", "failed"))
                return reply.get("result")
    raise HostError("ldm's service closed the connection")


def launcher(variable: str, name: str) -> list:
    """How to start `ldm` or `ldm-gui`: LDM_COMMAND / LDM_GUI_COMMAND (installers set these), else on PATH."""
    if os.environ.get(variable):
        return os.environ[variable].split()
    found = shutil.which(name)
    if not found:
        raise HostError(f"can't start LDM: {name} not found")
    return [found]


def start(arguments: list) -> None:
    detached = ({"creationflags": subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP}
                if sys.platform == "win32" else {"start_new_session": True})
    subprocess.Popen(arguments, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     **detached)


def service_call(method: str, *params):
    """A call on the local channel, starting the service first if it isn't running."""
    try:
        return channel_call(method, *params)
    except OSError:
        start(launcher("LDM_COMMAND", "ldm") + ["daemon"])
    deadline = time.monotonic() + START_WAIT
    while True:
        try:
            return channel_call(method, *params)
        except OSError:
            if time.monotonic() > deadline:
                raise HostError("ldm's service did not start") from None
            time.sleep(0.2)


def channel_running() -> bool:
    try:
        channel_open().close()
        return True
    except OSError:
        return False


# --- commands ------------------------------------------------------------------------------

def has_owner(name: str) -> bool:
    reply = gdbus("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus.NameHasOwner",
                  gv_str(name))
    return reply.startswith("(true")


def header_name(name: str) -> str:
    """"accept-language" -> "Accept-Language"."""
    return "-".join(part.capitalize() for part in name.split("-"))


def headers_for(item: dict) -> dict:
    # Everything the browser sent for a grabbed file (signed stream links can check User-Agent, Accept-Language…),
    # then the item's own fields.
    headers = {header_name(k): v for k, v in (item.get("headers") or {}).items() if v}
    for key, header in (("referer", "Referer"), ("origin", "Origin"), ("cookies", "Cookie"),
                        ("user_agent", "User-Agent")):
        if item.get(key):
            headers[header] = item[key]
    return headers


def add_direct(item: dict) -> int:
    options = {"name": item.get("filename"), "headers": headers_for(item), "kind": item.get("kind")}
    if use_socket():
        return int(service_call("Add", item["url"], {k: v for k, v in options.items() if v not in (None, "", {})}))
    return _uint64(gdbus(BUS_NAME, OBJECT_PATH, f"{INTERFACE}.Add", gv_str(item["url"]), gv_vardict(options)))


def add_dialog(item: dict) -> None:
    request = {"url": item["url"], "filename": item.get("filename"), "headers": headers_for(item),
               "size": item.get("size"), "page_url": item.get("page_url"), "kind": item.get("kind"),
               "captures": item.get("captures") or []}  # media the extension caught on the page
    if use_socket():
        # The service hands it to the window; no window open: start it, and ask again once it connects.
        if service_call("ShowAddDialog", request):
            return
        start(launcher("LDM_GUI_COMMAND", "ldm-gui"))
        deadline = time.monotonic() + START_WAIT
        while not service_call("ShowAddDialog", request):
            if time.monotonic() > deadline:
                raise HostError("LDM's window did not open")
            time.sleep(0.3)
        return
    gdbus(BUS_NAME, OBJECT_PATH, f"{INTERFACE}.GetSettings")  # starts the daemon first
    gdbus(GUI_NAME, GUI_PATH, "org.gtk.Actions.Activate", gv_str("add-request"),
          f"[<{gv_str(json.dumps(request))}>]", "@a{sv} {}")


def handle(message: dict) -> dict:
    command = message.get("cmd")
    if command == "ping":
        if use_socket():
            running = channel_running()
            return {"daemon": running, "gui": bool(running and channel_call("WindowOpen")),
                    "host_version": VERSION}
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
