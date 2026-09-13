"""Local helpers: ``python -m mailtransporter.cli sync`` runs one pass."""

from __future__ import annotations

import argparse
import json
import sys

from .runtime import build_forwarder, configure_logging


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mailtransporter")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("sync", help="Run one forwarding pass with the current environment")
    args = parser.parse_args(argv)

    configure_logging()
    if args.command == "sync":
        result = build_forwarder().run()
        print(json.dumps(result.to_dict(), indent=2, default=str))
        return 0 if result.ok else 1
    return 2


if __name__ == "__main__":
    sys.exit(main())
