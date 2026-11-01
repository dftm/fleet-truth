import socket
import subprocess
import sys
from pathlib import Path

import pytest

from fleet_truth import cli, version
from tests.conftest import NetBlocked

ROOT = Path(__file__).resolve().parents[1]


def test_unopted_test_cannot_open_a_socket():
    with pytest.raises(NetBlocked):
        socket.create_connection(("127.0.0.1", 9))
    with pytest.raises(NetBlocked):
        socket.getaddrinfo("example.com", 443)
    with pytest.raises(NetBlocked):
        socket.getaddrinfo("localhost", 80)
    s = socket.socket()
    try:
        with pytest.raises(NetBlocked):
            s.connect(("127.0.0.1", 9))
        with pytest.raises(NetBlocked):
            s.connect_ex(("93.184.216.34", 80))
    finally:
        s.close()


@pytest.mark.loopback
def test_loopback_marker_allows_only_loopback():
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    c = socket.socket()
    try:
        c.connect(srv.getsockname())
        with pytest.raises(NetBlocked):
            socket.socket().connect(("93.184.216.34", 80))
        with pytest.raises(NetBlocked):
            socket.getaddrinfo("example.com", 443)
    finally:
        c.close()
        srv.close()


def test_version_file_and_info():
    assert version.read_version() == "0.1.0"
    assert (ROOT / "fleet_truth" / "VERSION").read_text().strip() == "0.1.0"
    i = version.info()
    assert set(i) == {"version", "build", "builtAt", "commit"}
    assert len(i["build"]) == 8


def test_commit_is_full_git_sha_when_set(monkeypatch):
    monkeypatch.setenv("GIT_SHA", "a" * 40)
    assert version.info()["commit"] == "a" * 40
    monkeypatch.delenv("GIT_SHA")
    assert version.info()["commit"] is None


def test_cli_version_and_help(capsys):
    with pytest.raises(SystemExit) as e:
        cli.main(["--version"])
    assert e.value.code == 0
    assert capsys.readouterr().out.strip() == "0.1.0"
    with pytest.raises(SystemExit):
        cli.main(["--help"])
    out = capsys.readouterr().out
    for sub in ("once", "serve", "check-config"):
        assert sub in out


def test_console_module_runs():
    r = subprocess.run([sys.executable, "-m", "fleet_truth.cli", "--version"], cwd=ROOT,
                       capture_output=True, text=True)
    assert r.returncode == 0 and r.stdout.strip() == "0.1.0"
