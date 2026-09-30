import argparse
import asyncio
import re
import sys
from datetime import UTC, datetime, timedelta
from typing import NoReturn
from uuid import UUID

from threads_platform.application.clock import SystemClock
from threads_platform.application.ports.repositories import UnitOfWorkFactory
from threads_platform.application.ports.threads import (
    ThreadsCredentialError,
    ThreadsCredentialSecretResolver,
)
from threads_platform.config.settings import get_settings
from threads_platform.domain.accounts import (
    AccountStatus,
    CredentialStatus,
    OAuthCredentialMetadata,
)
from threads_platform.domain.time import normalize_utc
from threads_platform.infrastructure.persistence.database import (
    create_database_engine,
    create_session_factory,
)
from threads_platform.infrastructure.persistence.uow import SQLAlchemyUnitOfWorkFactory
from threads_platform.infrastructure.threads_api.credentials import (
    EnvironmentThreadsCredentialSecretResolver,
    normalize_threads_credential_ref,
)

_SCOPE_NAME = re.compile(r"threads_[a-z0-9_]{1,56}\Z")


class CredentialAdminError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class _SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        del message
        print("invalid command arguments", file=sys.stderr)
        self.print_usage(file=sys.stderr)
        raise SystemExit(2)


def validate_credential_metadata(
    credential_ref: str,
    expires_at: datetime,
    token_type: str,
    granted_scopes: tuple[str, ...],
) -> tuple[str, datetime, str, tuple[str, ...]]:
    try:
        normalized_ref = normalize_threads_credential_ref(credential_ref)
    except Exception:
        raise CredentialAdminError("THREADS_CREDENTIAL_INVALID") from None
    if expires_at.tzinfo is None or expires_at.utcoffset() != timedelta(0):
        raise CredentialAdminError("THREADS_CREDENTIAL_INVALID")
    if token_type.casefold() != "bearer":
        raise CredentialAdminError("THREADS_CREDENTIAL_INVALID")
    if "threads_basic" not in granted_scopes or any(
        not _SCOPE_NAME.fullmatch(scope) for scope in granted_scopes
    ):
        raise CredentialAdminError("THREADS_CREDENTIAL_INVALID")
    return normalized_ref, normalize_utc(expires_at), token_type, granted_scopes


async def bind_credential_metadata(
    unit_of_work_factory: UnitOfWorkFactory,
    *,
    account_id: UUID,
    credential_ref: str,
    expires_at: datetime,
    token_type: str,
    granted_scopes: tuple[str, ...],
) -> None:
    reference, expiry, resolved_token_type, scopes = validate_credential_metadata(
        credential_ref, expires_at, token_type, granted_scopes
    )
    now = normalize_utc(SystemClock().now())
    failure: str | None = None
    async with unit_of_work_factory() as unit_of_work:
        account = await unit_of_work.accounts.get_for_update(account_id)
        if account is None:
            failure = "THREADS_ACCOUNT_NOT_FOUND"
        else:
            credential = await unit_of_work.oauth_credentials.get_for_update(account_id)
            if credential is None:
                await unit_of_work.oauth_credentials.add(
                    OAuthCredentialMetadata(
                        account_id=account_id,
                        credential_ref=reference,
                        token_type=resolved_token_type,
                        granted_scopes=scopes,
                        expires_at=expiry,
                        status=CredentialStatus.ACTIVE,
                        updated_at=now,
                    )
                )
            else:
                credential.credential_ref = reference
                credential.token_type = resolved_token_type
                credential.granted_scopes = scopes
                credential.expires_at = expiry
                credential.status = CredentialStatus.ACTIVE
                credential.updated_at = now
                await unit_of_work.oauth_credentials.update(credential)
            if account.status is AccountStatus.REAUTHORIZATION_REQUIRED:
                account.status = AccountStatus.ACTIVE
                account.updated_at = now
                await unit_of_work.accounts.update(account)
    if failure is not None:
        raise CredentialAdminError(failure)


async def revoke_credential_metadata(
    unit_of_work_factory: UnitOfWorkFactory, *, account_id: UUID
) -> None:
    now = normalize_utc(SystemClock().now())
    failure: str | None = None
    async with unit_of_work_factory() as unit_of_work:
        account = await unit_of_work.accounts.get_for_update(account_id)
        if account is None:
            failure = "THREADS_ACCOUNT_NOT_FOUND"
        else:
            credential = await unit_of_work.oauth_credentials.get_for_update(account_id)
            if credential is None:
                failure = "THREADS_CREDENTIAL_NOT_CONFIGURED"
            else:
                credential.status = CredentialStatus.REVOKED
                credential.updated_at = now
                await unit_of_work.oauth_credentials.update(credential)
                if account.status is not AccountStatus.DISABLED:
                    account.status = AccountStatus.REAUTHORIZATION_REQUIRED
                    account.updated_at = now
                    await unit_of_work.accounts.update(account)
    if failure is not None:
        raise CredentialAdminError(failure)


async def verify_credential_reference(
    secret_resolver: ThreadsCredentialSecretResolver, *, credential_ref: str
) -> None:
    try:
        normalized_ref = normalize_threads_credential_ref(credential_ref)
        await secret_resolver.resolve(normalized_ref)
    except ThreadsCredentialError as error:
        raise CredentialAdminError(error.code) from None
    except Exception:
        raise CredentialAdminError("THREADS_CREDENTIAL_INVALID") from None


def _utc_datetime(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise argparse.ArgumentTypeError("invalid UTC expiry") from None
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise argparse.ArgumentTypeError("expiry must be UTC") from None
    return parsed.astimezone(UTC)


def build_parser() -> argparse.ArgumentParser:
    parser = _SafeArgumentParser(description="Manage Threads credential metadata")
    commands = parser.add_subparsers(dest="command", required=True)
    bind = commands.add_parser("bind", aliases=["rotate"], help="bind or rotate metadata")
    bind.add_argument("--account-id", required=True)
    bind.add_argument("--credential-ref", required=True)
    bind.add_argument("--expires-at", required=True, type=_utc_datetime)
    bind.add_argument("--token-type", required=True)
    bind.add_argument("--scope", action="append", required=True)
    revoke = commands.add_parser("revoke", help="revoke credential metadata")
    revoke.add_argument("--account-id", required=True)
    verify = commands.add_parser("verify", help="check a referenced environment secret")
    verify.add_argument("--credential-ref", required=True)
    return parser


async def _run_admin_command(args: argparse.Namespace) -> None:
    if args.command == "verify":
        await verify_credential_reference(
            EnvironmentThreadsCredentialSecretResolver(),
            credential_ref=args.credential_ref,
        )
        return
    try:
        account_id = UUID(args.account_id)
    except AttributeError, ValueError:
        raise CredentialAdminError("THREADS_ACCOUNT_ID_INVALID") from None
    settings = get_settings()
    if settings.database_url is None:
        raise CredentialAdminError("THREADS_PLATFORM_DATABASE_URL_REQUIRED")
    engine = create_database_engine(settings.database_url)
    unit_of_work_factory = SQLAlchemyUnitOfWorkFactory(create_session_factory(engine))
    try:
        if args.command in {"bind", "rotate"}:
            await bind_credential_metadata(
                unit_of_work_factory,
                account_id=account_id,
                credential_ref=args.credential_ref,
                expires_at=args.expires_at,
                token_type=args.token_type,
                granted_scopes=tuple(args.scope),
            )
        else:
            await revoke_credential_metadata(unit_of_work_factory, account_id=account_id)
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        asyncio.run(_run_admin_command(args))
    except CredentialAdminError as error:
        print(error.code, file=sys.stderr)
        return 1
    except Exception:
        print("credential metadata operation failed", file=sys.stderr)
        return 1
    print(
        "credential reference available"
        if args.command == "verify"
        else "credential metadata updated"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
