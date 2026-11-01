"""Suite-wide isolation: tests never reach a real network.

Every socket connect and every name lookup raises NetBlocked, unless the test carries
`@pytest.mark.loopback`, which allows connecting to 127.0.0.1 / ::1 only (fake servers).
Git subprocesses are separate processes and are not affected; tests keep them on local paths.
"""
from __future__ import annotations

import ipaddress
import socket
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

_PROXY_VARS = ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "NO_PROXY")


class NetBlocked(AssertionError):
    """A test tried to use the network without opting in."""


def _is_loopback(host) -> bool:
    if isinstance(host, bytes):
        host = host.decode("ascii", "replace")
    if host in ("localhost",):
        return True
    try:
        return ipaddress.ip_address(str(host).strip("[]")).is_loopback
    except ValueError:
        return False


@pytest.fixture(autouse=True)
def _isolated_home(monkeypatch, tmp_path_factory):
    """`~` expands inside a per-test temp dir: no test may create files under the real home directory."""
    home = tmp_path_factory.mktemp("home")
    monkeypatch.setenv("HOME", str(home))


@pytest.fixture(autouse=True)
def _no_ambient_proxy(monkeypatch):
    for name in _PROXY_VARS:
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(name.lower(), raising=False)


@pytest.fixture(autouse=True)
def _block_network(monkeypatch, request):
    allow_loopback = request.node.get_closest_marker("loopback") is not None
    real_getaddrinfo = socket.getaddrinfo
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex

    def guard(host):
        if not (allow_loopback and _is_loopback(host)):
            raise NetBlocked(f"network blocked in tests (host={host!r}); mark the test @pytest.mark.loopback "
                             "for 127.0.0.1 fake servers")

    def getaddrinfo(host, *a, **kw):
        guard(host)
        return real_getaddrinfo(host, *a, **kw)

    def create_connection(address, *a, **kw):
        guard(address[0])
        raise NetBlocked("socket.create_connection is not allowed in tests; use socket.socket().connect")

    def connect(self, address):
        if self.family in (socket.AF_INET, socket.AF_INET6):
            guard(address[0])
        return real_connect(self, address)

    def connect_ex(self, address):
        if self.family in (socket.AF_INET, socket.AF_INET6):
            guard(address[0])
        return real_connect_ex(self, address)

    def gethostbyname(host):
        guard(host)
        return socket.gethostbyname_ex(host)[2][0]

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    monkeypatch.setattr(socket, "create_connection", create_connection)
    monkeypatch.setattr(socket, "gethostbyname", gethostbyname)
    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)


@pytest.fixture(autouse=True)
def _git_never_leaves_the_machine(monkeypatch):
    """No git invocation may name a remote other than a local path, file:// or 127.0.0.1 (never github.com)."""
    from fleet_truth import gitmirror

    real = gitmirror.run_git

    def guarded(args, **kw):
        for a in args:
            if isinstance(a, str) and "://" in a and not a.startswith("file://") and "://127.0.0.1" not in a:
                raise AssertionError(f"test tried to run git against a real remote: {a!r}")
        return real(args, **kw)

    monkeypatch.setattr(gitmirror, "run_git", guarded)
