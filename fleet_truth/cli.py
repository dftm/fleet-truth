"""Command line entry point: `fleet-truth {once,serve,check-config,test-alert}`."""
from __future__ import annotations

import argparse
import sys

from . import config as cfgmod
from . import runtime, version


def _stub(name: str) -> int:
    print(f"fleet-truth {name}: not implemented")
    return 0


def _check_config(path: str) -> int:
    try:
        cfg = cfgmod.load(path)
    except cfgmod.ConfigError as e:
        for line in e.errors:
            print(f"config error: {line}", file=sys.stderr)
        return 3
    print(cfgmod.render_plan(cfg))
    return 0


def _once(path: str) -> int:
    try:
        cfg = cfgmod.load(path)
    except cfgmod.ConfigError as e:
        for line in e.errors:
            print(f"config error: {line}", file=sys.stderr)
        return 3
    try:
        out = runtime.run_once(cfg, config_path=path)
    except runtime.RunLocked:
        print("another fleet-truth run is in progress (run lock held); exiting without doing anything", file=sys.stderr)
        return 5
    except runtime.LockError as e:
        print(f"lock error: {e}", file=sys.stderr)
        return 4
    except Exception as e:  # noqa: BLE001 - CLI boundary: class name only, never a message that could hold secrets
        print(f"cycle failed: {type(e).__name__}", file=sys.stderr)
        return 4
    print(runtime.format_summary(out.cr))
    for d in out.deliveries:
        if d.undelivered:
            print(f"alert for {d.decision.app}/{d.decision.check} was not delivered to any channel; will retry", file=sys.stderr)
    if out.dead_man and out.dead_man.action in ("withheld", "ping_error"):
        print(f"dead-man: {out.dead_man.action}: {out.dead_man.error}", file=sys.stderr)
    out.store.close()
    return out.cr.exit_code


def _test_alert(path: str) -> int:
    try:
        cfg = cfgmod.load(path)
    except cfgmod.ConfigError as e:
        for line in e.errors:
            print(f"config error: {line}", file=sys.stderr)
        return 3
    if not cfg.alerts:
        print("no alert channels configured", file=sys.stderr)
        return 1
    res = runtime.test_alerts(cfg)
    for name, r in res.items():
        print(f"{name}: ok" if r.ok else f"{name}: failed ({r.error})")
    return 0 if all(r.ok for r in res.values()) else 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="fleet-truth", description="Read-only fleet monitor.")
    p.add_argument("--version", action="version", version=version.read_version())
    sub = p.add_subparsers(dest="cmd", metavar="{once,serve,check-config,test-alert}")
    once = sub.add_parser("once", help="run one check cycle and exit (0 ok/info, 1 warn/unknown, 2 crit, 3 config error)")
    once.add_argument("-c", "--config", default="fleet.yaml", metavar="PATH", help="path to fleet.yaml (default ./fleet.yaml)")
    ta = sub.add_parser("test-alert", help="send one [TEST] message to every configured alert channel")
    ta.add_argument("-c", "--config", default="fleet.yaml", metavar="PATH")
    serve = sub.add_parser("serve", help="serve the status page and run the cycle loop")
    serve.add_argument("-c", "--config", default="fleet.yaml", metavar="PATH")
    cc = sub.add_parser("check-config", help="validate fleet.yaml and print the resolved plan")
    cc.add_argument("path", metavar="PATH", help="path to fleet.yaml")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd is None:
        build_parser().print_help()
        return 0
    if args.cmd == "check-config":
        return _check_config(args.path)
    if args.cmd == "test-alert":
        return _test_alert(args.config)
    if args.cmd == "once":
        return _once(args.config)
    return _stub(args.cmd)


if __name__ == "__main__":
    sys.exit(main())
