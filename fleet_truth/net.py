"""The pinned fetcher: the only HTTP client in Fleet Truth (design section 9.1).

  [1]  Parse the URL: https (or http only with allow_private), no userinfo, no legacy numeric hosts
       (2130706433, 0x7f000001, 127.1).
  [2]  Resolve once; refuse if ANY answer is blocked. Loopback, link-local (incl. cloud metadata),
       multicast, unspecified, reserved and 0.0.0.0/8 are refused always, also with allow_private;
       allow_private permits only RFC 1918 and 100.64.0.0/10. IPv4-mapped, 6to4, Teredo and NAT64
       forms are unwrapped and judged by the embedded IPv4.
  [3]  Connect the socket to that exact validated IP. TLS uses server_hostname=<host> with full
       verification; Host: <host>. No second lookup happens.
  [4]  Redirects are never followed: a 3xx is a result.
  [5]  Proxy environment variables are never consulted (http.client does not read them).

Size and time are capped: body <= max_bytes, whole exchange <= timeout_s (a watchdog closes the
socket, so slow-drip servers cannot stretch it). Error messages carry the host only, never the
path, query or any header value (Telegram bot tokens live in a URL path).

`_hooks` is a test-only parameter: it can allow loopback targets and substitute the TLS trust
store. Nothing in config.py can reach it (a test proves the config loader has no such key).
"""
from __future__ import annotations

import http.client
import ipaddress
import socket
import ssl
import threading
import time
from dataclasses import dataclass
from typing import Callable
from urllib.parse import urlsplit

from . import version

MAX_BYTES_DEFAULT = 65536
USER_AGENT = f"fleet-truth/{version.read_version()}"

_PRIVATE_OK = tuple(ipaddress.ip_network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10"))
_THIS_NETWORK = ipaddress.ip_network("0.0.0.0/8")
_CGNAT = ipaddress.ip_network("100.64.0.0/10")
_METADATA_V6 = ipaddress.ip_address("fd00:ec2::254")
_NAT64 = ipaddress.ip_network("64:ff9b::/96")
_V4_COMPAT = ipaddress.ip_network("::/96")


class FetchError(Exception):
    """kind: BAD_URL BLOCKED DNS CONNECT TLS TIMEOUT TOO_LARGE PROTOCOL. Message is safe to log."""

    def __init__(self, kind: str, message: str):
        self.kind = kind
        self.message = message
        super().__init__(f"{kind}: {message}")


@dataclass(frozen=True)
class Response:
    status: int
    headers: dict
    body: bytes
    peer_ip: str
    cert_not_after: float | None  # epoch seconds, https only
    elapsed_ms: int


@dataclass(frozen=True)
class TestHooks:
    """Test-only. allow_loopback permits 127.0.0.0/8 and ::1; ssl_context replaces the trust store."""

    allow_loopback: bool = False
    ssl_context: ssl.SSLContext | None = None


# ----------------------------------------------------------------------------- address policy
def _embedded_v4(ip: ipaddress.IPv6Address) -> list[ipaddress.IPv4Address]:
    out: list[ipaddress.IPv4Address] = []
    if ip.ipv4_mapped is not None:
        out.append(ip.ipv4_mapped)
    if ip.sixtofour is not None:
        out.append(ip.sixtofour)
    if ip.teredo is not None:
        server, client = ip.teredo
        out.extend([server, client])
    if ip in _NAT64:
        out.append(ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF))
    if ip in _V4_COMPAT and int(ip) > 1:
        out.append(ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF))
    return out


def blocked_reason(ip: ipaddress.IPv4Address | ipaddress.IPv6Address, allow_private: bool,
                   allow_loopback: bool = False) -> str | None:
    """None when the address may be connected to, else a short reason (no secrets in it)."""
    if isinstance(ip, ipaddress.IPv6Address):
        for v4 in _embedded_v4(ip):
            r = blocked_reason(v4, allow_private, allow_loopback)
            if r:
                return f"{r} (embedded in IPv6 form)"
        if ip == _METADATA_V6:
            return "cloud metadata address"
    if ip.is_loopback:
        return None if allow_loopback else "loopback address"
    if ip.is_link_local:
        return "link-local address (includes cloud metadata)"
    if ip.is_multicast:
        return "multicast address"
    if ip.is_unspecified:
        return "unspecified address"
    if ip.is_reserved:
        return "reserved address"
    if isinstance(ip, ipaddress.IPv4Address) and ip in _THIS_NETWORK:
        return "0.0.0.0/8 address"
    if allow_private and isinstance(ip, ipaddress.IPv4Address) and any(ip in n for n in _PRIVATE_OK):
        return None
    if ip.is_global and not (isinstance(ip, ipaddress.IPv4Address) and ip in _CGNAT):
        return None
    return "private or non-global address (set allow_private: true for RFC 1918 / tailnet targets)"


def _legacy_numeric(host: str) -> bool:
    """True for hosts inet_aton would read as an IPv4 address in a non-dotted-quad spelling."""
    try:
        ipaddress.ip_address(host)
        return False
    except ValueError:
        pass
    try:
        socket.inet_aton(host)
        return True
    except OSError:
        return False


@dataclass(frozen=True)
class Target:
    scheme: str
    host: str
    port: int
    path: str
    literal_ip: ipaddress.IPv4Address | ipaddress.IPv6Address | None


def parse_url(url: str, allow_private: bool) -> Target:
    if not isinstance(url, str) or not url or any(ord(c) <= 32 or ord(c) == 127 for c in url):
        raise FetchError("BAD_URL", "URL is empty or contains whitespace/control characters")
    try:
        s = urlsplit(url)
        port = s.port
    except ValueError:
        raise FetchError("BAD_URL", "URL is not valid") from None
    if s.scheme not in ("http", "https"):
        raise FetchError("BAD_URL", "scheme must be https (or http with allow_private)")
    if s.scheme == "http" and not allow_private:
        raise FetchError("BAD_URL", "plain http is allowed only with allow_private")
    if "@" in s.netloc or s.username is not None or s.password is not None:
        raise FetchError("BAD_URL", "userinfo in URL is not allowed")
    host = (s.hostname or "").lower()
    if not host:
        raise FetchError("BAD_URL", "URL has no host")
    if "%" in host or not host.isascii():
        raise FetchError("BAD_URL", "host must be plain ASCII without zone id")
    literal = None
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        if _legacy_numeric(host):
            raise FetchError("BAD_URL", "numeric host spelling is not allowed; use a dotted quad or a name") from None
    path = s.path or "/"
    if s.query:
        path += "?" + s.query
    return Target(s.scheme, host, port or (443 if s.scheme == "https" else 80), path, literal)


def validate_addresses(target: Target, allow_private: bool, resolver: Callable | None,
                       hooks: TestHooks | None) -> list[str]:
    """Resolve target.host once and return the validated IPs. Raises BLOCKED if any answer is bad."""
    allow_lo = bool(hooks and hooks.allow_loopback)
    if target.literal_ip is not None:
        ips = [target.literal_ip]
    else:
        res = resolver if resolver is not None else socket.getaddrinfo
        try:
            infos = res(target.host, target.port, type=socket.SOCK_STREAM)
        except OSError:
            raise FetchError("DNS", f"cannot resolve {target.host}") from None
        ips = []
        for info in infos:
            try:
                ip = ipaddress.ip_address(str(info[4][0]).split("%")[0])
            except ValueError:
                raise FetchError("BLOCKED", f"{target.host} resolved to an unparseable address") from None
            if ip not in ips:
                ips.append(ip)
        if not ips:
            raise FetchError("DNS", f"no address for {target.host}")
    for ip in ips:
        reason = blocked_reason(ip, allow_private, allow_lo)
        if reason:
            raise FetchError("BLOCKED", f"{target.host} -> {ip}: {reason}")
    return [str(ip) for ip in ips]


# ----------------------------------------------------------------------------- connection
class _Watch:
    """Whole-exchange deadline. A timer shuts the socket down so blocking reads return."""

    def __init__(self, timeout_s: float):
        self.t0 = time.monotonic()
        self.timeout_s = timeout_s
        self.deadline = self.t0 + timeout_s
        self.sock: socket.socket | None = None
        self.timed_out = False
        self._timer = threading.Timer(timeout_s, self._fire)
        self._timer.daemon = True
        self._timer.start()

    def remaining(self) -> float:
        return max(0.05, self.deadline - time.monotonic())

    def _fire(self) -> None:
        self.timed_out = True
        s = self.sock
        if s is not None:
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def stop(self) -> None:
        self._timer.cancel()


def _pinned_connect(target: Target, ips: list[str], ctx: ssl.SSLContext | None, watch: _Watch) -> tuple[socket.socket, str]:
    last: Exception | None = None
    for ip in ips:
        fam = socket.AF_INET6 if ":" in ip else socket.AF_INET
        sock = socket.socket(fam, socket.SOCK_STREAM)
        try:
            sock.settimeout(watch.remaining())
            sock.connect((ip, target.port))
        except OSError as e:
            sock.close()
            last = e
            if watch.timed_out or isinstance(e, TimeoutError):
                raise FetchError("TIMEOUT", f"connect to {target.host} timed out") from None
            continue
        watch.sock = sock
        if target.scheme == "https":
            assert ctx is not None
            try:
                sock.settimeout(watch.remaining())
                ssock = ctx.wrap_socket(sock, server_hostname=target.host)
            except ssl.SSLCertVerificationError as e:
                sock.close()
                raise FetchError("TLS", f"certificate verification failed for {target.host}: {e.verify_message}") from None
            except ssl.SSLError as e:
                sock.close()
                raise FetchError("TLS", f"TLS handshake with {target.host} failed ({e.reason or 'ssl error'})") from None
            except OSError:
                sock.close()
                if watch.timed_out:
                    raise FetchError("TIMEOUT", f"TLS handshake with {target.host} timed out") from None
                raise FetchError("CONNECT", f"TLS handshake with {target.host} failed") from None
            watch.sock = ssock
            return ssock, ip
        return sock, ip
    if last is not None:
        raise FetchError("CONNECT", f"cannot connect to {target.host}: {type(last).__name__}")
    raise FetchError("CONNECT", f"no address to connect to for {target.host}")


class _PinnedMixin:
    _target: Target
    _ips: list[str]
    _ctx: ssl.SSLContext | None
    _watch: _Watch
    peer_ip: str = ""
    cert_not_after: float | None = None

    def connect(self):  # type: ignore[override]
        self.sock, self.peer_ip = _pinned_connect(self._target, self._ips, self._ctx, self._watch)
        self.cert_not_after = _not_after(self.sock)


class _PinnedHTTP(_PinnedMixin, http.client.HTTPConnection):
    pass


class _PinnedHTTPS(_PinnedMixin, http.client.HTTPSConnection):
    pass


def _tls_context(hooks: TestHooks | None) -> ssl.SSLContext:
    if hooks and hooks.ssl_context is not None:
        return hooks.ssl_context
    return ssl.create_default_context()


def _not_after(sock) -> float | None:
    if not isinstance(sock, ssl.SSLSocket):
        return None
    try:
        cert = sock.getpeercert()
        return float(ssl.cert_time_to_seconds(cert["notAfter"])) if cert else None
    except (KeyError, ValueError, OSError):
        return None


def fetch(url: str, headers: dict | None = None, allow_private: bool = False, method: str = "GET",
          body: bytes | None = None, timeout_s: float = 10.0, max_bytes: int = MAX_BYTES_DEFAULT,
          resolver: Callable | None = None, *, _hooks: TestHooks | None = None) -> Response:
    """One request through the pinned path. Raises FetchError; never follows redirects."""
    if method not in ("GET", "HEAD", "POST"):
        raise FetchError("BAD_URL", "method not allowed")
    target = parse_url(url, allow_private)
    ips = validate_addresses(target, allow_private, resolver, _hooks)
    hdrs = {"User-Agent": USER_AGENT, "Accept": "*/*", "Connection": "close"}
    for k, v in (headers or {}).items():
        hdrs[str(k)] = str(v)
    watch = _Watch(timeout_s)
    conn = None
    try:
        cls = _PinnedHTTPS if target.scheme == "https" else _PinnedHTTP
        conn = cls(target.host, target.port, timeout=timeout_s)
        conn._target, conn._ips, conn._watch = target, ips, watch
        conn._ctx = _tls_context(_hooks) if target.scheme == "https" else None
        try:
            conn.request(method, target.path, body=body, headers=hdrs)
            resp = conn.getresponse()
            status = resp.status
            rheaders: dict[str, str] = {}
            for k, v in resp.getheaders():
                k = k.lower()
                rheaders[k] = f"{rheaders[k]}, {v}" if k in rheaders else v
            cl = rheaders.get("content-length", "")
            if cl.isdigit() and int(cl) > max_bytes and method != "HEAD":
                raise FetchError("TOO_LARGE", f"response from {target.host} exceeds {max_bytes} bytes")
            chunks: list[bytes] = []
            total = 0
            if method != "HEAD" and status not in (204, 304) and not (100 <= status < 200):
                while True:
                    if time.monotonic() > watch.deadline:
                        raise FetchError("TIMEOUT", f"response from {target.host} too slow")
                    if watch.sock is not None:
                        try:
                            watch.sock.settimeout(watch.remaining())
                        except OSError:  # http.client already closed it (Connection: close, body complete)
                            pass
                    chunk = resp.read1(8192)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > max_bytes:
                        raise FetchError("TOO_LARGE", f"response from {target.host} exceeds {max_bytes} bytes")
                    chunks.append(chunk)
                if watch.timed_out:
                    raise FetchError("TIMEOUT", f"response from {target.host} too slow")
            result = Response(status, rheaders, b"".join(chunks), conn.peer_ip, conn.cert_not_after,
                              int((time.monotonic() - watch.t0) * 1000))
        except FetchError:
            raise
        except (TimeoutError, socket.timeout):
            raise FetchError("TIMEOUT", f"{target.host} timed out") from None
        except (http.client.HTTPException, OSError, ValueError) as e:
            if watch.timed_out:
                raise FetchError("TIMEOUT", f"{target.host} timed out") from None
            if isinstance(e, ValueError):
                raise FetchError("BAD_URL", "invalid request header") from None
            raise FetchError("PROTOCOL" if isinstance(e, http.client.HTTPException) else "CONNECT",
                             f"{target.host}: {type(e).__name__}") from e
        return result
    finally:
        watch.stop()
        if conn is not None:
            try:
                conn.close()
            except OSError:
                pass


def peer_cert(url: str, allow_private: bool = False, timeout_s: float = 10.0,
              resolver: Callable | None = None, *, _hooks: TestHooks | None = None) -> tuple[str, float | None]:
    """TLS handshake only, through the same pinned path. Returns (peer_ip, notAfter epoch)."""
    target = parse_url(url, allow_private)
    if target.scheme != "https":
        raise FetchError("BAD_URL", "TLS check needs an https URL")
    ips = validate_addresses(target, allow_private, resolver, _hooks)
    watch = _Watch(timeout_s)
    sock = None
    try:
        sock, ip = _pinned_connect(target, ips, _tls_context(_hooks), watch)
        return ip, _not_after(sock)
    finally:
        watch.stop()
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
