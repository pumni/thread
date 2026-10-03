from __future__ import annotations

import argparse
import asyncio
import getpass
import sys

from threads_platform.config.settings import get_settings
from threads_platform.infrastructure.persistence.database import (
    create_database_engine,
    create_session_factory,
)
from threads_platform.infrastructure.security.operator_auth import (
    OperatorAuthError,
    OperatorAuthService,
)


def _read_password() -> str:
    if sys.stdin.isatty():
        return getpass.getpass("First Owner password: ")
    line = sys.stdin.readline(1026)
    if not line or len(line) > 1025:
        raise OperatorAuthError("OPERATOR_PASSWORD_INPUT_INVALID")
    return line.rstrip("\r\n")


async def _bootstrap(username: str, password: str) -> None:
    database_url = get_settings().database_url
    if database_url is None:
        raise OperatorAuthError("DATABASE_URL_REQUIRED")
    engine = create_database_engine(database_url)
    try:
        user = await OperatorAuthService(create_session_factory(engine)).bootstrap_first_owner(
            username,
            password,
        )
        print(f"First Owner created: {user.username}")
    finally:
        await engine.dispose()


def bootstrap_owner_from_stdin(username: str) -> int:
    try:
        password = _read_password()
        asyncio.run(_bootstrap(username, password))
    except OperatorAuthError as error:
        print(error.code, file=sys.stderr)
        return 2
    except Exception:
        print("OPERATOR_BOOTSTRAP_FAILED", file=sys.stderr)
        return 2
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Create the first Workspace Owner using local stdin only."
    )
    parser.add_argument("--username", required=True)
    args = parser.parse_args()
    return bootstrap_owner_from_stdin(args.username)


if __name__ == "__main__":
    raise SystemExit(main())
