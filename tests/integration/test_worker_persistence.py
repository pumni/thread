from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from threads_platform.domain.accounts import AccountExecutionMode, ThreadsAccount
from threads_platform.domain.workers import (
    AccountWorkerAssignment,
    BrowserProfile,
    NetworkProfile,
    NetworkProtocol,
    WorkerCapability,
    WorkerNode,
    WorkerStatus,
)
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
