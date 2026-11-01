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
    # IPv6 ULA (Tailscale's fd7a:115c:a1e0::/48 among it) is admitted by allow_private only
    assert net.blocked_reason(ipaddress.ip_address("fd7a:115c:a1e0::1"), True) is None
    assert net.blocked_reason(ipaddress.ip_address("fd7a:115c:a1e0::1"), False) is not None


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
    for needle in ("TestHooks", "_hooks"):
        assert needle not in inspect.getsource(config)
    # the owner-facing per-URL flag exists, but only for a literal loopback host
    d = {"apps": [{"name": "a", "health": {"url": "https://a.example.com/", "allow_loopback": True}}]}
    with pytest.raises(config.ConfigError):
        config.parse(d)
    d = {"apps": [{"name": "a", "health": {"url": "https://a.example.com/", "allow_everything": True}}]}
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


# ------------------------------------------------------------------ review F9: policy edges
META = ["169.254.169.254", "169.254.170.2", "100.100.100.200", "168.63.129.16", "fd00:ec2::254",
        "::ffff:169.254.169.254", "::ffff:100.100.100.200", "64:ff9b::a9fe:a9fe", "2002:a9fe:a9fe::"]
SPECIAL = ["fec0::1", "fec0:1234::5", "feff::1", "192.0.0.8", "198.18.0.1", "198.19.255.1", "240.0.0.1", "255.255.255.255",
           "192.88.99.1", "0.1.2.3", "64:ff9b:1::1", "::ffff:198.18.0.1"]


@pytest.mark.parametrize("addr", META)
@pytest.mark.parametrize("allow_private", [False, True])
def test_cloud_metadata_addresses_are_never_allowed(addr, allow_private):
    assert net.blocked_reason(ipaddress.ip_address(addr), allow_private) is not None
    host = f"[{addr}]" if ":" in addr else addr
    with pytest.raises(net.FetchError) as e:
        net.fetch(f"https://{host}/", allow_private=allow_private)
    assert e.value.kind == "BLOCKED"


@pytest.mark.parametrize("addr", SPECIAL)
@pytest.mark.parametrize("allow_private", [False, True])
def test_site_local_and_special_ranges_refused_regardless_of_allow_private(addr, allow_private):
    assert net.blocked_reason(ipaddress.ip_address(addr), allow_private) is not None


def test_name_resolving_to_metadata_refused_even_with_allow_private():
    for ip in ("100.100.100.200", "169.254.169.254", "fd00:ec2::254"):
        with pytest.raises(net.FetchError) as e:
            net.fetch("https://innocent.example.com/", allow_private=True, resolver=res_to("93.184.216.34", ip))
        assert e.value.kind == "BLOCKED"


@pytest.mark.parametrize("host", ["metadata.google.internal", "METADATA.GOOGLE.INTERNAL.", "x.metadata.google.internal",
                                  "metadata", "instance-data", "metadata.goog", "instance-data.ec2.internal"])
def test_metadata_hostnames_refused_before_any_lookup(host):
    calls = []

    def resolver(h, p, type=0):
        calls.append(h)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", p))]

    for ap, scheme in ((False, "https"), (True, "http")):
        with pytest.raises(net.FetchError) as e:
            net.fetch(f"{scheme}://{host}/computeMetadata/v1/", allow_private=ap, resolver=resolver)
        assert e.value.kind == "BLOCKED"
    assert calls == []


@pytest.mark.parametrize("url", ["https://example.com:0/", "https://example.com:65536/", "https://example.com:99999/",
                                 "https://example.com:-1/", "https://example.com:x/"])
def test_bad_ports_rejected(url):
    with pytest.raises(net.FetchError) as e:
        net.fetch(url, resolver=res_to("93.184.216.34"))
    assert e.value.kind == "BAD_URL"


def test_bad_ports_rejected_at_config_load():
    base = {"apps": [{"name": "a", "health": {"url": "https://a.example.com:0/"}}]}
    with pytest.raises(config.ConfigError) as e:
        config.parse(base)
    assert "port" in e.value.errors[0]
    for bad in ("https://a.example.com:70000/", "https://a.example.com:0/x"):
        with pytest.raises(config.ConfigError):
            config.parse({"apps": [{"name": "a", "version": {"url": bad}}]})
    for host in ("a.example.com:0", "a.example.com:99999", "https://a.example.com", "a.example.com/x", "u@a.example.com", ""):
        with pytest.raises(config.ConfigError):
            config.parse({"apps": [{"name": "a", "tls": {"host": host}}]})
    assert config.parse({"apps": [{"name": "a", "tls": {"host": "a.example.com:8443"}}]}).apps[0].tls.host == "a.example.com:8443"
    with pytest.raises(config.ConfigError):
        config.parse({"apps": [{"name": "a"}], "alerts": [{"type": "email", "smtp_host": "h", "smtp_port": 70000,
                                                           "from": "a@b.c", "to": "a@b.c"}]})


def test_dns_resolution_counts_inside_the_time_limit():
    gate = threading.Event()

    def slow(host, port, type=0):
        gate.wait(5)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]

    try:
        t0 = time.monotonic()
        with pytest.raises(net.FetchError) as e:
            net.fetch("https://slow.example.com/", timeout_s=0.5, resolver=slow)
        assert e.value.kind == "TIMEOUT" and "lookup" in e.value.message and time.monotonic() - t0 < 2.0
        t0 = time.monotonic()
        with pytest.raises(net.FetchError) as e:
            net.peer_cert("https://slow.example.com/", timeout_s=0.5, resolver=slow)
        assert e.value.kind == "TIMEOUT" and time.monotonic() - t0 < 2.0
    finally:
        gate.set()


# ------------------------------------------------------------------ review (g): layers proven alone, version independent
@pytest.fixture
def stdlib_says_everything_is_fine(monkeypatch):
    """Neutralise every stdlib ipaddress classification so ONLY Fleet Truth's own rules can refuse an address
    (what Python 3.12 and 3.14 disagree about no longer matters)."""
    for cls in (ipaddress.IPv4Address, ipaddress.IPv6Address):
        for name in ("is_loopback", "is_link_local", "is_multicast", "is_unspecified", "is_reserved", "is_private",
                     "is_site_local"):
            monkeypatch.setattr(cls, name, property(lambda self: False), raising=False)
        monkeypatch.setattr(cls, "is_global", property(lambda self: True))


@pytest.mark.parametrize("addr", ["127.0.0.1", "::1", "169.254.1.1", "fe80::1", "224.0.0.1", "ff02::1", "0.0.0.0", "::",
                                  "::ffff:127.0.0.1", "::ffff:169.254.1.1", "64:ff9b::7f00:1", "2002:7f00:1::", "::127.0.0.1",
                                  "::ffff:224.0.0.1"] + META + SPECIAL)
@pytest.mark.parametrize("allow_private", [False, True])
def test_own_rules_refuse_without_any_help_from_stdlib(stdlib_says_everything_is_fine, addr, allow_private):
    assert net.blocked_reason(ipaddress.ip_address(addr), allow_private) is not None


def test_embedded_ipv4_unwrap_layer_alone(stdlib_says_everything_is_fine):
    """An address the (neutralised) stdlib calls global, whose embedded IPv4 is on Fleet Truth's own list."""
    for addr in ("::ffff:198.18.0.1", "::ffff:100.100.100.200", "64:ff9b::c612:1", "2002:c612:1::", "::198.18.0.1"):
        ip = ipaddress.ip_address(addr)
        assert net.blocked_reason(ip, False).endswith("(embedded in IPv6 form)"), addr
        assert net.blocked_reason(ip, True).endswith("(embedded in IPv6 form)"), addr


def test_public_addresses_still_pass_under_the_neutralised_stdlib(stdlib_says_everything_is_fine):
    assert net.blocked_reason(ipaddress.ip_address("93.184.216.34"), False) is None
    assert net.blocked_reason(ipaddress.ip_address("10.1.2.3"), True) is None


@pytest.mark.loopback
def test_declared_size_layer_alone_fails_fast_without_reading_the_body():
    def lie(w):
        w.write(b"HTTP/1.1 200 OK\r\nContent-Length: 99999999\r\n\r\n")
        w.flush()
        time.sleep(3)  # never sends the body: only the declared-length check can answer quickly

    srv = FakeServer(lambda r: lie)
    try:
        t0 = time.monotonic()
        with pytest.raises(net.FetchError) as e:
            net.fetch(srv.url(), allow_private=True, timeout_s=8, _hooks=LO)
        assert e.value.kind == "TOO_LARGE" and time.monotonic() - t0 < 1.5
    finally:
        srv.close()


# ------------------------------------------------------------------ review 2 R10: per-URL allow_loopback
class LoServer:
    def __enter__(self):
        self.srv = FakeServer(lambda r: (200, {}, b'{"ok": true}'))
        return self.srv

    def __exit__(self, *a):
        self.srv.close()


@pytest.mark.loopback
@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "LOCALHOST"])
def test_allow_loopback_admits_a_literal_loopback_host_with_no_test_hook(host):
    with LoServer() as srv:
        url = f"http://{host}:{srv.port}/healthz"
        r = net.fetch(url, allow_loopback=True, resolver=lambda *a, **k: (_ for _ in ()).throw(AssertionError("no DNS")))
        assert r.status == 200 and r.peer_ip in ("127.0.0.1", "::1")
        # flag absent: every card-3 refusal still holds
        for kw, kind in (({}, "BAD_URL"), ({"allow_private": True}, "BLOCKED")):   # plain http needs a flag at all
            with pytest.raises(net.FetchError) as e:
                net.fetch(url, **kw)
            assert e.value.kind == kind
        assert len(srv.requests) == 1


@pytest.mark.loopback
def test_allow_loopback_does_not_resolve_names_to_loopback():
    with LoServer() as srv:
        calls = []

        def resolver(h, p, type=0):
            calls.append(h)
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", p))]

        for name in ("evil.example.com", "app.local", "localhost.evil.com", "localhost."):
            with pytest.raises(net.FetchError) as e:
                net.fetch(f"http://{name}:{srv.port}/", allow_loopback=True, allow_private=True, resolver=resolver)
            assert e.value.kind in ("BLOCKED", "BAD_URL"), name
        assert not srv.requests
        # mixed answers (public + loopback) are refused too
        with pytest.raises(net.FetchError) as e:
            net.fetch("https://evil.example.com/", allow_loopback=True, resolver=res_to("93.184.216.34", "127.0.0.1"))
        assert e.value.kind == "BLOCKED"


@pytest.mark.parametrize("url", ["http://10.0.0.5/", "http://192.168.1.1/", "http://100.100.40.10/", "http://169.254.169.254/",
                                 "http://100.100.100.200/", "http://0.0.0.0/", "http://[fd00:ec2::254]/", "http://[::ffff:127.0.0.1]/",
                                 "http://[fe80::1]/", "http://224.0.0.1/", "http://metadata.google.internal/", "http://[::]/",
                                 "http://2130706433/", "http://127.1/"])
def test_allow_loopback_admits_no_other_address_class(url):
    with pytest.raises(net.FetchError) as e:
        net.fetch(url, allow_loopback=True, resolver=res_to("93.184.216.34"))
    assert e.value.kind in ("BLOCKED", "BAD_URL")


def test_allow_loopback_does_not_widen_allow_private_and_vice_versa():
    for ip in ("10.1.2.3", "100.100.40.10"):
        t = net.parse_url(f"https://{ip}/", True, True)
        assert net.validate_addresses(t, True, None, None, None, True) == [ip]        # allow_private still does that
        with pytest.raises(net.FetchError):
            net.validate_addresses(net.parse_url(f"https://{ip}/", False, True), False, None, None, None, True)
    with pytest.raises(net.FetchError):                                                # allow_private never admits loopback
        net.validate_addresses(net.parse_url("https://127.0.0.1/", True), True, None, None)


def test_allow_loopback_config_rules():
    ok = {"apps": [{"name": "a", "health": {"url": "http://127.0.0.1:8790/healthz", "allow_loopback": True},
                    "version": {"url": "http://localhost:8790/api/version", "allow_loopback": True}}]}
    cfg = config.parse(ok)
    assert cfg.apps[0].health.allow_loopback and cfg.apps[0].version.allow_loopback
    assert not config.parse({"apps": [{"name": "a", "health": {"url": "https://a.example.com/"}}]}).apps[0].health.allow_loopback
    with pytest.raises(config.ConfigError) as e:   # flag absent: http to loopback is still an error
        config.parse({"apps": [{"name": "a", "health": {"url": "http://127.0.0.1:8790/healthz"}}]})
    assert "allow_loopback" in e.value.errors[0]
    for bad in ("http://app.example.com/", "http://10.0.0.5/", "https://[::ffff:127.0.0.1]/", "http://localhost.evil.com/",
                "http://169.254.169.254/"):
        with pytest.raises(config.ConfigError) as e:
            config.parse({"apps": [{"name": "a", "health": {"url": bad, "allow_loopback": True}}]})
        assert "allow_loopback" in e.value.errors[0], bad
    assert config.parse({"apps": [{"name": "a", "health": {"url": "http://[::1]:80/", "allow_loopback": True}}]})


@pytest.mark.loopback
def test_healthz_and_deep_probe_use_the_flag_end_to_end():
    from fleet_truth.checks import http as H
    with LoServer() as srv:
        cfg = config.parse({"apps": [{"name": "a", "health": {"url": f"http://127.0.0.1:{srv.port}/healthz", "allow_loopback": True,
                                                              "expect_json": {"ok": True}},
                                      "version": {"url": f"http://localhost:{srv.port}/api/version", "allow_loopback": True}}]})
        app = cfg.apps[0]
        assert H.healthz(app, net.fetch).status == "ok"                      # default fetcher, no test hook
        assert H.deep_probe(app, cfg.credentials, net.fetch).status == "ok"
        off = config.parse({"apps": [{"name": "a", "health": {"url": f"http://127.0.0.1:{srv.port}/healthz", "allow_private": True}}]}).apps[0]
        r = H.healthz(off, net.fetch)
        assert (r.status, r.verdict) == ("unknown", "BLOCKED_TARGET")        # without the flag it is refused
