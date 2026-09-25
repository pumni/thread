from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from threads_platform.application.ports.repositories import UnitOfWorkFactory
from threads_platform.application.worker_sessions import (
    WorkerSessionControlError,
    WorkerSessionService,
)
from threads_platform.domain.accounts import AccountExecutionMode, ThreadsAccount
from threads_platform.domain.workers import (
    AccountWorkerAssignment,
    BrowserProfile,
    BrowserSessionState,
    NetworkProfile,
    NetworkProtocol,
    WorkerCapability,
    WorkerNode,
    WorkerStatus,
)
from threads_platform.infrastructure.persistence.models import WorkerAccountSessionRecord
from threads_platform.infrastructure.persistence.repositories import (
    SQLAlchemyAccountRepository,
    SQLAlchemyAccountWorkerAssignmentRepository,
    SQLAlchemyBrowserProfileRepository,
    SQLAlchemyNetworkProfileRepository,
    SQLAlchemyWorkerCapabilityRepository,
    SQLAlchemyWorkerRepository,
)

pytestmark = pytest.mark.integration


async def test_worker_assignment_profile_and_network_metadata_round_trip(
    db_session: AsyncSession,
) -> None:
    account = ThreadsAccount(
        threads_user_id=f"worker-account-{uuid4()}",
        username="worker_account",
        execution_mode=AccountExecutionMode.HYBRID,
    )
    accounts = SQLAlchemyAccountRepository(db_session)
    await accounts.add(account)

    worker = WorkerNode(
        worker_id=uuid4(),
        display_name="Office worker",
        hostname="WORKSTATION-04",
        platform="windows",
        status=WorkerStatus.ONLINE,
        max_browser_sessions=4,
        active_browser_sessions=2,
    )
    workers = SQLAlchemyWorkerRepository(db_session)
    await workers.add(worker)
    capabilities = SQLAlchemyWorkerCapabilityRepository(db_session)
    await capabilities.replace_for_worker(
        worker.worker_id,
        [WorkerCapability(worker_id=worker.worker_id, name="synthetic.echo", version=1)],
    )

    profile = BrowserProfile(worker_id=worker.worker_id, profile_ref="threads-main")
    profiles = SQLAlchemyBrowserProfileRepository(db_session)
    await profiles.add(profile)
    network = NetworkProfile(
        account_id=account.id,
        name="account proxy",
        protocol=NetworkProtocol.HTTPS,
        host="proxy.example.test",
        port=8443,
        credential_ref="secret-store://accounts/proxy-1",
    )
    networks = SQLAlchemyNetworkProfileRepository(db_session)
    await networks.add(network)
    assignment = AccountWorkerAssignment(
        account_id=account.id,
        worker_id=worker.worker_id,
        profile_ref=profile.profile_ref,
        network_profile_id=network.id,
    )
    assignments = SQLAlchemyAccountWorkerAssignmentRepository(db_session)
    await assignments.add(assignment)

    loaded_account = await accounts.get(account.id)
    loaded_worker = await workers.get(worker.worker_id)
    loaded_profile = await profiles.get(worker.worker_id, profile.profile_ref)
    loaded_network = await networks.get(account.id, network.id)
    loaded_assignment = await assignments.get_active(account.id)
    loaded_capabilities = await capabilities.list_for_worker(worker.worker_id)

    assert loaded_account is not None
    assert loaded_account.execution_mode is AccountExecutionMode.HYBRID
    assert loaded_worker is not None
    assert loaded_worker.worker_id == worker.worker_id
    assert loaded_worker.hostname == "WORKSTATION-04"
    assert loaded_worker.max_browser_sessions == 4
    assert loaded_worker.active_browser_sessions == 2
    assert loaded_profile is not None and loaded_profile.profile_ref == "threads-main"
    assert loaded_network is not None
    assert loaded_network.credential_ref == "secret-store://accounts/proxy-1"
    assert loaded_assignment is not None
    assert loaded_assignment.worker_id == worker.worker_id
    assert loaded_assignment.profile_ref == "threads-main"
    assert [(capability.name, capability.version) for capability in loaded_capabilities] == [
        ("synthetic.echo", 1)
    ]

    assert loaded_worker is not None
    loaded_worker.status = WorkerStatus.DRAINING
    await workers.update(loaded_worker)
    account.execution_mode = AccountExecutionMode.BROWSER_ONLY
    await accounts.update(account)
    updated_worker = await workers.get(worker.worker_id)
    updated_account = await accounts.get(account.id)
    assert updated_worker is not None and updated_worker.status is WorkerStatus.DRAINING
    assert updated_account is not None
    assert updated_account.execution_mode is AccountExecutionMode.BROWSER_ONLY


async def test_only_one_active_worker_assignment_per_account(db_session: AsyncSession) -> None:
    accounts = SQLAlchemyAccountRepository(db_session)
    account = ThreadsAccount(threads_user_id=f"worker-account-{uuid4()}", username="example")
    await accounts.add(account)
    workers = SQLAlchemyWorkerRepository(db_session)
    profiles = SQLAlchemyBrowserProfileRepository(db_session)
    assignments = SQLAlchemyAccountWorkerAssignmentRepository(db_session)
    first_worker = WorkerNode(uuid4(), "first", "first-host")
    second_worker = WorkerNode(uuid4(), "second", "second-host")
    await workers.add(first_worker)
    await workers.add(second_worker)
    await profiles.add(BrowserProfile(first_worker.worker_id, "profile-a"))
    await profiles.add(BrowserProfile(second_worker.worker_id, "profile-b"))
    await assignments.add(AccountWorkerAssignment(account.id, first_worker.worker_id, "profile-a"))

    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await assignments.add(
                AccountWorkerAssignment(account.id, second_worker.worker_id, "profile-b")
            )


async def test_assignment_foreign_key_keeps_profile_on_the_assigned_worker(
    db_session: AsyncSession,
) -> None:
    accounts = SQLAlchemyAccountRepository(db_session)
    account = ThreadsAccount(threads_user_id=f"worker-account-{uuid4()}", username="example")
    await accounts.add(account)
    workers = SQLAlchemyWorkerRepository(db_session)
    profiles = SQLAlchemyBrowserProfileRepository(db_session)
    first_worker = WorkerNode(uuid4(), "first", "first-host")
    second_worker = WorkerNode(uuid4(), "second", "second-host")
    await workers.add(first_worker)
    await workers.add(second_worker)
    await profiles.add(BrowserProfile(first_worker.worker_id, "profile-a"))
    assignments = SQLAlchemyAccountWorkerAssignmentRepository(db_session)

    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await assignments.add(
                AccountWorkerAssignment(account.id, second_worker.worker_id, "profile-a")
            )


async def test_active_profile_cannot_be_shared_between_accounts(db_session: AsyncSession) -> None:
    accounts = SQLAlchemyAccountRepository(db_session)
    worker = WorkerNode(uuid4(), "worker", "host")
    await SQLAlchemyWorkerRepository(db_session).add(worker)
    first = ThreadsAccount(threads_user_id=f"profile-owner-{uuid4()}", username="first")
    second = ThreadsAccount(threads_user_id=f"profile-owner-{uuid4()}", username="second")
    await accounts.add(first)
    await accounts.add(second)
    profile = BrowserProfile(worker.worker_id, "single-owner-profile")
    await SQLAlchemyBrowserProfileRepository(db_session).add(profile)
    assignments = SQLAlchemyAccountWorkerAssignmentRepository(db_session)
    await assignments.add(AccountWorkerAssignment(first.id, worker.worker_id, profile.profile_ref))

    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await assignments.add(
                AccountWorkerAssignment(second.id, worker.worker_id, profile.profile_ref)
            )


async def test_worker_session_context_and_intervention_state_are_durable(
    unit_of_work_factory: UnitOfWorkFactory,
    db_session: AsyncSession,
) -> None:
    account = ThreadsAccount(threads_user_id=f"session-account-{uuid4()}", username="session")
    worker = WorkerNode(uuid4(), "session worker", "session-host", platform="windows")
    profile = BrowserProfile(worker.worker_id, f"profile-{uuid4()}")
    network = NetworkProfile(
        account_id=account.id,
        name="account route",
        protocol=NetworkProtocol.HTTPS,
        host="proxy.example.test",
        port=8443,
        credential_ref="secret-store://account/proxy",
    )
    async with unit_of_work_factory() as unit_of_work:
        await unit_of_work.accounts.add(account)
        await unit_of_work.workers.add(worker)
        await unit_of_work.browser_profiles.add(profile)
        await unit_of_work.network_profiles.add(network)
        await unit_of_work.assignments.add(
            AccountWorkerAssignment(
                account.id,
                worker.worker_id,
                profile.profile_ref,
                network_profile_id=network.id,
            )
        )

    service = WorkerSessionService(unit_of_work_factory)
    context = await service.account_context(worker.worker_id, account.id)
    assert context.profile_ref == profile.profile_ref
    assert context.network_profile is not None
    assert context.network_profile.account_id == account.id
    with pytest.raises(WorkerSessionControlError, match="ACCOUNT_WORKER_AFFINITY_MISMATCH"):
        await service.account_context(uuid4(), account.id)

    session_id = uuid4()
    login_required = await service.report_state(
        worker.worker_id,
        account_id=account.id,
        profile_ref=profile.profile_ref,
        session_id=session_id,
        state=BrowserSessionState.LOGIN_REQUIRED,
        revision=1,
    )
    assert login_required.requires_intervention
    assert (
        await service.report_state(
            worker.worker_id,
            account_id=account.id,
            profile_ref=profile.profile_ref,
            session_id=session_id,
            state=BrowserSessionState.LOGIN_REQUIRED,
            revision=1,
        )
        == login_required
    )
    with pytest.raises(WorkerSessionControlError, match="SESSION_REPORT_STALE"):
        await service.report_state(
            worker.worker_id,
            account_id=account.id,
            profile_ref=profile.profile_ref,
            session_id=session_id,
            state=BrowserSessionState.SESSION_EXPIRED,
            revision=1,
        )
    challenge = await service.report_state(
        worker.worker_id,
        account_id=account.id,
        profile_ref=profile.profile_ref,
        session_id=session_id,
        state=BrowserSessionState.CHALLENGE_REQUIRED,
        revision=2,
    )
    assert challenge.requires_intervention

    stored = await db_session.get(WorkerAccountSessionRecord, account.id)
    assert stored is not None
    assert stored.worker_id == worker.worker_id
    assert stored.state == BrowserSessionState.CHALLENGE_REQUIRED
    assert stored.intervention_required
    assert "secret-store://account/proxy" not in repr(stored)
