from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import cast
from uuid import uuid4

import httpx2
import pytest
import structlog.testing
from opentelemetry.trace import Tracer
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import threads_platform.app as app_module
from threads_platform.app import create_app
from threads_platform.application.commands.runtime import CommandRuntime
from threads_platform.application.crm_protocol_v1 import CommandReceiptV1
from threads_platform.application.worker_control import WorkerControlService
from threads_platform.config.settings import Settings
from threads_platform.domain.commands import CommandStatus
from threads_platform.domain.operators import OperatorRole, OperatorUser
from threads_platform.infrastructure.persistence.models import (
    OperatorSessionRecord,
    OperatorUserRecord,
    WorkerEnrollmentRecord,
    WorkspaceRecord,
)
from threads_platform.infrastructure.security.operator_auth import (
    OperatorAuthError,
    OperatorAuthService,
)
from threads_platform.infrastructure.security.worker_auth import challenge_message
from threads_platform.workers.key_store import WorkerDeviceIdentity

pytestmark = pytest.mark.integration


class FakeCommandRuntime:
    def set_execution_duration_observer(
        self,
        observer: Callable[[float], None] | None,
    ) -> None:
        del observer

    def set_tracer(self, tracer: Tracer | None, *, process_role: str) -> None:
        del tracer, process_role

    async def receive(self, command: dict[str, object]) -> CommandReceiptV1:
        return CommandReceiptV1(
            command_id=str(command.get("command_id", "synthetic-command")),
            correlation_id=str(command.get("correlation_id", "synthetic-correlation")),
            status=CommandStatus.RECEIVED,
        )


class MutableClock:
    def __init__(self) -> None:
        self.current = datetime(2026, 10, 3, 12, tzinfo=UTC)

    def now(self) -> datetime:
        return self.current

    def advance(self, duration: timedelta) -> None:
        self.current += duration


async def _login(client: httpx2.AsyncClient, username: str, password: str) -> dict[str, object]:
    response = await client.post(
        "/v1/operator/login",
        json={"username": username, "password": password},
    )
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    return response.json()


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _create_user(
    service: OperatorAuthService,
    actor_token: str,
    username: str,
    role: OperatorRole,
) -> tuple[OperatorUser, str]:
    created = await service.create_user(actor_token, username, role)
    return created.user, created.temporary_password


async def test_operator_migration_creates_one_workspace_and_digest_only_sessions(
    db_session: AsyncSession,
    operator_auth_service: OperatorAuthService,
) -> None:
    workspaces = list(await db_session.scalars(select(WorkspaceRecord)))
    assert len(workspaces) == 1
    owner = await operator_auth_service.bootstrap_first_owner(
        "first-owner", "long synthetic passphrase"
    )
    with pytest.raises(OperatorAuthError, match="OPERATOR_BOOTSTRAP_ALREADY_COMPLETED"):
        await operator_auth_service.bootstrap_first_owner(
            "second-owner", "long synthetic passphrase"
        )

    issued = await operator_auth_service.login(owner.username, "long synthetic passphrase")
    assert issued is not None
    assert issued.access_token not in repr(issued)

    stored = list(await db_session.scalars(select(OperatorSessionRecord)))
    assert len(stored) == 1
    assert stored[0].token_digest == hashlib.sha256(issued.access_token.encode()).hexdigest()
    assert issued.access_token not in stored[0].token_digest
    assert await operator_auth_service.authenticate(issued.access_token) is not None
    await operator_auth_service.logout(issued.access_token)
    assert await operator_auth_service.authenticate(issued.access_token) is None


async def test_operator_http_four_role_matrix_legacy_isolation_and_server_actor(
    unit_of_work_factory: object,
    operator_auth_service: OperatorAuthService,
    db_session: AsyncSession,
) -> None:
    owner = await operator_auth_service.bootstrap_first_owner("owner", "owner passphrase 2026")
    owner_login = await operator_auth_service.login(owner.username, "owner passphrase 2026")
    assert owner_login is not None
    roles = {
        OperatorRole.ADMIN: "admin",
        OperatorRole.OPERATOR: "operator",
        OperatorRole.VIEWER: "viewer",
    }
    role_tokens = {OperatorRole.OWNER: owner_login.access_token}
    users_by_role = {OperatorRole.OWNER: owner}
    for role, username in roles.items():
        user, temporary = await _create_user(
            operator_auth_service,
            owner_login.access_token,
            username,
            role,
        )
        login = await operator_auth_service.login(username, temporary)
        assert login is not None and login.operator.must_change_password
        changed = await operator_auth_service.change_password(
            login.access_token,
            f"{username} new passphrase 2026",
        )
        assert not changed.must_change_password
        role_tokens[role] = login.access_token
        users_by_role[role] = user
        assert user.role == role.value

    legacy_token = "SYNTHETIC_LEGACY_WORKER_ADMIN_TOKEN"
    crm_token = "SYNTHETIC_CRM_INGRESS_TOKEN"
    control = WorkerControlService(unit_of_work_factory)  # type: ignore[arg-type]
    app = create_app(
        Settings(
            database_url=None,
            worker_admin_token=SecretStr(legacy_token),
            crm_ingress_token=SecretStr(crm_token),
            worker_tls_required=True,
        ),
        worker_control_service=control,
        operator_auth_service=operator_auth_service,
        command_runtime=cast(CommandRuntime, FakeCommandRuntime()),
    )
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app),
        base_url="https://operator.test",
    ) as client:
        api_owner_login = await _login(client, owner.username, "owner passphrase 2026")
        role_tokens[OperatorRole.OWNER] = str(api_owner_login["access_token"])
        for role, token in role_tokens.items():
            assert (await client.get("/v1/operator/me", headers=_headers(token))).status_code == 200
            drain_view = await client.get(
                f"/v1/workers/{uuid4()}/drain",
                headers=_headers(token),
            )
            assert drain_view.status_code == 404
            users_response = await client.get("/v1/operator/users", headers=_headers(token))
            users_status = users_response.status_code
            assert users_status == (
                200 if role in {OperatorRole.OWNER, OperatorRole.ADMIN} else 403
            )
            if users_status == 200:
                assert users_response.headers["Cache-Control"] == "no-store"
            creatable_roles: set[OperatorRole] = (
                set(OperatorRole)
                if role is OperatorRole.OWNER
                else {OperatorRole.OPERATOR, OperatorRole.VIEWER}
                if role is OperatorRole.ADMIN
                else set()
            )
            for target_role in OperatorRole:
                created = await client.post(
                    "/v1/operator/users",
                    headers=_headers(token),
                    json={
                        "username": f"matrix-{role.value.lower()}-{target_role.value.lower()}",
                        "role": target_role.value,
                    },
                )
                assert created.status_code == (201 if target_role in creatable_roles else 403)
            for target_role, target_user in users_by_role.items():
                may_change = role is OperatorRole.OWNER or (
                    role is OperatorRole.ADMIN
                    and target_role in {OperatorRole.OPERATOR, OperatorRole.VIEWER}
                )
                updated = await client.patch(
                    f"/v1/operator/users/{target_user.id}",
                    headers=_headers(token),
                    json={"enabled": True},
                )
                assert updated.status_code == (200 if may_change else 403)
            enrollment = await client.post(
                "/v1/workers/enrollments",
                headers=_headers(token),
                json={"created_by": "forged-client-actor"},
            )
            assert enrollment.status_code == (
                200 if role in {OperatorRole.OWNER, OperatorRole.ADMIN} else 403
            )
            command = await client.post(
                "/v1/operator/commands",
                headers=_headers(token),
                json={
                    "command_id": f"{role.value}-command",
                    "correlation_id": "synthetic-correlation",
                },
            )
            assert command.status_code == (403 if role is OperatorRole.VIEWER else 202)

        assert (
            await client.post(
                "/v1/workers/enrollments",
                headers=_headers(legacy_token),
                json={},
            )
        ).status_code == 401
        assert (
            await client.get("/v1/operator/me", headers=_headers(legacy_token))
        ).status_code == 401
        assert (await client.get("/v1/operator/me", headers=_headers(crm_token))).status_code == 401
        assert (
            await client.post(
                "/v1/commands", headers=_headers(role_tokens[OperatorRole.OWNER]), json={}
            )
        ).status_code == 401

        enrollment = await control.create_enrollment()
        identity = WorkerDeviceIdentity.generate()
        worker_id = uuid4()
        await control.enroll(
            enrollment.code,
            worker_id=worker_id,
            display_name="Synthetic worker",
            hostname="SYNTHETIC-HOST",
            platform="windows",
            public_key=identity.public_key_bytes,
        )
        challenge = await control.create_challenge(worker_id)
        signature = identity.sign(challenge_message(challenge.challenge_id, challenge.nonce))
        worker_session = await control.exchange_challenge(challenge.challenge_id, signature)
        assert (
            await client.post(
                "/v1/workers/enrollments",
                headers=_headers(worker_session.access_token),
                json={},
            )
        ).status_code == 401
        assert (
            await client.get("/v1/operator/me", headers=_headers(worker_session.access_token))
        ).status_code == 401

    enrollments = list(await db_session.scalars(select(WorkerEnrollmentRecord)))
    assert [item.created_by for item in enrollments if item.created_by == "admin"]
    assert all(item.created_by != "forged-client-actor" for item in enrollments)
    audit_events = await operator_auth_service.audit_events()
    assert any(
        event.event_type == "worker.enrollment_created" and event.actor_username == "admin"
        for event in audit_events
    )
    assert all(event.actor_username != "forged-client-actor" for event in audit_events)


async def test_windows_profile_disables_legacy_token_even_if_configured(
    unit_of_work_factory: object,
    operator_auth_service: OperatorAuthService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    legacy_token = "SYNTHETIC_LEGACY_WORKER_ADMIN_TOKEN"
    control = WorkerControlService(unit_of_work_factory)  # type: ignore[arg-type]
    monkeypatch.setattr(app_module, "sys", SimpleNamespace(platform="win32"))
    app = create_app(
        Settings(
            database_url=None,
            worker_admin_token=SecretStr(legacy_token),
            worker_admin_auth_profile="legacy_linux_it",
        ),
        worker_control_service=control,
        operator_auth_service=operator_auth_service,
    )
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app),
        base_url="https://operator.test",
    ) as client:
        response = await client.post(
            "/v1/workers/enrollments",
            headers=_headers(legacy_token),
            json={},
        )
    assert response.status_code == 401


async def test_expiry_role_downgrade_and_disable_take_effect_on_next_request(
    unit_of_work_factory: object,
    operator_auth_service: OperatorAuthService,
) -> None:
    owner = await operator_auth_service.bootstrap_first_owner("owner", "owner passphrase 2026")
    owner_login = await operator_auth_service.login(owner.username, "owner passphrase 2026")
    assert owner_login is not None
    created = await operator_auth_service.create_user(
        owner_login.access_token,
        "operator",
        OperatorRole.OPERATOR,
    )
    operator_login = await operator_auth_service.login(
        created.user.username,
        created.temporary_password,
    )
    assert operator_login is not None
    await operator_auth_service.change_password(
        operator_login.access_token, "operator passphrase 2026"
    )
    app = create_app(
        Settings(database_url=None),
        operator_auth_service=operator_auth_service,
    )
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app),
        base_url="https://operator.test",
    ) as client:
        first = await client.get("/v1/operator/me", headers=_headers(operator_login.access_token))
        assert first.status_code == 200 and first.json()["role"] == "OPERATOR"
        downgraded = await operator_auth_service.update_user(
            owner_login.access_token,
            created.user.id,
            role=OperatorRole.VIEWER,
            enabled=None,
        )
        assert downgraded.role is OperatorRole.VIEWER
        next_request = await client.get(
            "/v1/operator/me",
            headers=_headers(operator_login.access_token),
        )
        assert next_request.status_code == 200 and next_request.json()["role"] == "VIEWER"
        forbidden = await client.post(
            "/v1/operator/commands",
            headers=_headers(operator_login.access_token),
            json={},
        )
        assert forbidden.status_code == 403
        await operator_auth_service.update_user(
            owner_login.access_token,
            created.user.id,
            role=None,
            enabled=False,
        )
        disabled = await client.get(
            "/v1/operator/me", headers=_headers(operator_login.access_token)
        )
        assert disabled.status_code == 401
        await operator_auth_service.update_user(
            owner_login.access_token,
            created.user.id,
            role=None,
            enabled=True,
        )
        still_revoked = await client.get(
            "/v1/operator/me", headers=_headers(operator_login.access_token)
        )
        assert still_revoked.status_code == 401

        await client.post("/v1/operator/logout", headers=_headers(owner_login.access_token))
        assert (
            await client.get("/v1/operator/me", headers=_headers(owner_login.access_token))
        ).status_code == 401


async def test_concurrent_last_enabled_owner_changes_leave_an_enabled_owner(
    operator_auth_service: OperatorAuthService,
    db_session: AsyncSession,
) -> None:
    first = await operator_auth_service.bootstrap_first_owner("owner-one", "owner one passphrase")
    first_session = await operator_auth_service.login(first.username, "owner one passphrase")
    assert first_session is not None
    second = await operator_auth_service.create_user(
        first_session.access_token,
        "owner-two",
        OperatorRole.OWNER,
    )
    second_session = await operator_auth_service.login(
        second.user.username,
        second.temporary_password,
    )
    assert second_session is not None
    await operator_auth_service.change_password(second_session.access_token, "owner two passphrase")

    outcomes = await asyncio.gather(
        operator_auth_service.update_user(
            first_session.access_token,
            first.id,
            role=OperatorRole.ADMIN,
            enabled=None,
        ),
        operator_auth_service.update_user(
            second_session.access_token,
            second.user.id,
            role=None,
            enabled=False,
        ),
        return_exceptions=True,
    )
    assert sum(isinstance(item, OperatorAuthError) for item in outcomes) == 1
    assert any(
        isinstance(item, OperatorAuthError) and item.code == "LAST_ENABLED_OWNER_REQUIRED"
        for item in outcomes
    )
    owners = list(
        await db_session.scalars(
            select(OperatorUserRecord).where(
                OperatorUserRecord.role == OperatorRole.OWNER.value,
                OperatorUserRecord.enabled.is_(True),
            )
        )
    )
    assert len(owners) >= 1


async def test_operator_login_expiry_and_redaction(
    operator_session_factory: object,
) -> None:
    clock = MutableClock()
    service = OperatorAuthService(
        operator_session_factory,  # type: ignore[arg-type]
        clock=clock.now,
        session_ttl=timedelta(seconds=60),
    )
    owner = await service.bootstrap_first_owner("expiry-owner", "expiry passphrase 2026")
    issued = await service.login(owner.username, "expiry passphrase 2026")
    assert issued is not None
    with structlog.testing.capture_logs() as events:
        await service.login(owner.username, "wrong passphrase value")
    assert issued.access_token not in repr(events)
    assert "expiry passphrase 2026" not in repr(events)
    clock.advance(timedelta(seconds=61))
    assert await service.authenticate(issued.access_token) is None
