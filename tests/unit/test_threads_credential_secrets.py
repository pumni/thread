import re
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime

import pytest
from pydantic import SecretStr

from threads_platform.application.ports.threads import (
    ThreadsCredentialError,
    ThreadsCredentialErrorCode,
)
from threads_platform.config.settings import Settings
from threads_platform.infrastructure.threads_api.credentials import (
    EnvironmentThreadsCredentialSecretResolver,
    normalize_threads_credential_ref,
)
from threads_platform.tools.credential_admin import (
    CredentialAdminError,
    build_parser,
    main,
    validate_credential_metadata,
    verify_credential_reference,
)

_VARIABLE = "THREADS_PLATFORM_THREADS_TOKEN_ACCOUNT_TEST_V1"
_REFERENCE = f"env://{_VARIABLE}"
_SYNTHETIC_TOKEN = "SYNTHETIC_THREADS_TOKEN_SENTINEL_FOR_TESTS_ONLY"


class LookupOnlyEnvironment(Mapping[str, str]):
    def __init__(self, values: dict[str, str]) -> None:
        self._entries = values
        self.lookups: list[str] = []

    def __getitem__(self, key: str) -> str:
        self.lookups.append(key)
        return self._entries[key]

    def __iter__(self) -> Iterator[str]:
        raise AssertionError("resolver must not enumerate environment variables")

    def __len__(self) -> int:
        raise AssertionError("resolver must not enumerate environment variables")


async def test_environment_resolver_returns_secret_str_for_exact_threads_reference() -> None:
    environment = LookupOnlyEnvironment({_VARIABLE: _SYNTHETIC_TOKEN})
    resolver = EnvironmentThreadsCredentialSecretResolver(environment)

    secret = await resolver.resolve(_REFERENCE)

    assert isinstance(secret, SecretStr)
    assert secret.get_secret_value() == _SYNTHETIC_TOKEN
    assert _SYNTHETIC_TOKEN not in repr(secret)
    assert _SYNTHETIC_TOKEN not in repr(resolver)
    assert environment.lookups == [_VARIABLE]


@pytest.mark.parametrize(
    "credential_ref",
    [
        "env://THREADS_PLATFORM_DATABASE_URL",
        "env://THREADS_PLATFORM_CRM_INGRESS_TOKEN",
        "env://THREADS_PLATFORM_WORKER_ADMIN_TOKEN",
        "file://THREADS_PLATFORM_THREADS_TOKEN_TEST",
        "http://THREADS_PLATFORM_THREADS_TOKEN_TEST",
        "https://THREADS_PLATFORM_THREADS_TOKEN_TEST",
        "shell://THREADS_PLATFORM_THREADS_TOKEN_TEST",
        "env://THREADS_PLATFORM_THREADS_TOKEN_TEST?name=x",
        "env://THREADS_PLATFORM_THREADS_TOKEN_../DATABASE_URL",
        "env://THREADS_PLATFORM_THREADS_TOKEN_$(DATABASE_URL)",
        f"env://THREADS_PLATFORM_THREADS_TOKEN_{'A' * 100}",
        "THREADS_PLATFORM_THREADS_TOKEN_TEST",
    ],
)
async def test_resolver_rejects_unsafe_refs_before_lookup(credential_ref: str) -> None:
    environment = LookupOnlyEnvironment({})
    resolver = EnvironmentThreadsCredentialSecretResolver(environment)

    with pytest.raises(ThreadsCredentialError) as error:
        await resolver.resolve(credential_ref)

    assert error.value.code == ThreadsCredentialErrorCode.INVALID.value
    assert credential_ref not in str(error.value)
    assert environment.lookups == []


async def test_missing_secret_is_safe_and_retryable() -> None:
    resolver = EnvironmentThreadsCredentialSecretResolver({})

    with pytest.raises(ThreadsCredentialError) as error:
        await resolver.resolve(_REFERENCE)

    assert error.value.code == ThreadsCredentialErrorCode.SECRET_UNAVAILABLE.value
    assert error.value.retryable
    assert _REFERENCE not in repr(error.value)
    assert _SYNTHETIC_TOKEN not in repr(error.value)


@pytest.mark.parametrize(
    "value",
    ["", " SYNTHETIC_VALUE ", "SYNTHETIC_VALUE\r", "SYNTHETIC_VALUE\n", "x" * 8193],
)
async def test_resolver_rejects_invalid_environment_values_without_echo(value: str) -> None:
    resolver = EnvironmentThreadsCredentialSecretResolver({_VARIABLE: value})

    with pytest.raises(ThreadsCredentialError) as error:
        await resolver.resolve(_REFERENCE)

    assert error.value.code == ThreadsCredentialErrorCode.SECRET_UNAVAILABLE.value
    assert error.value.retryable
    assert not value or value not in str(error.value)
    assert _REFERENCE not in repr(error.value)


async def test_resolver_observes_rotation_without_caching_secret_values() -> None:
    environment = {_VARIABLE: "SYNTHETIC_FIRST_TOKEN_SENTINEL"}
    resolver = EnvironmentThreadsCredentialSecretResolver(environment)

    first = await resolver.resolve(_REFERENCE)
    environment[_VARIABLE] = "SYNTHETIC_SECOND_TOKEN_SENTINEL"
    second = await resolver.resolve(_REFERENCE)

    assert first.get_secret_value() == "SYNTHETIC_FIRST_TOKEN_SENTINEL"
    assert second.get_secret_value() == "SYNTHETIC_SECOND_TOKEN_SENTINEL"


def test_admin_ref_normalization_accepts_only_dedicated_namespace() -> None:
    assert normalize_threads_credential_ref(_VARIABLE) == _REFERENCE
    assert normalize_threads_credential_ref(_REFERENCE) == _REFERENCE
    with pytest.raises(ThreadsCredentialError) as error:
        normalize_threads_credential_ref("env://THREADS_PLATFORM_DATABASE_URL")
    assert _VARIABLE not in str(error.value)


def test_metadata_validation_rejects_invalid_ref_without_echo() -> None:
    with pytest.raises(CredentialAdminError) as error:
        validate_credential_metadata(
            "env://THREADS_PLATFORM_DATABASE_URL",
            datetime(2026, 11, 29, tzinfo=UTC),
            "Bearer",
            ("threads_basic",),
        )
    assert error.value.code == "THREADS_CREDENTIAL_INVALID"
    assert "THREADS_PLATFORM_DATABASE_URL" not in repr(error.value)


def test_metadata_validation_requires_basic_scope() -> None:
    with pytest.raises(CredentialAdminError) as error:
        validate_credential_metadata(
            _REFERENCE,
            datetime(2026, 11, 29, tzinfo=UTC),
            "Bearer",
            ("threads_read_replies",),
        )

    assert error.value.code == "THREADS_CREDENTIAL_INVALID"


async def test_admin_verify_does_not_return_or_render_the_secret() -> None:
    resolver = EnvironmentThreadsCredentialSecretResolver({_VARIABLE: _SYNTHETIC_TOKEN})

    outcome = await verify_credential_reference(resolver, credential_ref=_REFERENCE)

    assert outcome is None
    assert _SYNTHETIC_TOKEN not in repr(outcome)


@pytest.mark.parametrize("option", ["--token", "--access-token", "--secret", "--refresh-token"])
def test_admin_cli_rejects_token_value_options_without_echo(
    option: str, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["bind", option, _SYNTHETIC_TOKEN])

    output = capsys.readouterr().err
    assert exit_info.value.code == 2
    assert _SYNTHETIC_TOKEN not in output
    assert re.search(rf"{re.escape(option)}(?:\s|$)", output) is None


def test_admin_parser_uses_metadata_only_inputs(capsys: pytest.CaptureFixture[str]) -> None:
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["bind", "--help"])
    help_output = capsys.readouterr().out
    assert "--credential-ref" in help_output
    assert "--token " not in help_output
    assert "--access-token" not in help_output
    assert "--secret" not in help_output
    assert "--refresh-token" not in help_output
    assert Settings().threads_token_provider_mode == "disabled"
