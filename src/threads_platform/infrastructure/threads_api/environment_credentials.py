import os
import re
from collections.abc import Mapping

from pydantic import SecretStr

from threads_platform.application.ports.threads import (
    ThreadsCredentialError,
    ThreadsCredentialErrorCode,
    ThreadsCredentialSecretResolver,
)

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
