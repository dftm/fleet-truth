"""Fake servers and certificates for tests. Everything binds 127.0.0.x; nothing leaves the host."""
from __future__ import annotations

import shutil
import ssl
import subprocess
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


@dataclass
class Req:
    method: str
    path: str
    headers: dict
    raw: str
    body: bytes = b""
    sni: str | None = None


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address):  # clients hang up on purpose in some tests
        pass


class FakeServer:
    """handler(req) -> (status, headers, body) | callable(wfile) for streaming misbehaviour."""

    def __init__(self, handler, host="127.0.0.1", tls: tuple[str, str] | None = None):
        self.requests: list[Req] = []
        outer = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _do(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n) if n else b""
                raw = self.requestline + "\r\n" + "".join(f"{k}: {v}\r\n" for k, v in self.headers.items())
                req = Req(self.command, self.path, {k.lower(): v for k, v in self.headers.items()}, raw, body,
                          getattr(self.connection, "_fake_sni", None))
                outer.requests.append(req)
                out = handler(req)
                if callable(out):
                    out(self.wfile)
                    return
                status, headers, payload = out
                self.send_response(status)
                for k, v in headers.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(payload)

            do_GET = do_POST = do_HEAD = _do

        self.httpd = _Server((host, 0), H)
        self.port = self.httpd.server_address[1]
        self.host = host
        self.snis: list[str | None] = []
        if tls:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(*tls)

            def sni(sslsock, name, _ctx):
                outer.snis.append(name)

            ctx.sni_callback = sni
            self.httpd.socket = ctx.wrap_socket(self.httpd.socket, server_side=True, do_handshake_on_connect=False)
        self.scheme = "https" if tls else "http"
        self.thread = threading.Thread(target=lambda: self.httpd.serve_forever(poll_interval=0.02), daemon=True)
        self.thread.start()

    def url(self, path="/", host=None):
        return f"{self.scheme}://{host or self.host}:{self.port}{path}"

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def drip_body(n=1000, delay=0.3):
    """Valid headers, then one body byte every `delay` seconds."""
    def stream(w):
        w.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\n\r\n")
        w.flush()
        try:
            for _ in range(n):
                w.write(b"x")
                w.flush()
                time.sleep(delay)
        except OSError:
            pass
    return stream


def drip_headers(delay=0.3):
    """The status line itself arrives one byte at a time."""
    def stream(w):
        try:
            for ch in b"HTTP/1.1 200 OK\r\nX-Pad: " + b"a" * 400:
                w.write(bytes([ch]))
                w.flush()
                time.sleep(delay)
        except OSError:
            pass
    return stream


_cert_cache: dict[tuple, tuple[str, str]] = {}


def make_cert(directory: Path, hostname="fleet.test", days=30) -> tuple[str, str]:
    """Self-signed EC cert with SAN=hostname. Returns (certfile, keyfile). Needs the openssl CLI."""
    key = (str(directory), hostname, days)
    if key in _cert_cache:
        return _cert_cache[key]
    if not shutil.which("openssl"):
        import pytest
        pytest.skip("openssl CLI not available")
    directory.mkdir(parents=True, exist_ok=True)
    cert = directory / f"{hostname}-{days}.crt"
    k = directory / f"{hostname}-{days}.key"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1",
                    "-nodes", "-keyout", str(k), "-out", str(cert), "-days", str(days), "-subj", f"/CN={hostname}",
                    "-addext", f"subjectAltName=DNS:{hostname}"], check=True, capture_output=True)
    _cert_cache[key] = (str(cert), str(k))
    return _cert_cache[key]


def client_ctx(certfile: str) -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ctx.load_verify_locations(certfile)
    return ctx
