import functools
import socket
import threading
import time

import pytest

from fleet_truth import config, cycle, net, runtime
from fleet_truth.checks import runner as runner_mod
from fleet_truth.checks.base import CheckResult
from fleet_truth.store import Store
from tests.fakes import FakeServer

LO = net.TestHooks(allow_loopback=True)


def mkcfg(n=5, access=True, tmp=None, **extra):
    apps = []
    for i in range(n):
        a = {"name": f"app{i}", "health": {"url": f"https://app{i}.example.com/healthz"}}
        if access:
            a["version"] = {"url": f"https://app{i}.example.com/api/version", "auth": "access"}
        apps.append(a)
    d = {"apps": apps, "interval_seconds": 300, "confirm": 2}
    if access:
        d["credentials"] = {"access_client_id_env": "FT_ID", "access_client_secret_env": "FT_SECRET"}
    if tmp:
        d["data_dir"] = str(tmp)
    d.update(extra)
    return config.parse(d)


class FakeRunner:
    """script: (app, now) -> list of (check, CheckResult). Per-app overrides via `by_app`."""

    def __init__(self, fn):
        self.fn = fn

    def run_app(self, app, now):
        yield from self.fn(app, now)


OK = CheckResult("ok", "OK")
REJ = CheckResult("crit", "AUTH_REJECTED", "refused")
NETDOWN = CheckResult("crit", "DOWN", "cannot connect", {"net_error": "CONNECT"})
HTTP503 = CheckResult("crit", "DOWN", "HTTP 503")


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "t.db")
    yield s
    s.close()


def cycles(cfg, store, fn, n, t0=1_000_000):
    out = []
    for i in range(n):
        out.append(cycle.run_cycle(cfg, store, FakeRunner(fn), now=t0 + i * 300, deadline_s=5))
    return out


def test_five_apps_auth_rejected_together_give_exactly_one_decision(store):
    cfg = mkcfg()

    def fn(app, now):
        yield "HEALTHZ", OK
        yield "DEEP_PROBE", REJ

    out = cycles(cfg, store, fn, 4)
    assert [len(c.decisions) for c in out] == [0, 1, 0, 0]
    d = out[1].decisions[0]
    assert (d.app, d.check, d.kind, d.level) == ("_monitor", "ACCESS_TOKEN", "alert", "crit")
    assert d.verdict == "TOKEN_REFUSED_EVERYWHERE"
    for i in range(5):
        assert store.get_state(f"app{i}", "DEEP_PROBE").alerted_status is None  # per-app alerts were held back


def test_token_fixed_gives_one_recovery_and_apps_stay_quiet(store):
    cfg = mkcfg()
    state = {"bad": True}

    def fn(app, now):
        yield "HEALTHZ", OK
        yield "DEEP_PROBE", REJ if state["bad"] else OK

    out = cycles(cfg, store, fn, 2)
    state["bad"] = False
    out += cycles(cfg, store, fn, 2, t0=1_000_000 + 600)
    kinds = [(d.app, d.kind) for c in out for d in c.decisions]
    assert kinds == [("_monitor", "alert"), ("_monitor", "recovered")]


def test_partial_auth_rejection_alerts_per_app(store):
    cfg = mkcfg()

    def fn(app, now):
        yield "DEEP_PROBE", OK if app.name == "app0" else REJ

    out = cycles(cfg, store, fn, 2)
    assert sorted(d.app for d in out[1].decisions) == ["app1", "app2", "app3", "app4"]


def test_single_access_app_rejected_is_a_normal_alert(store):
    cfg = mkcfg(n=1)
    out = cycles(cfg, store, lambda app, now: iter([("DEEP_PROBE", REJ)]), 2)
    assert [(d.app, d.check) for d in out[1].decisions] == [("app0", "DEEP_PROBE")]


def test_all_network_checks_failing_gives_one_monitor_offline_alert(store):
    cfg = mkcfg(access=False, n=4)

    def fn(app, now):
        yield "HEALTHZ", NETDOWN
        yield "TLS_EXPIRY", NETDOWN

    out = cycles(cfg, store, fn, 3)
    assert [len(c.decisions) for c in out] == [0, 1, 0]
    d = out[1].decisions[0]
    assert (d.app, d.check, d.verdict) == ("_monitor", "NETWORK", "MONITOR_OFFLINE")


def test_http_503_everywhere_is_not_monitor_offline(store):
    cfg = mkcfg(access=False, n=3)
    out = cycles(cfg, store, lambda app, now: iter([("HEALTHZ", HTTP503)]), 2)
    assert sorted(d.app for d in out[1].decisions) == ["app0", "app1", "app2"]


def test_one_reachable_app_means_the_others_are_really_down(store):
    cfg = mkcfg(access=False, n=3)
    out = cycles(cfg, store, lambda app, now: iter([("HEALTHZ", OK if app.name == "app0" else NETDOWN)]), 2)
    assert sorted(d.app for d in out[1].decisions) == ["app1", "app2"]


def test_check_past_deadline_is_timeout_cycle_and_cycle_returns_on_time(store):
    cfg = mkcfg(access=False, n=2)
    release = threading.Event()

    def fn(app, now):
        if app.name == "app1":
            release.wait(10)  # stuck
        yield "HEALTHZ", OK

    try:
        t0 = time.monotonic()
        cr = cycle.run_cycle(cfg, store, FakeRunner(fn), now=1000, deadline_s=0.4)
        assert time.monotonic() - t0 < 2.0
        assert cr.timed_out_apps == ["app1"]
        assert cr.results[("app1", "HEALTHZ")].verdict == "TIMEOUT_CYCLE"
        assert cr.results[("app1", "HEALTHZ")].status == "unknown"
        assert cr.results[("app0", "HEALTHZ")].status == "ok"
        row = [r for r in store.results_for_run(cr.run_id) if r["app"] == "app1"][0]
        assert row["verdict"] == "TIMEOUT_CYCLE"
    finally:
        release.set()
    time.sleep(0.1)
    late = {r["check_name"]: r["verdict"] for r in store.results_for_run(cr.run_id) if r["app"] == "app1"}
    assert late == {"HEALTHZ": "TIMEOUT_CYCLE", "TLS_EXPIRY": "TIMEOUT_CYCLE"}  # late result discarded


def test_partial_results_kept_when_deadline_hits_mid_app(store):
    cfg = mkcfg(access=False, n=1)
    release = threading.Event()

    def fn(app, now):
        yield "HEALTHZ", OK
        release.wait(10)
        yield "TLS_EXPIRY", OK

    try:
        cr = cycle.run_cycle(cfg, store, FakeRunner(fn), now=1000, deadline_s=0.3)
    finally:
        release.set()
    assert cr.results[("app0", "HEALTHZ")].status == "ok"
    assert cr.results[("app0", "TLS_EXPIRY")].verdict == "TIMEOUT_CYCLE"


def test_runner_exception_becomes_unknown(store):
    cfg = mkcfg(access=False, n=1)

    def fn(app, now):
        raise RuntimeError("boom SECRET")
        yield  # pragma: no cover

    cr = cycle.run_cycle(cfg, store, FakeRunner(fn), now=1000, deadline_s=2)
    r = cr.results[("app0", "RUNNER")]
    assert (r.status, r.verdict) == ("unknown", "ERROR") and "SECRET" not in r.detail


def test_check_exception_becomes_unknown_error(monkeypatch, tmp_path, store):
    cfg = mkcfg(access=False, n=1, tmp=tmp_path)
    monkeypatch.setattr(runner_mod.http_mod, "healthz", lambda *a, **k: (_ for _ in ()).throw(ValueError("hostile")))
    cr = cycle.run_cycle(cfg, store, runner_mod.Runner(cfg), now=1000, deadline_s=2)
    r = cr.results[("app0", "HEALTHZ")]
    assert (r.status, r.verdict, r.detail) == ("unknown", "ERROR", "check raised ValueError")


def test_exit_codes_by_worst_status(store):
    cfg = mkcfg(access=False, n=1)
    for res, code in ((OK, 0), (CheckResult("info", "I"), 0), (CheckResult("warn", "W"), 1),
                      (CheckResult("unknown", "U"), 1), (CheckResult("crit", "C"), 2)):
        cr = cycle.run_cycle(cfg, store, FakeRunner(lambda a, n, r=res: iter([("HEALTHZ", r)])), now=1000, deadline_s=2)
        assert cr.exit_code == code


def test_rollback_alert_state_restores_previous_fields(store):
    cfg = mkcfg(access=False, n=1)
    out = cycles(cfg, store, lambda a, n: iter([("HEALTHZ", CheckResult("crit", "DOWN"))]), 2)
    d = out[1].decisions[0]
    assert store.get_state("app0", "HEALTHZ").alerted_status == "crit"
    cycle.rollback_alert_state(store, d)
    assert store.get_state("app0", "HEALTHZ").alerted_status is None
    nxt = cycle.run_cycle(cfg, store, FakeRunner(lambda a, n: iter([("HEALTHZ", CheckResult("crit", "DOWN"))])),
                          now=1_000_000 + 900, deadline_s=2)
    assert [x.kind for x in nxt.decisions] == ["alert"]  # retried


def test_deleting_the_db_recreates_it(tmp_path):
    cfg = mkcfg(access=False, n=1, tmp=tmp_path / "data")
    f = lambda a, n: iter([("HEALTHZ", OK)])  # noqa: E731
    fac = lambda c, **kw: FakeRunner(f)  # noqa: E731
    cr, store, _ = runtime.run_once(cfg, runner_factory=fac, now=1000)
    store.close()
    db = runtime.db_path(cfg)
    assert db.exists()
    for suffix in ("", "-wal", "-shm"):
        p = db.with_name(db.name + suffix)
        if p.exists():
            p.unlink()
    cr2, store2, _ = runtime.run_once(cfg, runner_factory=fac, now=2000)
    assert db.exists() and cr2.results[("app0", "HEALTHZ")].status == "ok"
    assert store2.get_state("app0", "HEALTHZ").consecutive == 1  # history gone, check works
    store2.close()


# ------------------------------------------------------------------ integration with the real Runner
@pytest.mark.loopback
def test_real_runner_one_cycle_with_local_fixtures(tmp_path, monkeypatch):
    from tests.gitfix import T0, Repos

    monkeypatch.setenv("FT_ID", "id-SEKRIT-1")
    monkeypatch.setenv("FT_SECRET", "SEKRIT-secret-2")
    r = Repos(tmp_path / "g")
    c = r.commit("1.0.0", T0)
    r.push()
    srv = FakeServer(lambda req: (200, {}, b'{"ok": true, "version": "1.0.0", "commit": "%s", "builtAt": "2026-09-01"}' % c.encode()))
    bk = tmp_path / "state"
    bk.mkdir()
    (bk / "status.json").write_text('{"app": "x", "result": "ok", "last_ok_epoch": %d}' % int(time.time()))
    cfg = config.parse({
        "data_dir": str(tmp_path / "data"),
        "apps": [{"name": "x", "github": "o/x", "branch": "main", "repo": str(r.repo), "version_file": "VERSION",
                  "health": {"url": srv.url("/healthz"), "allow_private": True},
                  "version": {"url": srv.url("/api/version"), "allow_private": True},
                  "backup": {"status_file": str(bk / "status.json"), "app_name": "x"}}]})
    spec_factory = lambda app, d: r.spec  # noqa: E731 - file:// origin instead of github
    rn = runner_mod.Runner(cfg, fetch=functools.partial(net.fetch, _hooks=LO), spec_factory=spec_factory)
    store = Store(tmp_path / "t.db")
    try:
        cr = cycle.run_cycle(cfg, store, rn, now=time.time(), deadline_s=30)
    finally:
        srv.close()
    got = {k[1]: (v.status, v.verdict) for k, v in cr.results.items()}
    assert got["HEALTHZ"] == ("ok", "OK") and got["DEEP_PROBE"] == ("ok", "OK")
    assert got["DRIFT"] == ("ok", "IN_SYNC")
    assert got["UNPUSHED"] == ("ok", "OK")
    assert got["BACKUP_AGE"] == ("ok", "OK")
    assert got["STALE_BUILD"][1] == "BUILD_TIME_NOT_SET"
    assert "TLS_EXPIRY" not in got  # http URL
    assert cr.exit_code == 1  # unknown (STALE_BUILD date-only) is never green


@pytest.mark.loopback
def test_drift_is_unknown_when_deep_probe_failed(tmp_path):
    from tests.gitfix import T0, Repos

    r = Repos(tmp_path / "g")
    r.commit("1", T0); r.push()
    srv = FakeServer(lambda req: (403, {}, b""))
    cfg = config.parse({"data_dir": str(tmp_path / "d"), "apps": [{
        "name": "x", "github": "o/x", "repo": str(r.repo), "version_file": "VERSION",
        "version": {"url": srv.url(), "allow_private": True}}]})
    rn = runner_mod.Runner(cfg, fetch=functools.partial(net.fetch, _hooks=LO), spec_factory=lambda a, d: r.spec)
    store = Store(tmp_path / "t.db")
    try:
        cr = cycle.run_cycle(cfg, store, rn, now=time.time(), deadline_s=30)
    finally:
        srv.close()
    assert cr.results[("x", "DEEP_PROBE")].verdict == "AUTH_REJECTED"
    assert (cr.results[("x", "DRIFT")].status, cr.results[("x", "DRIFT")].verdict) == ("unknown", "PROBE_FAILED")
    assert cr.results[("x", "STALE_BUILD")].verdict == "PROBE_FAILED"
    assert cr.results[("x", "UNPUSHED")].status == "ok"


def test_tls_check_runs_every_six_hours_not_every_cycle(tmp_path, store):
    cfg = mkcfg(access=False, n=1, tmp=tmp_path)
    calls = []
    rn = runner_mod.Runner(cfg, fetch=lambda *a, **k: (_ for _ in ()).throw(net.FetchError("CONNECT", "x")),
                           cert_fn=lambda url, **k: calls.append(url) or ("1.2.3.4", time.time() + 90 * 86400),
                           sleep=lambda s: None, meta_get=store.meta_get, meta_set=store.meta_set)
    for i in range(3):
        list(rn.run_app(cfg.apps[0], 1000 + i * 300))
    assert len(calls) == 1
    list(rn.run_app(cfg.apps[0], 1000 + 6 * 3600 + 1))
    assert len(calls) == 2


def test_cli_once_exit_codes_and_config_error(tmp_path, capsys, monkeypatch):
    from fleet_truth import cli

    bad = tmp_path / "bad.yaml"
    bad.write_text("apps: [{name: A}]")
    assert cli.main(["once", "-c", str(bad)]) == 3
    good = tmp_path / "fleet.yaml"
    good.write_text(f"data_dir: {tmp_path / 'd'}\napps:\n  - name: a\n    health: {{url: 'https://a.example.com/healthz'}}\n")
    monkeypatch.setattr(runtime, "Runner", lambda cfg, **kw: FakeRunner(lambda a, n: iter([("HEALTHZ", CheckResult("crit", "DOWN"))])))
    assert cli.main(["once", "-c", str(good)]) == 2
    assert "HEALTHZ" in capsys.readouterr().out
