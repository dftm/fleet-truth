"""Command line entry point: `fleet-truth {once,serve,check-config}`."""
from __future__ import annotations

import argparse
import sys

from . import version


def _stub(name: str) -> int:
    print(f"fleet-truth {name}: not implemented")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="fleet-truth", description="Read-only fleet monitor.")
    p.add_argument("--version", action="version", version=version.read_version())
    sub = p.add_subparsers(dest="cmd", metavar="{once,serve,check-config}")
    sub.add_parser("once", help="run one check cycle and exit (exit code = worst status)")
    sub.add_parser("serve", help="serve the status page and run the cycle loop")
    sub.add_parser("check-config", help="validate fleet.yaml and print the resolved plan")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd is None:
        build_parser().print_help()
        return 0
    return _stub(args.cmd)


if __name__ == "__main__":
    sys.exit(main())
