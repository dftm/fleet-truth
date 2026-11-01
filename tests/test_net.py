import ipaddress
import socket
import threading
import time

import pytest

from fleet_truth import config, net
from tests.conftest import NetBlocked
from tests.fakes import FakeServer, client_ctx, drip_body, drip_headers, make_cert

LO = net.TestHooks(allow_loopback=True)


def res_to(*ips):
    calls = []

    def resolver(host, port, type=0):
        calls.append(host)
        return [(socket.AF_INET6 if ":" in ip else socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port)) for ip in ips]

    resolver.calls = calls
    return resolver


# ------------------------------------------------------------------ SSRF refusal table
@pytest.mark.parametrize("url", [
    "https://127.0.0.1/", "https://[::1]/", "https://169.254.169.254/latest/meta-data/", "https://0.0.0.0/",
    "https://[::ffff:127.0.0.1]/", "https://2130706433/", "https://0x7f000001/", "https://127.1/",
    "https://0177.0.0.1/", "https://[fd00:ec2::254]/", "https://[2002:7f00:1::]/", "https://[64:ff9b::7f00:1]/",
    "https://[::ffff:169.254.169.254]/", "https://10.0.0.5/", "https://192.168.1.1/", "https://100.100.40.10/",
    "https://0.1.2.3/", "https://255.255.255.255/", "https://[::ffff:7f00:1]/", "https://[::127.0.0.1]/", "https://127.0.0.1@evil.example.com/",
])
@pytest.mark.parametrize("allow_private", [False, True])
def test_literal_hosts_refused_before_any_socket(url, allow_private):
    private_ok = url in ("https://10.0.0.5/", "https://192.168.1.1/", "https://100.100.40.10/")
    if private_ok and allow_private:
        t = net.parse_url(url, True)
        assert net.validate_addresses(t, True, None, None)  # accepted by policy
        return
    # NetBlocked (AssertionError) would mean a connect was attempted: it must be a FetchError first
    with pytest.raises(net.FetchError) as e:
        net.fetch(url, allow_private=allow_private)
    assert e.value.kind in ("BLOCKED", "BAD_URL")


def test_allow_private_accepts_rfc1918_and_tailnet_but_never_loopback_or_linklocal():
    for ip in ("10.1.2.3", "192.168.0.9", "172.16.5.5", "100.100.40.10"):
        assert net.blocked_reason(ipaddress.ip_address(ip), True) is None
        assert net.blocked_reason(ipaddress.ip_address(ip), False) is not None
    for ip in ("127.0.0.1", "127.9.9.9", "::1", "169.254.169.254", "169.254.1.1", "0.0.0.0", "0.5.5.5",
               "fe80::1", "224.0.0.1", "240.0.0.1", "::ffff:127.0.0.1", "::ffff:10.0.0.1".replace("10.0.0.1", "169.254.169.254")):
        assert net.blocked_reason(ipaddress.ip_address(ip), True) is not None, ip
    assert net.blocked_reason(ipaddress.ip_address("93.184.216.34"), False) is None
    assert net.blocked_reason(ipaddress.ip_address("2606:2800:220:1:248:1893:25c8:1946"), False) is None
    # IPv6 ULA is not on the allow_private list
    assert net.blocked_reason(ipaddress.ip_address("fd7a:115c:a1e0::1"), True) is not None


def test_mixed_public_and_private_answers_refused():
    with pytest.raises(net.FetchError) as e:
        net.fetch("https://app.example.com/", resolver=res_to("93.184.216.34", "10.0.0.5"))
    assert e.value.kind == "BLOCKED"
    with pytest.raises(net.FetchError) as e:
        net.fetch("https://app.example.com/", allow_private=True, resolver=res_to("10.0.0.5", "127.0.0.1"))
    assert e.value.kind == "BLOCKED"


def test_name_resolving_to_loopback_refused_even_when_allow_private():
    with pytest.raises(net.FetchError) as e:
        net.fetch("https://app.example.com/", allow_private=True, resolver=res_to("127.0.0.1"))
    assert e.value.kind == "BLOCKED"


def test_url_hygiene():
    for bad in ("https://user:pw@example.com/", "https://u@example.com/", "ftp://example.com/", "https:///x",
                "https://exa mple.com/", "https://example.com/\r\nX: y", "", "http://example.com/"):
        with pytest.raises(net.FetchError) as e:
            net.fetch(bad, resolver=res_to("93.184.216.34"))
        assert e.value.kind == "BAD_URL", bad
    # plain http needs allow_private, then the address policy still applies
    with pytest.raises(net.FetchError):
        net.fetch("http://example.com/", resolver=res_to("93.184.216.34"))


def test_config_cannot_reach_the_loopback_hook():
    import inspect
    assert "_hooks" in inspect.signature(net.fetch).parameters
    for needle in ("allow_loopback", "TestHooks", "_hooks"):
        assert needle not in inspect.getsource(config)
    d = {"apps": [{"name": "a", "health": {"url": "https://a.example.com/", "allow_loopback": True}}]}
    with pytest.raises(config.ConfigError):
        config.parse(d)


# ------------------------------------------------------------------ pinning (TLS)
@pytest.mark.loopback
def test_tls_peer_is_validated_ip_and_sni_host_are_the_hostname(tmp_path):
    cert = make_cert(tmp_path)
    srv = FakeServer(lambda r: (200, {}, b'{"ok": true}'), host="127.0.0.2", tls=cert)
    try:
        calls = []

        def rebinding_resolver(host, port, type=0):
            calls.append(host)
            ip = "127.0.0.2" if len(calls) == 1 else "127.0.0.1"  # a second lookup would land elsewhere
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))]

        r = net.fetch(f"https://fleet.test:{srv.port}/p?q=1", resolver=rebinding_resolver,
                      _hooks=net.TestHooks(True, client_ctx(cert[0])))
        assert r.status == 200 and r.body == b'{"ok": true}'
        assert r.peer_ip == "127.0.0.2"
        assert calls == ["fleet.test"], "exactly one lookup: no second resolution"
        assert srv.snis == ["fleet.test"]
        req = srv.requests[0]
        assert req.headers["host"] == f"fleet.test:{srv.port}"
        assert req.path == "/p?q=1"
        assert r.cert_not_after and r.cert_not_after > time.time() + 20 * 86400
    finally:
        srv.close()


@pytest.mark.loopback
def test_tls_hostname_mismatch_and_untrusted_cert_fail(tmp_path):
    cert = make_cert(tmp_path)
    srv = FakeServer(lambda r: (200, {}, b"{}"), host="127.0.0.1", tls=cert)
    try:
        with pytest.raises(net.FetchError) as e:  # right trust store, wrong name
            net.fetch(f"https://other.test:{srv.port}/", resolver=res_to("127.0.0.1"),
                      _hooks=net.TestHooks(True, client_ctx(cert[0])))
        assert e.value.kind == "TLS"
        with pytest.raises(net.FetchError) as e:  # default trust store does not know the self-signed cert
            net.fetch(f"https://fleet.test:{srv.port}/", resolver=res_to("127.0.0.1"), _hooks=LO)
        assert e.value.kind == "TLS"
    finally:
        srv.close()


# ------------------------------------------------------------------ redirects, size, time, proxy
@pytest.mark.loopback
def test_redirect_is_a_result_and_never_followed():
    hop2 = FakeServer(lambda r: (200, {}, b"hop2"))
    srv = FakeServer(lambda r: (302, {"Location": hop2.url("/next")}, b""))
    try:
        r = net.fetch(srv.url("/start"), allow_private=True, _hooks=LO)
        assert r.status == 302 and r.headers["location"] == hop2.url("/next")
        assert len(srv.requests) == 1 and len(hop2.requests) == 0
    finally:
        srv.close(); hop2.close()


@pytest.mark.loopback
def test_body_over_cap_is_refused_declared_and_undeclared():
    big = b"x" * 70000
    srv = FakeServer(lambda r: (200, {}, big))
    chunked = FakeServer(lambda r: (lambda w: (w.write(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n"),
                                              [w.write(b"2000\r\n" + b"y" * 0x2000 + b"\r\n") for _ in range(12)],
                                              w.write(b"0\r\n\r\n"))))
    try:
        for s in (srv, chunked):
            with pytest.raises(net.FetchError) as e:
                net.fetch(s.url(), allow_private=True, _hooks=LO)
            assert e.value.kind == "TOO_LARGE"
        ok = net.fetch(srv.url(), allow_private=True, max_bytes=80000, _hooks=LO)
        assert len(ok.body) == 70000
    finally:
        srv.close(); chunked.close()


@pytest.mark.loopback
def test_slow_drip_body_and_headers_time_out_within_deadline():
    for stream in (drip_body(), drip_headers()):
        srv = FakeServer(lambda r, s=stream: s)
        try:
            t0 = time.monotonic()
            with pytest.raises(net.FetchError) as e:
                net.fetch(srv.url(), allow_private=True, timeout_s=1.5, _hooks=LO)
            assert e.value.kind == "TIMEOUT"
            assert time.monotonic() - t0 < 3.0
        finally:
            srv.close()


@pytest.mark.loopback
def test_default_timeout_is_under_eleven_seconds():
    srv = FakeServer(lambda r: drip_body())
    try:
        t0 = time.monotonic()
        with pytest.raises(net.FetchError) as e:
            net.fetch(srv.url(), allow_private=True, _hooks=LO)
        assert e.value.kind == "TIMEOUT" and time.monotonic() - t0 < 11.0
    finally:
        srv.close()


@pytest.mark.loopback
def test_proxy_environment_is_ignored(monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:9")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
    srv = FakeServer(lambda r: (200, {}, b"direct"))
    try:
        assert net.fetch(srv.url(), allow_private=True, _hooks=LO).body == b"direct"
        assert len(srv.requests) == 1
    finally:
        srv.close()


@pytest.mark.loopback
def test_loopback_refused_without_the_test_hook():
    srv = FakeServer(lambda r: (200, {}, b"x"))
    try:
        with pytest.raises(net.FetchError) as e:
            net.fetch(srv.url(), allow_private=True)
        assert e.value.kind == "BLOCKED" and not srv.requests
    finally:
        srv.close()


@pytest.mark.loopback
def test_error_messages_carry_host_only(tmp_path):
    srv = FakeServer(lambda r: (200, {}, b"x" * 70000))
    try:
        with pytest.raises(net.FetchError) as e:
            net.fetch(srv.url("/bot123456:SEKRIT-PATH/sendMessage?k=SEKRIT-Q"), headers={"X-Key": "SEKRIT-H"},
                      allow_private=True, _hooks=LO)
        assert "SEKRIT" not in str(e.value)
    finally:
        srv.close()
    with pytest.raises(net.FetchError) as e:
        net.fetch("https://127.0.0.1/bot123:SEKRIT-PATH/x")
    assert "SEKRIT" not in str(e.value)


def test_unopted_fetch_to_public_name_cannot_touch_network():
    # resolver injected, so no lookup; a connect attempt would raise NetBlocked (AssertionError)
    with pytest.raises(NetBlocked):
        net.fetch("https://app.example.com/", resolver=res_to("93.184.216.34"))
