import os
import re
from collections.abc import Mapping
from datetime import datetime
from uuid import UUID

from pydantic import SecretStr

from threads_platform.application.clock import Clock
from threads_platform.application.ports.repositories import UnitOfWork, UnitOfWorkFactory
from threads_platform.application.ports.threads import (
    ThreadsCredentialError,
    ThreadsCredentialErrorCode,
    ThreadsCredentialSecretResolver,
)
from threads_platform.domain.accounts import (
    AccountStatus,
    CredentialStatus,
    OAuthCredentialMetadata,
    ThreadsAccount,
)
from threads_platform.domain.time import normalize_utc

THREADS_TOKEN_ENV_PREFIX = "THREADS_PLATFORM_THREADS_TOKEN_"
_MAX_ENV_NAME_LENGTH = 128
_MAX_TOKEN_LENGTH = 8192
_ENV_SUFFIX = re.compile(r"[A-Z0-9_]+\Z")


def validate_threads_credential_ref(credential_ref: str) -> str:
    """Return the exact permitted environment name, without exposing invalid input."""
    if not credential_ref.startswith("env://"):
        raise ThreadsCredentialError(ThreadsCredentialErrorCode.INVALID) from None
    variable_name = credential_ref.removeprefix("env://")
    suffix = variable_name.removeprefix(THREADS_TOKEN_ENV_PREFIX)
    if (
        len(variable_name) > _MAX_ENV_NAME_LENGTH
        or not variable_name.startswith(THREADS_TOKEN_ENV_PREFIX)
        or not _ENV_SUFFIX.fullmatch(suffix)
    ):
        raise ThreadsCredentialError(ThreadsCredentialErrorCode.INVALID) from None
    return variable_name


def normalize_threads_credential_ref(value: str) -> str:
    """Accept a dedicated env variable name or its exact env:// reference form."""
    if value.startswith("env://"):
        validate_threads_credential_ref(value)
        return value
    if value.startswith(THREADS_TOKEN_ENV_PREFIX):
        reference = f"env://{value}"
        validate_threads_credential_ref(reference)
        return reference
    raise ThreadsCredentialError(ThreadsCredentialErrorCode.INVALID) from None


def _valid_secret_value(value: str) -> bool:
    return bool(
        value
        and len(value) <= _MAX_TOKEN_LENGTH
        and "\r" not in value
        and "\n" not in value
        and value == value.strip()
    )


class EnvironmentThreadsCredentialSecretResolver(ThreadsCredentialSecretResolver):
    def __init__(self, environment: Mapping[str, str] | None = None) -> None:
        self._environment = os.environ if environment is None else environment

    async def resolve(self, credential_ref: str) -> SecretStr:
        variable_name = validate_threads_credential_ref(credential_ref)
        try:
            value = self._environment.get(variable_name)
        except Exception:
            raise ThreadsCredentialError(ThreadsCredentialErrorCode.SECRET_UNAVAILABLE) from None
        if value is None:
            raise ThreadsCredentialError(ThreadsCredentialErrorCode.SECRET_UNAVAILABLE) from None
        if not _valid_secret_value(value):
            raise ThreadsCredentialError(ThreadsCredentialErrorCode.SECRET_UNAVAILABLE) from None
        return SecretStr(value)


class PersistentThreadsAccessTokenProvider:
    def __init__(
        self,
        unit_of_work_factory: UnitOfWorkFactory,
        secret_resolver: ThreadsCredentialSecretResolver,
        clock: Clock,
    ) -> None:
        self._unit_of_work_factory = unit_of_work_factory
        self._secret_resolver = secret_resolver
        self._clock = clock

    async def get_access_token(self, account_id: UUID) -> SecretStr:
        now = normalize_utc(self._clock.now())
        token: SecretStr | None = None
        failure: ThreadsCredentialError | None = None

        async with self._unit_of_work_factory() as unit_of_work:
            account = await unit_of_work.accounts.get_for_update(account_id)
            if account is None:
                failure = ThreadsCredentialError(ThreadsCredentialErrorCode.INVALID)
            else:
                credential = await unit_of_work.oauth_credentials.get_for_update(account_id)
                if credential is None:
                    if account.status is AccountStatus.ACTIVE:
                        account.status = AccountStatus.REAUTHORIZATION_REQUIRED
                        account.updated_at = now
                        await unit_of_work.accounts.update(account)
                        failure = ThreadsCredentialError(ThreadsCredentialErrorCode.NOT_CONFIGURED)
                    else:
                        failure = ThreadsCredentialError(
                            ThreadsCredentialErrorCode.REAUTHORIZATION_REQUIRED
                        )
                elif credential.status in {
                    CredentialStatus.REAUTHORIZATION_REQUIRED,
                    CredentialStatus.REVOKED,
                }:
                    await self._require_reauthorization(unit_of_work, account, now)
                    failure = ThreadsCredentialError(
                        ThreadsCredentialErrorCode.REAUTHORIZATION_REQUIRED
                    )
                elif credential.status is CredentialStatus.EXPIRED:
                    await self._require_reauthorization(unit_of_work, account, now)
                    failure = ThreadsCredentialError(ThreadsCredentialErrorCode.EXPIRED)
                elif credential.status is not CredentialStatus.ACTIVE:
                    await self._mark_invalid(unit_of_work, account, credential, now)
                    failure = ThreadsCredentialError(ThreadsCredentialErrorCode.INVALID)
                elif credential.expires_at is None:
                    await self._mark_invalid(unit_of_work, account, credential, now)
                    failure = ThreadsCredentialError(ThreadsCredentialErrorCode.INVALID)
                elif credential.expires_at <= now:
                    credential.status = CredentialStatus.EXPIRED
                    credential.updated_at = now
                    await unit_of_work.oauth_credentials.update(credential)
                    await self._require_reauthorization(unit_of_work, account, now)
                    failure = ThreadsCredentialError(ThreadsCredentialErrorCode.EXPIRED)
                elif account.status is not AccountStatus.ACTIVE:
                    failure = ThreadsCredentialError(
                        ThreadsCredentialErrorCode.REAUTHORIZATION_REQUIRED
                    )
                elif credential.token_type.casefold() != "bearer":
                    await self._mark_invalid(unit_of_work, account, credential, now)
                    failure = ThreadsCredentialError(ThreadsCredentialErrorCode.INVALID)
                elif "threads_basic" not in credential.granted_scopes:
                    await self._mark_invalid(unit_of_work, account, credential, now)
                    failure = ThreadsCredentialError(ThreadsCredentialErrorCode.INVALID)
                else:
                    try:
                        reference = validate_threads_credential_ref(credential.credential_ref)
                        token = await self._secret_resolver.resolve(f"env://{reference}")
                    except ThreadsCredentialError as error:
                        if error.code == ThreadsCredentialErrorCode.SECRET_UNAVAILABLE.value:
                            failure = ThreadsCredentialError(
                                ThreadsCredentialErrorCode.SECRET_UNAVAILABLE
                            )
                        else:
                            await self._mark_invalid(unit_of_work, account, credential, now)
                            failure = ThreadsCredentialError(ThreadsCredentialErrorCode.INVALID)
                    except Exception:
                        await self._mark_invalid(unit_of_work, account, credential, now)
                        failure = ThreadsCredentialError(ThreadsCredentialErrorCode.INVALID)
                    if token is not None and not _valid_secret_value(token.get_secret_value()):
                        token = None
                        failure = ThreadsCredentialError(
                            ThreadsCredentialErrorCode.SECRET_UNAVAILABLE
                        )

        if failure is not None:
            raise failure
        if token is None:
            raise ThreadsCredentialError(ThreadsCredentialErrorCode.INVALID)
        return token

    @staticmethod
    async def _require_reauthorization(
        unit_of_work: UnitOfWork, account: ThreadsAccount, now: datetime
    ) -> None:
        if account.status is not AccountStatus.DISABLED:
            if account.status is not AccountStatus.REAUTHORIZATION_REQUIRED:
                account.status = AccountStatus.REAUTHORIZATION_REQUIRED
                account.updated_at = now
                await unit_of_work.accounts.update(account)

    @classmethod
    async def _mark_invalid(
        cls,
        unit_of_work: UnitOfWork,
        account: ThreadsAccount,
        credential: OAuthCredentialMetadata,
        now: datetime,
    ) -> None:
        if account.status is not AccountStatus.DISABLED:
            credential.status = CredentialStatus.REAUTHORIZATION_REQUIRED
            credential.updated_at = now
            await unit_of_work.oauth_credentials.update(credential)
            await cls._require_reauthorization(unit_of_work, account, now)
