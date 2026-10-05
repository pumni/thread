"""Command-line entry point for standalone local accounts."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from threads_platform.standalone.accounts import (
    LocalAccountStore,
    StandaloneAccountError,
    resolve_standalone_data_root,
)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="threads-local")
    commands = parser.add_subparsers(dest="command", required=True)
    account_parser = commands.add_parser("account")
    account_commands = account_parser.add_subparsers(dest="account_command", required=True)
    add_parser = account_commands.add_parser("add")
    add_parser.add_argument("alias")
    account_commands.add_parser("list")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        store = LocalAccountStore(resolve_standalone_data_root())
        if args.account_command == "add":
            account = store.add(args.alias)
            sys.stdout.write(f"added {account.alias} {account.id}\n")
            return 0
        for account in store.list():
            sys.stdout.write(f"{account.alias} {account.id}\n")
        return 0
    except StandaloneAccountError as error:
        sys.stderr.write(f"ERROR {error.code}\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
