"""Small network servers for tests: HTTP forward proxy, SOCKS5 proxy, DNS-over-HTTPS (all local)."""

from __future__ import annotations

import base64
import http.client
import select
import socket
import socketserver
import struct
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit


class _Server:
    def _serve(self, server) -> None:
        self.server = server
        threading.Thread(target=server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


class ForwardProxy(_Server):
    """Plain-HTTP forward proxy (absolute-form GET), optionally with Basic proxy login."""

    def __init__(self, login: tuple[str, str] | None = None):
        self.requests: list[str] = []
        proxy = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def do_GET(self):
                if login:
                    expected = "Basic " + base64.b64encode(f"{login[0]}:{login[1]}".encode()).decode()
                    if self.headers.get("Proxy-Authorization") != expected:
                        self.send_response(407)
                        self.send_header("Proxy-Authenticate", 'Basic realm="proxy"')
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return
                proxy.requests.append(self.path)
                target = urlsplit(self.path)
                upstream = http.client.HTTPConnection(target.hostname, target.port or 80, timeout=30)
                headers = {k: v for k, v in self.headers.items() if not k.lower().startswith("proxy-")}
                upstream.request("GET", target.path + (f"?{target.query}" if target.query else ""), headers=headers)
                response = upstream.getresponse()
                body = response.read()
                self.send_response(response.status)
                for key, value in response.getheaders():
                    if key.lower() not in ("content-length", "connection", "transfer-encoding"):
                        self.send_header(key, value)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                upstream.close()

        self._serve(ThreadingHTTPServer(("127.0.0.1", 0), Handler))
        self.url = f"http://127.0.0.1:{self.server.server_port}"


class Socks5Proxy(_Server):
    """SOCKS5 CONNECT without authentication; records the destinations."""

    def __init__(self):
        self.destinations: list[tuple[str, int]] = []
        proxy = self

        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                conn = self.request
                _, n = conn.recv(2)
                conn.recv(n)
                conn.sendall(b"\x05\x00")  # no authentication
                _, cmd, _, atyp = conn.recv(4)
                if atyp == 1:
                    host = socket.inet_ntoa(conn.recv(4))
                elif atyp == 3:
                    host = conn.recv(conn.recv(1)[0]).decode()
                else:
                    host = socket.inet_ntop(socket.AF_INET6, conn.recv(16))
                (port,) = struct.unpack("!H", conn.recv(2))
                proxy.destinations.append((host, port))
                upstream = socket.create_connection((host, port))
                conn.sendall(b"\x05\x00\x00\x01" + socket.inet_aton("0.0.0.0") + struct.pack("!H", 0))
                sockets = [conn, upstream]
                try:
                    while True:
                        readable, _, _ = select.select(sockets, [], [], 30)
                        if not readable:
                            return
                        for sock in readable:
                            data = sock.recv(65536)
                            if not data:
                                return
                            (upstream if sock is conn else conn).sendall(data)
                finally:
                    upstream.close()

        class Server(socketserver.ThreadingTCPServer):
            daemon_threads = True
            allow_reuse_address = True

        self._serve(Server(("127.0.0.1", 0), Handler))
        self.url = f"socks5://127.0.0.1:{self.server.server_address[1]}"


class DohServer(_Server):
    """RFC 8484 POST endpoint at /dns-query answering A records from `names`; counts queries."""

    def __init__(self, names: dict[str, str]):
        import dns.message
        import dns.rdatatype
        import dns.rrset

        self.queries: list[str] = []
        doh = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def do_POST(self):
                query = dns.message.from_wire(self.rfile.read(int(self.headers["Content-Length"])))
                question = query.question[0]
                name = question.name.to_text().rstrip(".")
                doh.queries.append(f"{name}/{dns.rdatatype.to_text(question.rdtype)}")
                answer = dns.message.make_response(query)
                if question.rdtype == dns.rdatatype.A and name in names:
                    answer.answer.append(dns.rrset.from_text(question.name, 60, "IN", "A", names[name]))
                body = answer.to_wire()
                self.send_response(200)
                self.send_header("Content-Type", "application/dns-message")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self._serve(ThreadingHTTPServer(("127.0.0.1", 0), Handler))
        self.url = f"http://127.0.0.1:{self.server.server_port}/dns-query"
