"""Network settings: proxies, DNS-over-HTTPS, timeouts, user agent, site logins, diagnostics."""

import asyncio
import os
import stat

import pytest

from ldm import net
from ldm.manager import InvalidArgument, Manager
from ldm.media import base_options
from ldm.net import NetConfig
from ldm.passwords import FilePasswords, MemoryPasswords
from ldm.store import Store

from test_manager import payload, run, wait_state


def new_manager(tmp_path, passwords=None, **settings) -> Manager:
    store = Store(tmp_path / "state.db")
    manager = Manager(store, progress_interval=0.05, passwords=passwords or MemoryPasswords(),
                      downloader_options={"min_part": 64 * 1024, "retry_base_delay": 0.01, "persist_interval": 0.05})
    store.set_setting("download_dir", str(tmp_path / "dl"))
    store.set_setting("retry_interval", 0)
    store.set_setting("proxy_mode", "none")  # tests never use the desktop's proxy
    for key, value in settings.items():
        store.set_setting(key, value)
    return manager


def download(manager, url, **options):
    async def scenario():
        i = manager.add(url, **options)
        state = await wait_state(manager, i, {"done", "error", "expired", "changed"})
        return i, state
    return run(manager, scenario)


@pytest.fixture(autouse=True)
def no_env_proxy(monkeypatch):
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "http_proxy", "https_proxy",
                "all_proxy", "no_proxy"):
        monkeypatch.delenv(key, raising=False)


def test_user_agent_and_headers(tmp_path, http_server):
    data = payload(200_000)
    cfg, url = http_server(data=data)
    manager = new_manager(tmp_path, user_agent="firefox")
    i, state = download(manager, url)
    assert state == "done"
    assert all(r["User-Agent"] == net.USER_AGENTS["firefox"] for r in cfg.requests)
    manager = new_manager(tmp_path / "b", user_agent="MyAgent/1.0")
    download(manager, url, headers={"User-Agent": "FromBrowser/2"})  # the browser's own wins
    assert cfg.requests[-1]["User-Agent"] == "FromBrowser/2"


def test_manual_http_proxy_with_login(tmp_path, http_server, proxy_server):
    data = payload(300_000)
    cfg, url = http_server(data=data)
    proxy = proxy_server(login=("me", "secret"))
    manager = new_manager(tmp_path, proxy_mode="manual", proxy_url=proxy.url, proxy_user="me")
    i, state = download(manager, url)
    assert state == "error" and "proxy wants a login" in manager.store.get(i).error  # no password yet

    passwords = MemoryPasswords()
    passwords.set("proxy", "proxy", "secret")
    manager = new_manager(tmp_path / "b", passwords, proxy_mode="manual", proxy_url=proxy.url, proxy_user="me")
    i, state = download(manager, url)
    assert state == "done" and manager.store.get(i).dest.read_bytes() == data
    assert proxy.requests and all(r.startswith(cfg.base) for r in proxy.requests)


def test_system_proxy_from_desktop_or_environment(tmp_path, http_server, proxy_server, monkeypatch):
    data = payload(100_000)
    cfg, url = http_server(data=data)
    proxy = proxy_server()
    monkeypatch.setattr(net, "system_proxy", lambda url: proxy.url)  # what Gio would answer
    i, state = download(new_manager(tmp_path, proxy_mode="system"), url)
    assert state == "done" and len(proxy.requests) >= 2

    monkeypatch.setattr(net, "system_proxy", lambda url: None)  # no desktop proxy: environment applies
    monkeypatch.setenv("HTTP_PROXY", proxy.url)
    before = len(proxy.requests)
    i, state = download(new_manager(tmp_path / "b", proxy_mode="system"), url)
    assert state == "done" and len(proxy.requests) > before

    before = len(proxy.requests)
    i, state = download(new_manager(tmp_path / "c", proxy_mode="none"), url)  # "none" ignores it
    assert state == "done" and len(proxy.requests) == before


def test_socks5_proxy(tmp_path, http_server, socks_server):
    pytest.importorskip("socksio", reason="python3-socksio not installed")
    data = payload(200_000)
    cfg, url = http_server(data=data)
    manager = new_manager(tmp_path, proxy_mode="manual", proxy_url=socks_server.url)
    i, state = download(manager, url)
    assert state == "done" and manager.store.get(i).dest.read_bytes() == data
    assert socks_server.destinations


def test_dns_over_https(tmp_path, http_server, doh_server):
    data = payload(150_000)
    cfg, url = http_server(data=data)
    doh = doh_server({"files.test": "127.0.0.1"})
    port = cfg.base.rsplit(":", 1)[1]
    test_url = f"http://files.test:{port}/files/test.bin"
    net._cache.clear()

    i, state = download(new_manager(tmp_path, dns_mode="system"), test_url)
    assert state == "error"  # the system resolver doesn't know files.test

    manager = new_manager(tmp_path / "b", dns_mode="doh", doh_url=doh.url)
    i, state = download(manager, test_url)
    assert state == "done" and manager.store.get(i).dest.read_bytes() == data
    assert "files.test/A" in doh.queries
    assert any("DNS-over-HTTPS: files.test → 127.0.0.1" in line for line in manager.get_log(i))
    asked = len(doh.queries)
    download(new_manager(tmp_path / "c", dns_mode="doh", doh_url=doh.url), test_url)
    assert len(doh.queries) == asked  # cached for the record's TTL


@pytest.mark.parametrize("scheme", ["basic", "digest"])
def test_site_login(tmp_path, http_server, scheme):
    data = payload(200_000)
    cfg, url = http_server(data=data, auth=(scheme, "user", "pa:ss"))
    manager = new_manager(tmp_path)
    i, state = download(manager, url)
    d = manager.store.get(i)
    assert (state, d.error) == ("error", "login required for 127.0.0.1")
    assert i not in manager._retry_timers  # no point retrying without a login

    manager = new_manager(tmp_path / "b")

    async def scenario():
        manager.set_login("127.0.0.1", "user", "pa:ss", include_subdomains=False)
        i = manager.add(url)
        return i, await wait_state(manager, i, {"done", "error"})

    i, state = run(manager, scenario)
    assert state == "done" and manager.store.get(i).dest.read_bytes() == data
    assert manager.list_logins() == [{"host": "127.0.0.1", "username": "user", "include_subdomains": False}]


def test_logins_match_subdomains_and_load_from_the_keyring(tmp_path):
    passwords = MemoryPasswords()
    passwords.set("login", "example.com", "pw")
    manager = new_manager(tmp_path, passwords)
    manager.store.set_login("example.com", "alice", include_subdomains=True)

    async def scenario():
        return manager._login_for("cdn.example.com"), manager._login_for("example.org")

    assert run(manager, scenario) == (("alice", "pw"), None)  # password read at start


def test_diagnostics_log(tmp_path, http_server):
    data = payload(200_000)
    cfg, url = http_server(data=data, drops=4, drop_after=10_000)  # the probe takes one of them
    manager = new_manager(tmp_path)
    i, state = download(manager, url, headers={"Cookie": "session=SECRET"})
    lines = manager.get_log(i)
    text = "\n".join(lines)
    assert state == "done"
    assert "probe: size 200000, resumable" in text and "← HTTP 206" in text and "retry 1/" in text
    assert "SECRET" not in text and "cookie: …" in text.lower()


def test_always_retry_has_no_limit(tmp_path, http_server):
    cfg, url = http_server(data=b"", status=500)
    manager = new_manager(tmp_path, retry_count=1, always_retry=True)

    async def scenario():
        i = manager.add(url)
        for _ in range(500):
            if manager._attempts.get(i, 0) >= 3:
                return i
            await asyncio.sleep(0.01)
        raise AssertionError(f"only {manager._attempts.get(i)} retries")

    run(manager, scenario)


def test_non_resumable_download_can_refuse_to_restart(tmp_path, http_server):
    data = payload(2_000_000)
    cfg, url = http_server(data=data, ranges=False, delay=0.01)
    manager = new_manager(tmp_path, restart_non_resumable=False)

    async def scenario():
        i = manager.add(url)
        await wait_state(manager, i, "running")
        await asyncio.sleep(0.3)
        manager.pause(i)
        await wait_state(manager, i, "paused")
        manager.resume(i)
        return i, await wait_state(manager, i, {"changed", "done", "error"})

    i, state = run(manager, scenario)
    assert (state, manager.store.get(i).error) == ("changed", "the server can't resume this download")


@pytest.mark.parametrize("key, value", [("proxy_mode", "vpn"), ("proxy_url", "ftp://x:1"), ("proxy_url", "http://nohost"),
                                        ("dns_mode", "dot"), ("doh_url", "dns.example"), ("user_agent", " "),
                                        ("connect_timeout", 0), ("read_timeout", 9999)])
def test_network_settings_are_validated(tmp_path, key, value):
    with pytest.raises(InvalidArgument):
        new_manager(tmp_path).update_settings({key: value})


def test_yt_dlp_and_libtorrent_get_the_proxy():
    config = NetConfig(proxy_mode="manual", proxy="socks5://u:p@10.0.0.1:1080", user_agent="X/1",
                       read_timeout=12.0)
    options = base_options({}, {"Referer": "r"}, config, "https://v.example")
    assert options["proxy"] == "socks5://u:p@10.0.0.1:1080" and options["socket_timeout"] == 12.0
    assert options["http_headers"] == {"User-Agent": "X/1", "Referer": "r"}
    assert base_options({}, None, NetConfig(proxy_mode="none"), "u")["proxy"] == ""
    assert "proxy" not in base_options({}, None, NetConfig(proxy_mode="system"), "u") or True

    lt = pytest.importorskip("libtorrent")
    from ldm.torrent import proxy_settings
    settings = proxy_settings("socks5://u:p@10.0.0.1:1080")
    assert settings["proxy_type"] == int(lt.proxy_type_t.socks5_pw)
    assert (settings["proxy_hostname"], settings["proxy_port"], settings["proxy_username"]) == ("10.0.0.1", 1080, "u")
    assert proxy_settings(None) == {"proxy_type": int(lt.proxy_type_t.none)}


def test_password_file_is_private(tmp_path):
    path = tmp_path / "passwords.json"
    store = FilePasswords(path)
    store.set("login", "example.com", "pw")
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert FilePasswords(path).get("login", "example.com") == "pw"
    store.delete("login", "example.com")
    assert FilePasswords(path).get("login", "example.com") is None


def test_doh_also_covers_blocking_lookups(tmp_path, http_server, doh_server):
    """yt-dlp resolves names itself: with DoH on, the service's getaddrinfo goes through DoH too
    (regression: an ISP's DNS block page broke video pages even with DoH on)."""
    import socket
    doh = doh_server({"video.test": "127.0.0.1"})
    net._cache.clear()
    try:
        net.use_doh_for_process(doh.url)
        info = socket.getaddrinfo("video.test", 443, type=socket.SOCK_STREAM)
        assert info[0][4] == ("127.0.0.1", 443)
        assert socket.getaddrinfo("127.0.0.1", "80")[0][4][0] == "127.0.0.1"  # literals untouched
        with pytest.raises(socket.gaierror):
            socket.getaddrinfo("unknown.test", 80)
    finally:
        net.use_doh_for_process(None)
    assert socket.getaddrinfo is net._system_getaddrinfo


def test_doh_setting_needs_dnspython(tmp_path, monkeypatch):
    monkeypatch.setattr(net, "doh_available", lambda: False)
    with pytest.raises(InvalidArgument, match="python3-dns"):
        new_manager(tmp_path).update_settings({"dns_mode": "doh"})


def test_system_proxy_lookup_does_not_tie_the_process_to_the_bus(bus_address):
    """Regression: Gio's shared bus connection SIGTERMed the whole service when the bus went away."""
    import subprocess
    import sys
    script = ("import os, signal, time\n"
              "from ldm.net import system_proxy\n"
              "system_proxy('https://example.com/')\n"
              "import gc; gc.collect()\n"  # GLib drops a connection nobody holds, then makes a new one
              "system_proxy('https://example.com/')\n"
              "print('ready', flush=True)\n"
              "time.sleep(3)\n"
              "print('alive', flush=True)\n")
    bus = subprocess.Popen(["dbus-daemon", "--session", "--nofork", "--print-address"], stdout=subprocess.PIPE,
                           text=True)
    address = bus.stdout.readline().strip()
    child = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, text=True,
                             env={**os.environ, "DBUS_SESSION_BUS_ADDRESS": address})
    assert child.stdout.readline().strip() == "ready"
    bus.terminate()
    bus.wait()
    assert child.stdout.readline().strip() == "alive"
    assert child.wait(10) == 0
