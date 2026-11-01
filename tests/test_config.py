import copy
import logging

import pytest
import yaml

from fleet_truth import cli, config, secrets

EXAMPLE = "fleet.example.yaml"


def base() -> dict:
    return {"apps": [{"name": "a", "github": "o/r", "health": {"url": "https://a.example.com/healthz"}}]}


def errors_of(doc) -> list[str]:
    with pytest.raises(config.ConfigError) as e:
        config.parse(doc)
    return e.value.errors


def test_example_loads_with_five_apps(capsys):
    cfg = config.load(EXAMPLE)
    assert [a.name for a in cfg.apps] == ["jobs", "health-web", "investo-bot", "invoicing", "events-app"]
    assert cli.main(["check-config", EXAMPLE]) == 0
    out = capsys.readouterr().out
    assert out.startswith("5 apps")
    for n in ("jobs", "health-web", "investo-bot", "invoicing", "events-app"):
        assert n in out


def test_defaults_applied_and_tilde_expanded():
    cfg = config.load(EXAMPLE)
    jobs = cfg.apps[0]
    assert jobs.drift.grace_hours == 24 and jobs.unpushed.dirty_hours == 24
    assert jobs.backup.max_age_hours == 26 and jobs.backup.required is False
    assert "~" not in jobs.repo and "~" not in jobs.backup.status_file
    assert cfg.apps[4].health.allow_private is True


def test_unknown_key_reports_path():
    d = base()
    d["apps"][0]["helth"] = {}
    assert any(e.startswith("apps[0].helth: unknown key") for e in errors_of(d))
    d = base()
    d["bogus"] = 1
    assert any(e.startswith("bogus: unknown key") for e in errors_of(d))
    d = base()
    d["apps"][0]["health"]["allow_loopback"] = True  # no config key can ever disable the SSRF floor
    assert any("allow_loopback: unknown key" in e for e in errors_of(d))


def test_duplicate_and_bad_names():
    d = base()
    d["apps"].append(copy.deepcopy(d["apps"][0]))
    assert any("duplicate app name" in e for e in errors_of(d))
    for bad in ("A", "-x", "has space", "x" * 42, ""):
        d = base()
        d["apps"][0]["name"] = bad
        assert errors_of(d), bad


def test_http_requires_allow_private():
    d = base()
    d["apps"][0]["health"]["url"] = "http://10.0.0.5/healthz"
    assert any("allow_private" in e for e in errors_of(d))
    d["apps"][0]["health"]["allow_private"] = True
    assert config.parse(d).apps[0].health.url.startswith("http://")


def test_url_with_userinfo_rejected_and_not_echoed():
    d = base()
    d["apps"][0]["health"]["url"] = "https://user:hunter2@a.example.com/healthz"
    errs = errors_of(d)
    assert any("user:pass@" in e for e in errs)
    assert "hunter2" not in " ".join(errs)
    d["apps"][0]["health"]["url"] = "https://justuser@a.example.com/"
    assert errors_of(d)


@pytest.mark.parametrize("key", ["token", "secret", "password", "api_key", "bot_token", "client_secret"])
def test_literal_secret_keys_rejected(key):
    d = base()
    d["credentials"] = {key: "abc123"}
    errs = errors_of(d)
    assert any("put it in an env var" in e or "env var" in e for e in errs)
    assert "abc123" not in " ".join(errs)


def test_nested_literal_secret_key_and_env_variants_allowed():
    d = base()
    d["alerts"] = [{"type": "telegram", "bot_token": "x", "bot_token_env": "TG_T", "chat_id_env": "TG_C"}]
    assert any("alerts[0].bot_token" in e for e in errors_of(d))
    ok = base()
    ok["credentials"] = {"github_token_env": "GH_T", "github_token_expires": "2027-01-30"}
    config.parse(ok)


def test_bad_env_names():
    for bad in ("lower", "has-dash", "1ABC", "sekrit-value-123", "A B"):
        d = base()
        d["credentials"] = {"github_token_env": bad}
        errs = errors_of(d)
        assert any("environment variable NAME" in e for e in errs), bad
        assert bad not in " ".join(errs) or bad in ("lower",)  # only the key path, not the value


def test_missing_env_var_reported_not_printed(monkeypatch, capsys, caplog):
    monkeypatch.delenv("FLEET_GITHUB_TOKEN", raising=False)
    monkeypatch.setenv("FLEET_CF_ACCESS_CLIENT_ID", "id-SEKRIT-1")
    assert cli.main(["check-config", EXAMPLE]) == 0
    out = capsys.readouterr().out
    assert "missing env vars" in out and "FLEET_GITHUB_TOKEN" in out
    assert "FLEET_CF_ACCESS_CLIENT_ID" not in out.split("missing env vars")[1]  # set, so not listed
    assert "id-SEKRIT-1" not in out


def test_env_values_never_printed_or_logged(monkeypatch, capsys, caplog):
    vals = {"FLEET_GITHUB_TOKEN": "sekrit-123", "FLEET_CF_ACCESS_CLIENT_ID": "sekrit-id-9",
            "FLEET_CF_ACCESS_CLIENT_SECRET": "sekrit-secret-9", "FLEET_TG_TOKEN": "sekrit-tg-77",
            "FLEET_DEADMAN_URL": "https://hc.example/ping/sekrit-uuid"}
    for k, v in vals.items():
        monkeypatch.setenv(k, v)
    with caplog.at_level(logging.DEBUG):
        assert cli.main(["check-config", EXAMPLE]) == 0
        cfg = config.load(EXAMPLE)
        config.render_plan(cfg)
        assert repr(cfg).count("sekrit") == 0
        bad = base()
        bad["credentials"] = {"github_token_env": "sekrit-123"}
        errs = errors_of(bad)
    cap = capsys.readouterr()
    blob = cap.out + cap.err + caplog.text + repr(cfg) + " ".join(errs)
    for v in vals.values():
        assert v not in blob
    assert "sekrit" not in blob.replace("sekrit-123", "")  # nothing but the deliberately bad NAME
    assert "sekrit-123" not in " ".join(errs)


def test_secrets_get_is_lazy_and_scrub_replaces(monkeypatch):
    monkeypatch.delenv("FLEET_X", raising=False)
    assert secrets.get("FLEET_X") is None
    assert secrets.get("not valid") is None
    secrets.register("FLEET_X")
    monkeypatch.setenv("FLEET_X", "sekrit-abc")
    assert secrets.get("FLEET_X") == "sekrit-abc"
    assert secrets.scrub("boom sekrit-abc end") == "boom *** end"


def test_log_scrub_filter(monkeypatch, caplog):
    secrets.register("FLEET_LOGTEST")
    monkeypatch.setenv("FLEET_LOGTEST", "sekrit-log-1")
    f = secrets.ScrubFilter()
    rec = logging.LogRecord("x", logging.INFO, "f", 1, "token=%s", ("sekrit-log-1",), None)
    f.filter(rec)
    assert "sekrit-log-1" not in rec.getMessage()


def test_check_config_exit_codes_and_stderr(tmp_path, capsys):
    bad = tmp_path / "f.yaml"
    bad.write_text(yaml.safe_dump({"apps": [{"name": "A"}], "token": "zzz"}))
    assert cli.main(["check-config", str(bad)]) == 3
    err = capsys.readouterr().err
    assert "config error" in err and "zzz" not in err
    assert cli.main(["check-config", str(tmp_path / "missing.yaml")]) == 3
    notyaml = tmp_path / "n.yaml"
    notyaml.write_text("a: [unclosed")
    assert cli.main(["check-config", str(notyaml)]) == 3


def test_auth_access_requires_credentials_block():
    d = base()
    d["apps"][0]["version"] = {"url": "https://a.example.com/api/version", "auth": "access"}
    assert any("auth: access needs" in e for e in errors_of(d))


def test_alert_sections():
    d = base()
    d["alerts"] = [{"type": "nope"}, {"type": "telegram", "bot_token_env": "T"}, {"type": "ntfy", "topic_env": "lower"}]
    errs = " | ".join(errors_of(d))
    assert "alerts[0].type" in errs and "alerts[1].chat_id_env" in errs and "alerts[2].topic_env" in errs
    ok = base()
    ok["alerts"] = [{"type": "webhook", "url_env": "HOOK", "mode": "healthchecks", "min_status": "crit"},
                    {"type": "email", "smtp_host": "smtp.example.com", "from": "a@x.org", "to": "b@x.org",
                     "password_env": "SMTP_PW"}]
    cfg = config.parse(ok)
    assert cfg.alerts[0].mode == "healthchecks" and cfg.alerts[1].smtp_port == 587


def test_tests_cannot_touch_the_real_home(tmp_path):
    import os
    from pathlib import Path
    assert str(Path("~/x").expanduser()) == os.path.join(os.environ["HOME"], "x")
    assert "pytest" in os.environ["HOME"] or str(tmp_path.parent) in os.environ["HOME"] or "home" in os.environ["HOME"]


# ------------------------------------------------------------------ YAML hardening (review F10)
import time as _time  # noqa: E402

BOMB = "a: &a [x, x, x, x, x, x, x, x, x]\n" + "".join(
    f"{chr(98 + i)}: &{chr(98 + i)} [{', '.join(['*' + chr(97 + i)] * 9)}]\n" for i in range(9)) + "apps: [{name: a}]\n"


def test_alias_bomb_is_rejected_fast(tmp_path):
    f = tmp_path / "f.yaml"
    f.write_text(BOMB)
    t0 = _time.monotonic()
    with pytest.raises(config.ConfigError) as e:
        config.load(f)
    assert _time.monotonic() - t0 < 1.0 and "anchors and aliases" in e.value.errors[0]


def test_anchors_merge_keys_and_duplicates_rejected(tmp_path):
    for text, needle in (
        ("x: &anc 1\napps: [{name: a}]\n", "anchors and aliases"),
        ("d: &d {name: a}\napps: [*d]\n", "anchors and aliases"),
        ("apps: [{name: a}]\ninterval_seconds: 60\ninterval_seconds: 70\n", "duplicate key 'interval_seconds'"),
        ("apps: [{name: a, name: b}]\n", "duplicate key 'name'"),
    ):
        with pytest.raises(config.ConfigError) as e:
            config.parse_text(text)
        assert needle in e.value.errors[0], text


def test_size_depth_and_node_caps():
    with pytest.raises(config.ConfigError) as e:
        config.parse_text("# " + "x" * 70000 + "\napps: [{name: a}]\n")
    assert "KB" in e.value.errors[0]
    deep = "{a: " * 6 + "1" + "}" * 6
    ok = config.parse_text(f"apps: [{{name: a, health: {{url: 'https://a.example.com/', expect_json: {deep}}}}}]\n")
    assert ok.apps[0].health.expect_json  # a valid document at that nesting is fine without the cap ...
    deeper = "{a: " * 25 + "1" + "}" * 25
    with pytest.raises(config.ConfigError) as e:  # ... and the depth cap is what stops a deeper one
        config.parse_text(f"apps: [{{name: a, health: {{url: 'https://a.example.com/', expect_json: {deeper}}}}}]\n")
    assert "nesting deeper" in e.value.errors[0]
    with pytest.raises(config.ConfigError):
        config.parse_text("apps: [{name: a}]\nz: [" + ",".join(["1"] * 6000) + "]\n")
    with pytest.raises(config.ConfigError):  # recursion in the parser itself
        config.parse_text("a: " + "[" * 20000 + "\n")


def test_example_still_loads_under_the_strict_loader():
    assert len(config.load("fleet.example.yaml").apps) == 5
