import asyncio
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from tests.integration.threads_test_support import (
    BlockingHandler,
    FakeThreadsAPI,
    FixedClock,
    SimulatedProcessCrash,
    command_body,
    make_runtime,
    seed_account_and_post,
)
from threads_platform.application.commands.runtime import CommandRuntime
from threads_platform.application.ports.threads import (
    ThreadsAPIError,
    ThreadsTransportError,
)
from threads_platform.domain.commands import CommandStatus
from threads_platform.infrastructure.persistence.models import (
    CommandRecord,
    OutboxEventRecord,
    PostRecord,
)
from threads_platform.infrastructure.persistence.uow import SQLAlchemyUnitOfWorkFactory

pytestmark = pytest.mark.integration


async def test_publish_duplicate_command_is_idempotent_and_outbox_is_atomic(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
    db_session: AsyncSession,
) -> None:
    account_id, _ = await seed_account_and_post(unit_of_work_factory)
    api = FakeThreadsAPI()
    clock = FixedClock()
    runtime = make_runtime(unit_of_work_factory, api, clock)
    body = command_body(account_id, clock)

    first_receipt = await runtime.receive(body)
    duplicate_receipt = await runtime.receive(body)
    first_run = await runtime.process(first_receipt.command_id)
    duplicate_run = await runtime.process(first_receipt.command_id)
    post_count = await db_session.scalar(select(func.count()).select_from(PostRecord))
    event_count = await db_session.scalar(
        select(func.count())
        .select_from(OutboxEventRecord)
        .where(OutboxEventRecord.aggregate_id == first_receipt.command_id)
    )

    assert duplicate_receipt.duplicate is True
    assert first_run.status is CommandStatus.SUCCEEDED
    assert duplicate_run.executed is False
    assert len(api.created) == 1
    assert api.publish_calls == 1
    assert post_count == 2  # seeded root plus the published post
    assert event_count == 1


async def test_concurrent_workers_do_not_publish_same_command_twice(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
) -> None:
    account_id, _ = await seed_account_and_post(unit_of_work_factory)
    api = FakeThreadsAPI()
    entered = asyncio.Event()
    release = asyncio.Event()
    api.publish_entered = entered
    api.publish_release = release
    clock = FixedClock()
    runtime = make_runtime(unit_of_work_factory, api, clock)
    receipt = await runtime.receive(command_body(account_id, clock))
    first_worker = asyncio.create_task(runtime.process(receipt.command_id))
    await entered.wait()
    concurrent_worker = await runtime.process(receipt.command_id)
    release.set()
    first_result = await first_worker

    assert concurrent_worker.status is CommandStatus.PROCESSING
    assert concurrent_worker.executed is False
    assert first_result.status is CommandStatus.SUCCEEDED
    assert api.publish_calls == 1


async def test_crash_after_container_creation_resumes_from_checkpoint(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
) -> None:
    account_id, _ = await seed_account_and_post(unit_of_work_factory)
    api = FakeThreadsAPI()
    api.crash_on_status_once = True
    clock = FixedClock()
    runtime = make_runtime(unit_of_work_factory, api, clock, lease_duration=timedelta(seconds=6))
    receipt = await runtime.receive(command_body(account_id, clock))

    with pytest.raises(SimulatedProcessCrash):
        await runtime.process(receipt.command_id)
    clock.advance(timedelta(seconds=6))
    recovered = await runtime.process(receipt.command_id)

    assert recovered.status is CommandStatus.SUCCEEDED
    assert len(api.created) == 1
    assert api.publish_calls == 1


async def test_crash_after_publish_response_resumes_from_published_media_checkpoint(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
) -> None:
    account_id, _ = await seed_account_and_post(unit_of_work_factory)
    api = FakeThreadsAPI()
    api.crash_on_media_once = True
    clock = FixedClock()
    runtime = make_runtime(unit_of_work_factory, api, clock, lease_duration=timedelta(seconds=6))
    receipt = await runtime.receive(command_body(account_id, clock))

    with pytest.raises(SimulatedProcessCrash):
        await runtime.process(receipt.command_id)
    clock.advance(timedelta(seconds=6))
    recovered = await runtime.process(receipt.command_id)

    assert recovered.status is CommandStatus.SUCCEEDED
    assert len(api.created) == 1
    assert api.publish_calls == 1
    assert api.container_status_calls == 1


async def test_ambiguous_publish_timeout_reconciles_without_republishing(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
    db_session: AsyncSession,
) -> None:
    account_id, _ = await seed_account_and_post(unit_of_work_factory)
    api = FakeThreadsAPI()
    api.publish_error = ThreadsTransportError()
    api.container_status_responses = ["FINISHED", "PUBLISHED", "PUBLISHED"]
    clock = FixedClock()
    runtime = make_runtime(unit_of_work_factory, api, clock, max_attempts=2)
    receipt = await runtime.receive(command_body(account_id, clock))

    first_run = await runtime.process(receipt.command_id)
    clock.advance(timedelta(seconds=1))
    terminal_run = await runtime.process(receipt.command_id)
    command = await db_session.scalar(
        select(CommandRecord).where(CommandRecord.command_id == receipt.command_id)
    )
    post_count = await db_session.scalar(select(func.count()).select_from(PostRecord))
    event_count = await db_session.scalar(
        select(func.count())
        .select_from(OutboxEventRecord)
        .where(OutboxEventRecord.aggregate_id == receipt.command_id)
    )

    assert first_run.status is CommandStatus.FAILED_RETRYABLE
    assert terminal_run.status is CommandStatus.FAILED_FINAL
    assert command is not None
    assert command.error_code == "THREADS_PUBLISH_OUTCOME_AMBIGUOUS"
    assert api.container_status_calls == 3
    assert api.publish_calls == 1
    assert post_count == 1  # seeded root post only; no guessed published media row
    assert event_count == 1


@pytest.mark.parametrize(
    ("error", "expected_status", "expected_code"),
    [
        (
            ThreadsAPIError("THREADS_RATE_LIMITED", timedelta(seconds=2)),
            CommandStatus.FAILED_RETRYABLE,
            "THREADS_RATE_LIMITED",
        ),
        (
            ThreadsAPIError("THREADS_SERVER_ERROR"),
            CommandStatus.FAILED_RETRYABLE,
            "THREADS_SERVER_ERROR",
        ),
        (
            ThreadsAPIError("THREADS_AUTHENTICATION_FAILED"),
            CommandStatus.FAILED_FINAL,
            "THREADS_AUTHENTICATION_FAILED",
        ),
        (
            ThreadsAPIError("THREADS_PERMISSION_DENIED"),
            CommandStatus.FAILED_FINAL,
            "THREADS_PERMISSION_DENIED",
        ),
    ],
)
async def test_quota_failures_are_typed_and_bounded(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
    db_session: AsyncSession,
    error: ThreadsAPIError,
    expected_status: CommandStatus,
    expected_code: str,
) -> None:
    account_id, _ = await seed_account_and_post(unit_of_work_factory)
    api = FakeThreadsAPI()
    api.quota_error = error
    clock = FixedClock()
    runtime = make_runtime(unit_of_work_factory, api, clock)
    receipt = await runtime.receive(command_body(account_id, clock))

    result = await runtime.process(receipt.command_id)
    command = await db_session.scalar(
        select(CommandRecord).where(CommandRecord.command_id == receipt.command_id)
    )

    assert result.status is expected_status
    assert command is not None
    assert command.error_code == expected_code
    assert api.created == []
    assert api.publish_calls == 0


@pytest.mark.parametrize(
    ("container_status", "expected_code"),
    [
        ("IN_PROGRESS", "THREADS_CONTAINER_PROCESSING"),
        ("ERROR", "THREADS_CONTAINER_ERROR"),
        ("EXPIRED", "THREADS_CONTAINER_EXPIRED"),
    ],
)
async def test_container_status_controls_bounded_publish_recovery(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
    db_session: AsyncSession,
    container_status: str,
    expected_code: str,
) -> None:
    account_id, _ = await seed_account_and_post(unit_of_work_factory)
    api = FakeThreadsAPI()
    api.container_status = container_status
    clock = FixedClock()
    runtime = make_runtime(unit_of_work_factory, api, clock)
    receipt = await runtime.receive(command_body(account_id, clock))

    result = await runtime.process(receipt.command_id)
    command = await db_session.scalar(
        select(CommandRecord).where(CommandRecord.command_id == receipt.command_id)
    )

    assert result.status in {CommandStatus.FAILED_RETRYABLE, CommandStatus.FAILED_FINAL}
    assert command is not None
    assert command.error_code == expected_code
    assert api.publish_calls == 0


@pytest.mark.parametrize(
    ("command_type", "payload", "expected_types"),
    [
        ("threads.publish_image", {"image_url": "https://media.example/image.jpg"}, ["IMAGE"]),
        ("threads.publish_video", {"video_url": "https://media.example/video.mp4"}, ["VIDEO"]),
        (
            "threads.publish_carousel",
            {
                "items": [
                    {"media_type": "IMAGE", "url": "https://media.example/image.jpg"},
                    {"media_type": "VIDEO", "url": "https://media.example/video.mp4"},
                ]
            },
            ["IMAGE", "VIDEO", "CAROUSEL"],
        ),
    ],
)
async def test_documented_image_video_and_carousel_container_flows(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
    command_type: str,
    payload: dict[str, object],
    expected_types: list[str],
) -> None:
    account_id, _ = await seed_account_and_post(unit_of_work_factory)
    api = FakeThreadsAPI()
    clock = FixedClock()
    runtime = make_runtime(unit_of_work_factory, api, clock)
    receipt = await runtime.receive(command_body(account_id, clock, command_type, payload))

    result = await runtime.process(receipt.command_id)

    assert result.status is CommandStatus.SUCCEEDED
    assert [request.media_type for request in api.created] == expected_types
    if command_type == "threads.publish_carousel":
        assert all(request.is_carousel_item for request in api.created[:2])
        assert api.created[2].children == ("container-doc-1", "container-doc-2")
    assert api.publish_calls == 1


async def test_quote_post_uses_documented_quote_metadata_and_reply_control(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
) -> None:
    account_id, _ = await seed_account_and_post(unit_of_work_factory)
    api = FakeThreadsAPI()
    clock = FixedClock()
    runtime = make_runtime(unit_of_work_factory, api, clock)
    receipt = await runtime.receive(
        command_body(
            account_id,
            clock,
            "threads.publish_text",
            {
                "text": "A documented quote post",
                "quote_post_id": "quoted-media-doc-example",
                "reply_control": "followers_only",
            },
        )
    )

    result = await runtime.process(receipt.command_id)

    assert result.status is CommandStatus.SUCCEEDED
    assert api.created[0].quote_post_id == "quoted-media-doc-example"
    assert api.created[0].reply_control == "followers_only"


async def test_invalid_media_url_is_rejected_before_meta_adapter(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
) -> None:
    account_id, _ = await seed_account_and_post(unit_of_work_factory)
    api = FakeThreadsAPI()
    clock = FixedClock()
    runtime = make_runtime(unit_of_work_factory, api, clock)
    receipt = await runtime.receive(
        command_body(
            account_id,
            clock,
            "threads.publish_image",
            {"image_url": "file://local/image.png"},
        )
    )

    result = await runtime.process(receipt.command_id)

    assert receipt.status is CommandStatus.REJECTED
    assert result.status is CommandStatus.REJECTED
    assert api.created == []


async def test_invalid_media_error_is_final_and_never_calls_publish(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
    db_session: AsyncSession,
) -> None:
    account_id, _ = await seed_account_and_post(unit_of_work_factory)
    api = FakeThreadsAPI()
    api.create_error = ThreadsAPIError("THREADS_INVALID_REQUEST")
    clock = FixedClock()
    runtime = make_runtime(unit_of_work_factory, api, clock)
    receipt = await runtime.receive(
        command_body(
            account_id,
            clock,
            "threads.publish_image",
            {"image_url": "https://media.example/invalid-image"},
        )
    )

    result = await runtime.process(receipt.command_id)
    command = await db_session.scalar(
        select(CommandRecord).where(CommandRecord.command_id == receipt.command_id)
    )

    assert result.status is CommandStatus.FAILED_FINAL
    assert command is not None
    assert command.error_code == "THREADS_INVALID_REQUEST"
    assert api.publish_calls == 0


async def test_stale_execution_lease_cannot_finalize_business_state_or_result(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
    db_session: AsyncSession,
) -> None:
    account_id, _ = await seed_account_and_post(unit_of_work_factory)
    clock = FixedClock()
    handler = BlockingHandler()
    runtime = CommandRuntime(
        unit_of_work_factory,
        {"threads.publish_text": handler},
        clock=clock,
        execution_lease_duration=timedelta(seconds=30),
    )
    receipt = await runtime.receive(command_body(account_id, clock))
    worker = asyncio.create_task(runtime.process(receipt.command_id))
    await handler.started.wait()
    async with unit_of_work_factory() as unit_of_work:
        command = await unit_of_work.commands.get_by_command_id_for_update(receipt.command_id)
        assert command is not None
        command.execution_lease_token = uuid4()
        await unit_of_work.commands.update(command)
    handler.release.set()
    outcome = await worker
    result_event_count = await db_session.scalar(
        select(func.count())
        .select_from(OutboxEventRecord)
        .where(OutboxEventRecord.aggregate_id == receipt.command_id)
    )

    assert outcome.status is CommandStatus.PROCESSING
    assert outcome.executed is False
    assert result_event_count == 0
