"""Command line entry point: `fleet-truth {once,serve,check-config}`."""
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
        cr, store, _ = runtime.run_once(cfg)
    except Exception as e:  # noqa: BLE001 - CLI boundary: class name only, never a message that could hold secrets
        print(f"cycle failed: {type(e).__name__}", file=sys.stderr)
        return 4
    print(runtime.format_summary(cr))
    store.close()
    return cr.exit_code


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="fleet-truth", description="Read-only fleet monitor.")
    p.add_argument("--version", action="version", version=version.read_version())
    sub = p.add_subparsers(dest="cmd", metavar="{once,serve,check-config}")
    once = sub.add_parser("once", help="run one check cycle and exit (0 ok/info, 1 warn/unknown, 2 crit, 3 config error)")
    once.add_argument("-c", "--config", default="fleet.yaml", metavar="PATH", help="path to fleet.yaml (default ./fleet.yaml)")
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
    if args.cmd == "once":
        return _once(args.config)
    return _stub(args.cmd)


if __name__ == "__main__":
    sys.exit(main())
