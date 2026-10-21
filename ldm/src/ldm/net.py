"""Network settings for LDM's HTTP requests: proxy, DNS-over-HTTPS, timeouts, user agent and site logins.

`NetConfig.from_settings()` turns the settings into one object; `resolve(url)` picks the proxy for a
URL (system proxy settings via Gio when available); `client()` builds the httpx client.
DNS-over-HTTPS replaces name lookups for LDM's own connections only (1DM did it with a local VPN);
with a proxy the proxy resolves names, so DoH is not used then.
"""

from __future__ import annotations

import asyncio
import dataclasses
import ipaddress
import logging
import socket
import time
from typing import Any, Callable, Iterable
from urllib.parse import quote, urlsplit, urlunsplit

import httpcore
import httpx

log = logging.getLogger(__name__)

DEFAULT_USER_AGENT = "ldm/0.1"
USER_AGENTS = {
    "default": DEFAULT_USER_AGENT,
    "firefox": "Mozilla/5.0 (X11; Linux x86_64; rv:144.0) Gecko/20100101 Firefox/144.0",
    "chrome": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36",
    "mobile": ("Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) "
               "Chrome/141.0.0.0 Mobile Safari/537.36"),
}
DOH_PRESETS = {
    "cloudflare": "https://cloudflare-dns.com/dns-query",
    "google": "https://dns.google/dns-query",
    "quad9": "https://dns.quad9.net/dns-query",
}
PROXY_SCHEMES = ("http", "https", "socks5", "socks5h")
HIDDEN_HEADERS = {"cookie", "authorization", "proxy-authorization"}

Lookup = Callable[[str], "tuple[str, str] | None"]  # host -> (user, password)
EventCallback = Callable[[str], None]


def user_agent(setting: str) -> str:
    """A preset name or a custom string."""
    return USER_AGENTS.get(setting, setting) or DEFAULT_USER_AGENT


def doh_endpoint(setting: str) -> str:
    return DOH_PRESETS.get(setting, setting)


def check_proxy_url(url: str) -> None:
    parts = urlsplit(url)
    if parts.scheme not in PROXY_SCHEMES or not parts.hostname or not parts.port:
        raise ValueError("proxy must look like http://host:port or socks5://host:port")


def with_login(proxy: str, user: str, password: str | None) -> str:
    if not user:
        return proxy
    parts = urlsplit(proxy)
    login = quote(user, safe="") + (":" + quote(password, safe="") if password else "")
    return urlunsplit(parts._replace(netloc=f"{login}@{parts.hostname}:{parts.port}"))


def system_proxy(url: str) -> str | None:
    """The desktop's proxy for `url` (GNOME/KDE settings through Gio), or None for a direct connection.
    Blocking (a PAC script may be fetched): call it from a thread."""
    try:
        import gi
        gi.require_version("Gio", "2.0")
        from gi.repository import Gio
        _keep_running_without_bus(Gio)
        found = Gio.ProxyResolver.get_default().lookup(url, None)
    except Exception as e:  # no gi, or the resolver failed: environment variables still apply
        log.debug("system proxy lookup failed: %s", e)
        return None
    for proxy in found or []:
        if proxy.startswith("direct://"):
            return None
        proxy = proxy.replace("socks://", "socks5://", 1)
        if urlsplit(proxy).scheme in PROXY_SCHEMES:
            return proxy
    return None


def environment_proxy(url: str) -> str | None:
    """HTTP(S)_PROXY / ALL_PROXY / NO_PROXY, as other command-line tools read them."""
    import urllib.request

    parts = urlsplit(url)
    proxies = urllib.request.getproxies_environment()
    if not proxies or urllib.request.proxy_bypass_environment(parts.hostname or "", proxies):
        return None
    proxy = proxies.get(parts.scheme) or proxies.get("all")
    return proxy.replace("socks://", "socks5://", 1) if proxy else None


_session_bus = None  # GLib's shared connection, held so it isn't recreated with the default


def _keep_running_without_bus(Gio) -> None:
    """The proxy lookup uses GLib's shared session-bus connection, which by default ends the process
    with SIGTERM when the bus goes away. The service watches its own bus connection and shuts down
    cleanly (saving progress), so GLib must not decide that. GLib only keeps the shared connection
    while someone holds it (else the lookup makes a fresh one with the default), so it is held here,
    and replaced after the bus restarted."""
    global _session_bus
    try:
        if _session_bus is None or _session_bus.is_closed():
            _session_bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        _session_bus.set_exit_on_close(False)
    except Exception:  # no session bus: nothing to guard against
        pass


def safe_headers(headers: Iterable[tuple[str, str]]) -> str:
    return ", ".join(f"{k}: {'…' if k.lower() in HIDDEN_HEADERS else v}" for k, v in headers)


@dataclasses.dataclass(frozen=True)
class NetConfig:
    proxy_mode: str = "none"          # "none" | "system" | "manual"
    proxy: str | None = None          # manual proxy (with login), or the system proxy once resolved
    user_agent: str = DEFAULT_USER_AGENT
    connect_timeout: float = 15.0
    read_timeout: float = 30.0
    doh_url: str | None = None
    logins: Lookup | None = None      # site logins (Basic/Digest) by host
    resolved: bool = False

    @classmethod
    def from_settings(cls, settings: dict[str, Any], proxy_password: str | None = None,
                      logins: Lookup | None = None) -> NetConfig:
        manual = settings["proxy_mode"] == "manual" and settings["proxy_url"]
        return cls(
            proxy_mode=settings["proxy_mode"] if manual or settings["proxy_mode"] != "manual" else "none",
            proxy=with_login(settings["proxy_url"], settings["proxy_user"], proxy_password) if manual else None,
            user_agent=user_agent(settings["user_agent"]),
            connect_timeout=float(settings["connect_timeout"]),
            read_timeout=float(settings["read_timeout"]),
            doh_url=doh_endpoint(settings["doh_url"]) if settings["dns_mode"] == "doh" else None,
            logins=logins,
        )

    def resolve_sync(self, url: str) -> NetConfig:
        """Pick the proxy for this URL (system mode asks the desktop)."""
        if self.resolved:
            return self
        proxy = self.proxy
        if self.proxy_mode == "system":
            proxy = system_proxy(url) or environment_proxy(url)
        return dataclasses.replace(self, proxy=proxy, resolved=True)

    async def resolve(self, url: str) -> NetConfig:
        if self.proxy_mode != "system" or self.resolved:
            return dataclasses.replace(self, resolved=True)
        return await asyncio.get_running_loop().run_in_executor(None, self.resolve_sync, url)

    def client(self, headers: dict[str, str] | None = None, max_connections: int = 2,
               on_event: EventCallback | None = None) -> httpx.AsyncClient:
        """An httpx client for LDM's requests; `headers` (Referer, Cookie, …) override the defaults."""
        transport = httpx.AsyncHTTPTransport(
            limits=httpx.Limits(max_connections=max_connections),
            proxy=self.proxy,
            trust_env=False,  # the proxy was already chosen in resolve()
        )
        if self.doh_url and not self.proxy:
            pool = transport._pool  # httpx has no public hook for name resolution
            pool._network_backend = DoHBackend(self.doh_url, pool._network_backend, on_event,
                                               timeout=self.connect_timeout)
        hooks: dict[str, list] = {}
        if on_event:
            if self.proxy:
                on_event(f"using proxy {urlsplit(self.proxy).scheme}://{urlsplit(self.proxy).hostname}"
                         f":{urlsplit(self.proxy).port}")

            async def on_request(request: httpx.Request) -> None:
                on_event(f"→ {request.method} {request.url}  [{safe_headers(request.headers.items())}]")

            async def on_response(response: httpx.Response) -> None:
                on_event(f"← HTTP {response.status_code} {response.reason_phrase}  "
                         f"[{safe_headers(response.headers.items())}]")

            hooks = {"request": [on_request], "response": [on_response]}
        return httpx.AsyncClient(
            follow_redirects=True,
            headers={"User-Agent": self.user_agent, "Accept-Encoding": "identity", **(headers or {})},
            timeout=httpx.Timeout(self.read_timeout, connect=self.connect_timeout),
            transport=transport,
            auth=HostAuth(self.logins) if self.logins else None,
            event_hooks=hooks,
        )


class HostAuth(httpx.Auth):
    """Answers HTTP 401 with the saved login for that host (Basic or Digest, as the server asks)."""

    def __init__(self, logins: Lookup):
        self.logins = logins

    def auth_flow(self, request: httpx.Request):
        response = yield request
        if response.status_code != 401:
            return
        login = self.logins(request.url.host)
        if login is None:
            return
        challenge = response.headers.get("www-authenticate", "")
        if challenge.lower().startswith("digest"):
            yield from httpx.DigestAuth(*login).auth_flow(request)  # sends again, answers its challenge
        else:
            yield from httpx.BasicAuth(*login).auth_flow(request)


class DoHBackend(httpcore.AsyncNetworkBackend):
    """Resolves host names with DNS-over-HTTPS (RFC 8484), then connects through the default backend."""

    def __init__(self, url: str, inner: httpcore.AsyncNetworkBackend, on_event: EventCallback | None = None,
                 timeout: float = 15.0):
        self.url = url
        self.inner = inner
        self.on_event = on_event
        self.timeout = timeout

    async def connect_tcp(self, host: str, port: int, timeout: float | None = None, local_address: str | None = None,
                          socket_options=None) -> httpcore.AsyncNetworkStream:
        if _is_literal(host):
            return await self.inner.connect_tcp(host, port, timeout, local_address, socket_options)
        addresses = await resolve_doh(self.url, host, self.timeout)
        if self.on_event:
            self.on_event(f"DNS-over-HTTPS: {host} → {', '.join(addresses)}")
        error: Exception | None = None
        for address in addresses:
            try:
                return await self.inner.connect_tcp(address, port, timeout, local_address, socket_options)
            except (httpcore.ConnectError, httpcore.ConnectTimeout, OSError) as e:
                error = e
        raise httpcore.ConnectError(f"could not connect to {host} ({', '.join(addresses)}): {error}")

    async def connect_unix_socket(self, path: str, timeout: float | None = None, socket_options=None):
        return await self.inner.connect_unix_socket(path, timeout, socket_options)

    async def sleep(self, seconds: float) -> None:
        await self.inner.sleep(seconds)


_cache: dict[tuple[str, str], tuple[list[str], float]] = {}  # (doh url, host) -> (addresses, expires)


def _is_literal(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        ipaddress.ip_address(host.strip("[]"))
        return True
    except ValueError:
        return False


DOH_HEADERS = {"Content-Type": "application/dns-message", "Accept": "application/dns-message"}


def _doh_queries(host: str) -> list[tuple[int, bytes]]:
    import dns.message
    import dns.rdatatype

    queries = []
    for rdtype in (dns.rdatatype.A, dns.rdatatype.AAAA):
        query = dns.message.make_query(host, rdtype)
        query.id = 0  # RFC 8484: cache friendly
        queries.append((rdtype, query.to_wire()))
    return queries


def _doh_answer(content: bytes, rdtype: int) -> tuple[list[str], int]:
    import dns.message

    addresses, ttl = [], 300
    for rrset in dns.message.from_wire(content).answer:
        if rrset.rdtype == rdtype:
            addresses.extend(item.address for item in rrset)
            ttl = min(ttl, rrset.ttl)
    return addresses, ttl


def _cached(url: str, host: str) -> list[str] | None:
    cached = _cache.get((url, host))
    return cached[0] if cached and cached[1] > time.monotonic() else None


def _remember(url: str, host: str, answers: list[tuple[list[str], int]]) -> list[str]:
    addresses = [a for found, _ in answers for a in found]
    if not addresses:
        raise httpcore.ConnectError(f"DNS-over-HTTPS: no address for {host}")
    ttl = min(t for found, t in answers if found)
    _cache[(url, host)] = (addresses, time.monotonic() + max(ttl, 30))
    return addresses


async def resolve_doh(url: str, host: str, timeout: float = 15.0) -> list[str]:
    """A and AAAA records for `host` from the DoH server at `url` (cached for their TTL).
    The DoH server itself is reached with normal DNS."""
    if (cached := _cached(url, host)) is not None:
        return cached
    answers = []
    async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
        for rdtype, wire in _doh_queries(host):
            response = await client.post(url, content=wire, headers=DOH_HEADERS)
            response.raise_for_status()
            answers.append(_doh_answer(response.content, rdtype))
    return _remember(url, host, answers)


def resolve_doh_sync(url: str, host: str, timeout: float = 15.0) -> list[str]:
    """The same for blocking code (yt-dlp's threads)."""
    if (cached := _cached(url, host)) is not None:
        return cached
    answers = []
    with httpx.Client(timeout=timeout, trust_env=False) as client:
        for rdtype, wire in _doh_queries(host):
            response = client.post(url, content=wire, headers=DOH_HEADERS)
            response.raise_for_status()
            answers.append(_doh_answer(response.content, rdtype))
    return _remember(url, host, answers)


# --- DNS-over-HTTPS for the whole service ---------------------------------------------------
# yt-dlp makes its own connections (and has no DNS option), so with DoH on the service's own
# name lookups go through it. Only this process is affected; libtorrent has its own resolver.

_system_getaddrinfo = socket.getaddrinfo
_process_doh: tuple[str, float] | None = None  # (DoH URL, timeout)


def doh_available() -> bool:
    import importlib.util
    return importlib.util.find_spec("dns") is not None


def use_doh_for_process(url: str | None, timeout: float = 15.0) -> None:
    """Route this process's name lookups through DoH (None: back to the system resolver)."""
    global _process_doh
    _process_doh = (url, timeout) if url else None
    socket.getaddrinfo = _doh_getaddrinfo if url else _system_getaddrinfo


def _doh_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    config = _process_doh
    name = host.decode() if isinstance(host, bytes) else host
    if (config is None or not name or _is_literal(name) or name.endswith(".local")
            or name == urlsplit(config[0]).hostname):  # the DoH server: system DNS
        return _system_getaddrinfo(host, port, family, type, proto, flags)
    try:
        addresses = resolve_doh_sync(config[0], name, config[1])
    except Exception as e:
        raise socket.gaierror(socket.EAI_NONAME, f"DNS-over-HTTPS lookup of {name} failed: {e}") from None
    if isinstance(port, str):
        port = int(port) if port.isdigit() else socket.getservbyname(port)
    results = []
    for address in addresses:
        af = socket.AF_INET6 if ":" in address else socket.AF_INET
        if family not in (0, socket.AF_UNSPEC, af):
            continue
        sockaddr = (address, port or 0) if af == socket.AF_INET else (address, port or 0, 0, 0)
        results.append((af, type or socket.SOCK_STREAM, proto or socket.IPPROTO_TCP, "", sockaddr))
    if not results:
        raise socket.gaierror(socket.EAI_NONAME, f"DNS-over-HTTPS: no address for {name}")
    return results
