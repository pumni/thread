import asyncio
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from structlog.testing import capture_logs

import threads_platform.app as app_module
import threads_platform.scheduler as scheduler_module
from tests.integration.threads_test_support import FixedClock, command_body, seed_account_and_post
from threads_platform.application.ports.repositories import UnitOfWorkFactory
from threads_platform.application.ports.threads import (
    ReplyPage,
    ThreadsAPI,
    ThreadsCredentialError,
    ThreadsCredentialErrorCode,
)
from threads_platform.application.worker_jobs import WorkerJobService
from threads_platform.config.settings import LogLevel, Settings
from threads_platform.domain.accounts import (
    AccountStatus,
    CredentialStatus,
    OAuthCredentialMetadata,
    ThreadsAccount,
)
from threads_platform.domain.commands import CommandStatus
from threads_platform.infrastructure.persistence.models import (
    CommandRecord,
    OutboxEventRecord,
)
from threads_platform.infrastructure.persistence.uow import SQLAlchemyUnitOfWorkFactory
from threads_platform.infrastructure.threads_api.client import HttpThreadsAPI
from threads_platform.infrastructure.threads_api.composition import (
    ProcessCommandRuntimeComposition,
    compose_process_command_runtime,
)
from threads_platform.infrastructure.threads_api.credentials import (
    EnvironmentThreadsCredentialSecretResolver,
    PersistentThreadsAccessTokenProvider,
)
from threads_platform.tools.credential_admin import (
    bind_credential_metadata,
    revoke_credential_metadata,
)

pytestmark = pytest.mark.integration

_VARIABLE = "THREADS_PLATFORM_THREADS_TOKEN_ACCOUNT_TEST_V1"
_REFERENCE = f"env://{_VARIABLE}"
_SYNTHETIC_TOKEN = "SYNTHETIC_THREADS_TOKEN_SENTINEL_FOR_TESTS_ONLY"


class CountingResolver:
    def __init__(self) -> None:
        self.calls = 0

    async def resolve(self, credential_ref: str) -> SecretStr:
        self.calls += 1
        assert credential_ref == _REFERENCE
        return SecretStr(_SYNTHETIC_TOKEN)


class ConversationAPI:
    def __init__(self) -> None:
        self.tokens: list[str] = []

    async def get_conversation(
        self, token: SecretStr, thread_id: str, after: str | None
    ) -> ReplyPage:
        assert thread_id
        assert after is None
        self.tokens.append(token.get_secret_value())
        return ReplyPage((), None, has_more=False)


async def _add_account(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
    *,
    account_status: AccountStatus = AccountStatus.ACTIVE,
    credential_status: CredentialStatus | None = CredentialStatus.ACTIVE,
    credential_ref: str = _REFERENCE,
    token_type: str = "Bearer",
    granted_scopes: tuple[str, ...] = ("threads_basic",),
    expires_at: datetime | None = None,
) -> UUID:
    account = ThreadsAccount(
        threads_user_id=f"synthetic-user-{uuid4()}",
        username=f"synthetic-{uuid4().hex[:12]}",
        status=account_status,
    )
    async with unit_of_work_factory() as unit_of_work:
        await unit_of_work.accounts.add(account)
        if credential_status is not None:
            await unit_of_work.oauth_credentials.add(
                OAuthCredentialMetadata(
                    account_id=account.id,
                    credential_ref=credential_ref,
                    token_type=token_type,
                    granted_scopes=granted_scopes,
                    expires_at=expires_at
                    if expires_at is not None
                    else datetime.now(UTC) + timedelta(days=10),
                    status=credential_status,
                )
            )
    return account.id


async def _load_account_and_credential(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory, account_id: UUID
) -> tuple[ThreadsAccount, OAuthCredentialMetadata | None]:
    async with unit_of_work_factory() as unit_of_work:
        account = await unit_of_work.accounts.get(account_id)
        credential = await unit_of_work.oauth_credentials.get(account_id)
    assert account is not None
    return account, credential


def _provider(
    unit_of_work_factory: UnitOfWorkFactory,
    clock: FixedClock,
    resolver: object,
) -> PersistentThreadsAccessTokenProvider:
    return PersistentThreadsAccessTokenProvider(
        unit_of_work_factory,
        cast(EnvironmentThreadsCredentialSecretResolver, resolver),
        clock,
    )


async def test_oauth_credential_repository_round_trip_and_unique_account(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
) -> None:
    account_id = await _add_account(unit_of_work_factory, credential_status=None)
    expires_at = datetime(2026, 11, 29, 8, 50, tzinfo=UTC)
    credential = OAuthCredentialMetadata(
        account_id=account_id,
        credential_ref=_REFERENCE,
        token_type="bearer",
        granted_scopes=("threads_basic", "threads_read_replies"),
        expires_at=expires_at,
        status=CredentialStatus.ACTIVE,
    )
    async with unit_of_work_factory() as unit_of_work:
        await unit_of_work.oauth_credentials.add(credential)
    async with unit_of_work_factory() as unit_of_work:
        persisted = await unit_of_work.oauth_credentials.get(account_id)
    assert persisted is not None
    assert persisted.credential_ref == _REFERENCE
    assert persisted.token_type == "bearer"
    assert persisted.granted_scopes == ("threads_basic", "threads_read_replies")
    assert persisted.expires_at == expires_at
    assert persisted.status is CredentialStatus.ACTIVE

    duplicate = OAuthCredentialMetadata(account_id=account_id, credential_ref=_REFERENCE)
    with pytest.raises(IntegrityError):
        async with unit_of_work_factory() as unit_of_work:
            await unit_of_work.oauth_credentials.add(duplicate)


async def test_provider_returns_secretstr_for_valid_future_bearer_credential(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
) -> None:
    clock = FixedClock()
    account_id = await _add_account(
        unit_of_work_factory,
        token_type="bEaReR",
        expires_at=clock.now() + timedelta(days=1),
    )
    resolver = EnvironmentThreadsCredentialSecretResolver({_VARIABLE: _SYNTHETIC_TOKEN})

    token = await _provider(unit_of_work_factory, clock, resolver).get_access_token(account_id)

    assert isinstance(token, SecretStr)
    assert token.get_secret_value() == _SYNTHETIC_TOKEN
    assert _SYNTHETIC_TOKEN not in repr(token)


async def test_missing_metadata_reauthorizes_active_account(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
) -> None:
    account_id = await _add_account(unit_of_work_factory, credential_status=None)
    resolver = CountingResolver()

    with pytest.raises(ThreadsCredentialError) as error:
        await _provider(unit_of_work_factory, FixedClock(), resolver).get_access_token(account_id)

    account, credential = await _load_account_and_credential(unit_of_work_factory, account_id)
    assert error.value.code == ThreadsCredentialErrorCode.NOT_CONFIGURED.value
    assert not error.value.retryable
    assert account.status is AccountStatus.REAUTHORIZATION_REQUIRED
    assert credential is None
    assert resolver.calls == 0


@pytest.mark.parametrize("offset", [timedelta(0), -timedelta(seconds=1)])
async def test_expired_metadata_persists_expired_and_reauthorization_atomically(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
    offset: timedelta,
) -> None:
    clock = FixedClock()
    account_id = await _add_account(
        unit_of_work_factory,
        expires_at=clock.now() + offset,
    )
    resolver = CountingResolver()

    with pytest.raises(ThreadsCredentialError) as error:
        await _provider(unit_of_work_factory, clock, resolver).get_access_token(account_id)

    account, credential = await _load_account_and_credential(unit_of_work_factory, account_id)
    assert error.value.code == ThreadsCredentialErrorCode.EXPIRED.value
    assert not error.value.retryable
    assert account.status is AccountStatus.REAUTHORIZATION_REQUIRED
    assert credential is not None
    assert credential.status is CredentialStatus.EXPIRED
    assert resolver.calls == 0


@pytest.mark.parametrize(
    "credential_status",
    [CredentialStatus.REAUTHORIZATION_REQUIRED, CredentialStatus.REVOKED],
)
async def test_reauthorization_or_revoked_metadata_fails_without_secret_lookup(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
    credential_status: CredentialStatus,
) -> None:
    account_id = await _add_account(unit_of_work_factory, credential_status=credential_status)
    resolver = CountingResolver()

    with pytest.raises(ThreadsCredentialError) as error:
        await _provider(unit_of_work_factory, FixedClock(), resolver).get_access_token(account_id)

    account, _ = await _load_account_and_credential(unit_of_work_factory, account_id)
    assert error.value.code == ThreadsCredentialErrorCode.REAUTHORIZATION_REQUIRED.value
    assert account.status is AccountStatus.REAUTHORIZATION_REQUIRED
    assert resolver.calls == 0


async def test_reauthorization_account_with_active_metadata_skips_secret_lookup(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
) -> None:
    account_id = await _add_account(
        unit_of_work_factory,
        account_status=AccountStatus.REAUTHORIZATION_REQUIRED,
    )
    resolver = CountingResolver()

    with pytest.raises(ThreadsCredentialError) as error:
        await _provider(unit_of_work_factory, FixedClock(), resolver).get_access_token(account_id)

    account, credential = await _load_account_and_credential(unit_of_work_factory, account_id)
    assert error.value.code == ThreadsCredentialErrorCode.REAUTHORIZATION_REQUIRED.value
    assert account.status is AccountStatus.REAUTHORIZATION_REQUIRED
    assert credential is not None
    assert credential.status is CredentialStatus.ACTIVE
    assert resolver.calls == 0


async def test_disabled_account_remains_disabled_and_is_not_resolved(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
) -> None:
    account_id = await _add_account(unit_of_work_factory, account_status=AccountStatus.DISABLED)
    resolver = CountingResolver()

    with pytest.raises(ThreadsCredentialError) as error:
        await _provider(unit_of_work_factory, FixedClock(), resolver).get_access_token(account_id)

    account, credential = await _load_account_and_credential(unit_of_work_factory, account_id)
    assert error.value.code == ThreadsCredentialErrorCode.REAUTHORIZATION_REQUIRED.value
    assert account.status is AccountStatus.DISABLED
    assert credential is not None
    assert credential.status is CredentialStatus.ACTIVE
    assert resolver.calls == 0


async def test_expiry_marks_credential_but_never_reactivates_disabled_account(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
) -> None:
    clock = FixedClock()
    account_id = await _add_account(
        unit_of_work_factory,
        account_status=AccountStatus.DISABLED,
        expires_at=clock.now(),
    )
    with pytest.raises(ThreadsCredentialError):
        await _provider(unit_of_work_factory, clock, CountingResolver()).get_access_token(
            account_id
        )

    account, credential = await _load_account_and_credential(unit_of_work_factory, account_id)
    assert account.status is AccountStatus.DISABLED
    assert credential is not None
    assert credential.status is CredentialStatus.EXPIRED


async def test_missing_secret_is_retryable_without_metadata_mutation(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
) -> None:
    clock = FixedClock()
    account_id = await _add_account(
        unit_of_work_factory, expires_at=clock.now() + timedelta(days=1)
    )
    resolver = EnvironmentThreadsCredentialSecretResolver({})

    with pytest.raises(ThreadsCredentialError) as error:
        await _provider(unit_of_work_factory, clock, resolver).get_access_token(account_id)

    account, credential = await _load_account_and_credential(unit_of_work_factory, account_id)
    assert error.value.code == ThreadsCredentialErrorCode.SECRET_UNAVAILABLE.value
    assert error.value.retryable
    assert account.status is AccountStatus.ACTIVE
    assert credential is not None
    assert credential.status is CredentialStatus.ACTIVE


async def test_malformed_environment_secret_is_retryable_without_metadata_mutation(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
) -> None:
    clock = FixedClock()
    account_id = await _add_account(
        unit_of_work_factory, expires_at=clock.now() + timedelta(days=1)
    )
    resolver = EnvironmentThreadsCredentialSecretResolver({_VARIABLE: "SYNTHETIC_TOKEN\n"})

    with pytest.raises(ThreadsCredentialError) as error:
        await _provider(unit_of_work_factory, clock, resolver).get_access_token(account_id)

    account, credential = await _load_account_and_credential(unit_of_work_factory, account_id)
    assert error.value.code == ThreadsCredentialErrorCode.SECRET_UNAVAILABLE.value
    assert error.value.retryable
    assert account.status is AccountStatus.ACTIVE
    assert credential is not None
    assert credential.status is CredentialStatus.ACTIVE


@pytest.mark.parametrize(
    "metadata",
    [
        {"token_type": "MAC"},
        {"expires_at": None},
        {"granted_scopes": ("threads_read_replies",)},
        {"credential_ref": "env://THREADS_PLATFORM_DATABASE_URL"},
    ],
)
async def test_invalid_metadata_fails_closed_and_requires_reauthorization(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
    metadata: dict[str, object],
) -> None:
    clock = FixedClock()
    values: dict[str, object] = {
        "token_type": "Bearer",
        "granted_scopes": ("threads_basic",),
        "expires_at": clock.now() + timedelta(days=1),
        "credential_ref": _REFERENCE,
    }
    values.update(metadata)
    account = ThreadsAccount(threads_user_id=f"synthetic-{uuid4()}", username="synthetic")
    credential = OAuthCredentialMetadata(account_id=account.id, **values)  # type: ignore[arg-type]
    async with unit_of_work_factory() as unit_of_work:
        await unit_of_work.accounts.add(account)
        await unit_of_work.oauth_credentials.add(credential)
    resolver = CountingResolver()

    with pytest.raises(ThreadsCredentialError) as error:
        await _provider(unit_of_work_factory, clock, resolver).get_access_token(account.id)

    persisted_account, persisted_credential = await _load_account_and_credential(
        unit_of_work_factory, account.id
    )
    assert error.value.code == ThreadsCredentialErrorCode.INVALID.value
    assert not error.value.retryable
    assert persisted_account.status is AccountStatus.REAUTHORIZATION_REQUIRED
    assert persisted_credential is not None
    assert persisted_credential.status is CredentialStatus.REAUTHORIZATION_REQUIRED
    assert resolver.calls == 0


async def test_provider_failures_do_not_disclose_secret_ref_or_account_identifiers(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
) -> None:
    clock = FixedClock()
    account_id = await _add_account(
        unit_of_work_factory,
        expires_at=clock.now() + timedelta(days=1),
    )
    async with unit_of_work_factory() as unit_of_work:
        account = await unit_of_work.accounts.get(account_id)
        assert account is not None
        user_id = account.threads_user_id
        username = account.username

    with pytest.raises(ThreadsCredentialError) as error:
        await _provider(
            unit_of_work_factory,
            clock,
            EnvironmentThreadsCredentialSecretResolver({}),
        ).get_access_token(account_id)

    rendered = f"{error.value!s} {error.value!r}"
    assert _SYNTHETIC_TOKEN not in rendered
    assert _REFERENCE not in rendered
    assert str(account_id) not in rendered
    assert user_id not in rendered
    assert username not in rendered


async def test_bind_rotates_metadata_and_only_reactivates_reauthorization_accounts(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
) -> None:
    clock = FixedClock()
    account_id = await _add_account(
        unit_of_work_factory,
        account_status=AccountStatus.REAUTHORIZATION_REQUIRED,
        credential_ref="env://THREADS_PLATFORM_THREADS_TOKEN_OLD_V1",
        expires_at=clock.now(),
    )
    expiry = clock.now() + timedelta(days=60)

    await bind_credential_metadata(
        unit_of_work_factory,
        account_id=account_id,
        credential_ref="THREADS_PLATFORM_THREADS_TOKEN_NEW_V2",
        expires_at=expiry,
        token_type="Bearer",
        granted_scopes=("threads_basic", "threads_read_replies"),
    )

    account, credential = await _load_account_and_credential(unit_of_work_factory, account_id)
    assert account.status is AccountStatus.ACTIVE
    assert credential is not None
    assert credential.credential_ref == "env://THREADS_PLATFORM_THREADS_TOKEN_NEW_V2"
    assert credential.expires_at == expiry
    assert credential.granted_scopes == ("threads_basic", "threads_read_replies")
    assert credential.status is CredentialStatus.ACTIVE

    disabled_id = await _add_account(unit_of_work_factory, account_status=AccountStatus.DISABLED)
    await bind_credential_metadata(
        unit_of_work_factory,
        account_id=disabled_id,
        credential_ref=_REFERENCE,
        expires_at=expiry,
        token_type="Bearer",
        granted_scopes=("threads_basic",),
    )
    disabled, _ = await _load_account_and_credential(unit_of_work_factory, disabled_id)
    assert disabled.status is AccountStatus.DISABLED


async def test_revoke_updates_credential_and_account_without_changing_disabled(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
) -> None:
    active_id = await _add_account(unit_of_work_factory)
    disabled_id = await _add_account(unit_of_work_factory, account_status=AccountStatus.DISABLED)

    await revoke_credential_metadata(unit_of_work_factory, account_id=active_id)
    await revoke_credential_metadata(unit_of_work_factory, account_id=disabled_id)

    active, active_credential = await _load_account_and_credential(unit_of_work_factory, active_id)
    disabled, disabled_credential = await _load_account_and_credential(
        unit_of_work_factory, disabled_id
    )
    assert active.status is AccountStatus.REAUTHORIZATION_REQUIRED
    assert active_credential is not None
    assert active_credential.status is CredentialStatus.REVOKED
    assert disabled.status is AccountStatus.DISABLED
    assert disabled_credential is not None
    assert disabled_credential.status is CredentialStatus.REVOKED


async def test_environment_mode_process_composition_registers_local_api_handlers(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
) -> None:
    clock = FixedClock()
    worker_jobs = WorkerJobService(unit_of_work_factory, clock=clock)
    disabled = compose_process_command_runtime(
        Settings(), unit_of_work_factory, worker_jobs, clock=clock
    )
    assert not disabled.command_handler_registry
    assert disabled.threads_access_token_provider is None
    assert disabled.threads_api_gateway is None

    enabled = compose_process_command_runtime(
        Settings(
            database_url=SecretStr("postgresql+asyncpg://localhost/threads_test"),
            threads_token_provider_mode="environment",
        ),
        unit_of_work_factory,
        worker_jobs,
        clock=clock,
        secret_environment={_VARIABLE: _SYNTHETIC_TOKEN},
    )
    assert enabled.command_handler_registry
    assert isinstance(enabled.threads_access_token_provider, PersistentThreadsAccessTokenProvider)
    assert enabled.threads_api_gateway is not None
    assert enabled.http_client is not None
    await enabled.http_client.aclose()
    assert enabled.http_client.is_closed


async def test_fastapi_environment_mode_uses_shared_composition_and_closes_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(
        database_url=SecretStr("postgresql+asyncpg://localhost/threads_test"),
        threads_token_provider_mode="environment",
    )
    original_compose = app_module.compose_process_command_runtime
    compositions: list[ProcessCommandRuntimeComposition] = []

    def capture_composition(*args: Any, **kwargs: Any) -> ProcessCommandRuntimeComposition:
        composition = original_compose(*args, **kwargs)
        compositions.append(composition)
        return composition

    monkeypatch.setattr(app_module, "compose_process_command_runtime", capture_composition)
    application = app_module.create_app(settings)

    assert len(compositions) == 1
    composition = compositions[0]
    assert composition.command_handler_registry
    assert isinstance(
        composition.threads_access_token_provider, PersistentThreadsAccessTokenProvider
    )
    assert isinstance(composition.threads_api_gateway, HttpThreadsAPI)
    client = composition.http_client
    assert client is not None
    async with application.router.lifespan_context(application):
        assert not client.is_closed
    assert client.is_closed


async def test_scheduler_environment_mode_uses_shared_provider_and_closes_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(
        database_url=SecretStr("postgresql+asyncpg://localhost/threads_test"),
        threads_token_provider_mode="environment",
    )
    monkeypatch.setattr(scheduler_module, "get_settings", lambda: settings)

    def no_logging(_level: LogLevel) -> None:
        return None

    def no_signal_handlers(
        _loop: asyncio.AbstractEventLoop, _event: asyncio.Event
    ) -> Callable[[], None]:
        return lambda: None

    monkeypatch.setattr(scheduler_module, "configure_logging", no_logging)
    monkeypatch.setattr(
        scheduler_module,
        "install_shutdown_handlers",
        no_signal_handlers,
    )

    async def stop_runner(
        _runner: scheduler_module.SchedulerRunner, _stop_event: asyncio.Event
    ) -> None:
        return None

    monkeypatch.setattr(scheduler_module.SchedulerRunner, "run", stop_runner)
    original_compose = scheduler_module.compose_process_command_runtime
    compositions: list[ProcessCommandRuntimeComposition] = []

    def capture_composition(*args: Any, **kwargs: Any) -> ProcessCommandRuntimeComposition:
        composition = original_compose(*args, **kwargs)
        compositions.append(composition)
        return composition

    monkeypatch.setattr(scheduler_module, "compose_process_command_runtime", capture_composition)
    with capture_logs() as captured_logs:
        await scheduler_module.run_scheduler()

    assert len(compositions) == 1
    composition = compositions[0]
    assert composition.command_handler_registry
    assert isinstance(
        composition.threads_access_token_provider, PersistentThreadsAccessTokenProvider
    )
    assert isinstance(composition.threads_api_gateway, HttpThreadsAPI)
    client = composition.http_client
    assert client is not None and client.is_closed
    error_codes = {entry.get("error_code") for entry in captured_logs}
    assert "THREADS_ACCESS_TOKEN_PROVIDER_UNAVAILABLE" not in error_codes
    assert "CRM_RESULT_SINK_UNAVAILABLE" in error_codes


async def test_shared_environment_composition_runs_local_api_conversation_sync(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
    db_session: AsyncSession,
    caplog: pytest.LogCaptureFixture,
) -> None:
    clock = FixedClock()
    account_id, root = await seed_account_and_post(unit_of_work_factory)
    async with unit_of_work_factory() as unit_of_work:
        await unit_of_work.oauth_credentials.add(
            OAuthCredentialMetadata(
                account_id=account_id,
                credential_ref=_REFERENCE,
                token_type="Bearer",
                granted_scopes=("threads_basic",),
                expires_at=clock.now() + timedelta(days=1),
            )
        )
    api = ConversationAPI()
    composition = compose_process_command_runtime(
        Settings(
            database_url=SecretStr("postgresql+asyncpg://localhost/threads_test"),
            threads_token_provider_mode="environment",
        ),
        unit_of_work_factory,
        WorkerJobService(unit_of_work_factory, clock=clock),
        threads_api_gateway=cast(ThreadsAPI, api),
        secret_environment={_VARIABLE: _SYNTHETIC_TOKEN},
        clock=clock,
    )
    receipt = await composition.command_runtime.receive(
        command_body(
            account_id,
            clock,
            "threads.sync_conversation",
            {"threads_post_id": root.threads_post_id},
        )
    )
    result = await composition.command_runtime.process(receipt.command_id)
    row = await db_session.scalar(
        select(CommandRecord).where(CommandRecord.command_id == receipt.command_id)
    )
    assert row is not None
    deliveries = list(await db_session.scalars(select(OutboxEventRecord)))
    rendered = f"{result!r} {row.result!r} {row.error_code!r} {deliveries!r} {caplog.text}"

    assert result.status is CommandStatus.SUCCEEDED
    assert api.tokens == [_SYNTHETIC_TOKEN]
    assert row.error_code is None
    assert _SYNTHETIC_TOKEN not in rendered


async def test_missing_secret_command_failure_is_retryable_and_not_unexpected(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
    db_session: AsyncSession,
) -> None:
    clock = FixedClock()
    account_id, root = await seed_account_and_post(unit_of_work_factory)
    async with unit_of_work_factory() as unit_of_work:
        await unit_of_work.oauth_credentials.add(
            OAuthCredentialMetadata(
                account_id=account_id,
                credential_ref=_REFERENCE,
                token_type="Bearer",
                granted_scopes=("threads_basic",),
                expires_at=clock.now() + timedelta(days=1),
            )
        )
    composition = compose_process_command_runtime(
        Settings(
            database_url=SecretStr("postgresql+asyncpg://localhost/threads_test"),
            threads_token_provider_mode="environment",
        ),
        unit_of_work_factory,
        WorkerJobService(unit_of_work_factory, clock=clock),
        threads_api_gateway=cast(ThreadsAPI, ConversationAPI()),
        secret_environment={},
        clock=clock,
    )
    receipt = await composition.command_runtime.receive(
        command_body(
            account_id,
            clock,
            "threads.sync_conversation",
            {"threads_post_id": root.threads_post_id},
        )
    )

    result = await composition.command_runtime.process(receipt.command_id)
    account, credential = await _load_account_and_credential(unit_of_work_factory, account_id)
    command = await db_session.scalar(
        select(CommandRecord).where(CommandRecord.command_id == receipt.command_id)
    )

    assert result.status is CommandStatus.FAILED_RETRYABLE
    assert command is not None
    assert command.error_code == ThreadsCredentialErrorCode.SECRET_UNAVAILABLE.value
    assert command.error_code != "UNEXPECTED_HANDLER_ERROR"
    assert account.status is AccountStatus.ACTIVE
    assert credential is not None
    assert credential.status is CredentialStatus.ACTIVE


async def test_expiry_command_is_permanent_and_next_route_sees_reauthorization(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
    db_session: AsyncSession,
) -> None:
    clock = FixedClock()
    account_id, root = await seed_account_and_post(unit_of_work_factory)
    async with unit_of_work_factory() as unit_of_work:
        await unit_of_work.oauth_credentials.add(
            OAuthCredentialMetadata(
                account_id=account_id,
                credential_ref=_REFERENCE,
                expires_at=clock.now(),
            )
        )
    composition = compose_process_command_runtime(
        Settings(
            database_url=SecretStr("postgresql+asyncpg://localhost/threads_test"),
            threads_token_provider_mode="environment",
        ),
        unit_of_work_factory,
        WorkerJobService(unit_of_work_factory, clock=clock),
        threads_api_gateway=cast(ThreadsAPI, ConversationAPI()),
        secret_environment={_VARIABLE: _SYNTHETIC_TOKEN},
        clock=clock,
    )
    first = await composition.command_runtime.receive(
        command_body(
            account_id,
            clock,
            "threads.sync_conversation",
            {"threads_post_id": root.threads_post_id},
        )
    )
    first_result = await composition.command_runtime.process(first.command_id)
    second = await composition.command_runtime.receive(
        command_body(
            account_id,
            clock,
            "threads.sync_conversation",
            {"threads_post_id": root.threads_post_id},
        )
    )
    second_result = await composition.command_runtime.process(second.command_id)
    account, _ = await _load_account_and_credential(unit_of_work_factory, account_id)
    first_command = await db_session.scalar(
        select(CommandRecord).where(CommandRecord.command_id == first.command_id)
    )

    assert first_result.status is CommandStatus.FAILED_FINAL
    assert first_command is not None
    assert first_command.error_code == ThreadsCredentialErrorCode.EXPIRED.value
    assert second_result.status is CommandStatus.WAITING_INTERVENTION
    assert account.status is AccountStatus.REAUTHORIZATION_REQUIRED
