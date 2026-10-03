from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from threads_platform.application.operator_access import OperatorAction, role_allows
from threads_platform.domain.operators import OperatorPrincipal, OperatorRole, OperatorUser
from threads_platform.infrastructure.persistence.models import (
    OperatorLoginThrottleRecord,
    OperatorSessionRecord,
    OperatorUserRecord,
    WorkspaceAuditEventRecord,
    WorkspaceRecord,
)

SessionFactory = Callable[[], AsyncSession]
Clock = Callable[[], datetime]

WORKSPACE_SINGLETON_KEY = 1
SESSION_TTL = timedelta(hours=8)
LOGIN_FAILURE_WINDOW = timedelta(minutes=15)
LOGIN_LOCK_DURATION = timedelta(minutes=5)
LOGIN_FAILURE_LIMIT = 5
SCRYPT_N = 1 << 15
SCRYPT_R = 8
SCRYPT_P = 1
_USERNAME_RE = re.compile(r"[a-z0-9][a-z0-9._-]{2,99}\Z")
_DUMMY_PASSWORD_HASH = "scrypt$32768$8$1$" + "00" * 16 + "$" + "00" * 32


class OperatorAuthError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class IssuedOperatorSession:
    access_token: str = field(repr=False)
    expires_at: datetime
    operator: OperatorPrincipal


@dataclass(frozen=True, slots=True)
class CreatedOperatorUser:
    user: OperatorUser
    temporary_password: str = field(repr=False)


def _now() -> datetime:
    return datetime.now(UTC)


def normalize_username(username: str) -> str:
    normalized = username.strip().casefold()
    if not _USERNAME_RE.fullmatch(normalized):
        raise OperatorAuthError("OPERATOR_USERNAME_INVALID")
    return normalized


def validate_password(password: str) -> None:
    if len(password) < 12 or len(password) > 1024 or not password.strip():
        raise OperatorAuthError("OPERATOR_PASSWORD_INVALID")


def _password_hash(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
        dklen=32,
        maxmem=64 * 1024 * 1024,
    )
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${salt.hex()}${digest.hex()}"


def _password_matches(password: str, encoded: str) -> bool:
    try:
        algorithm, n_text, r_text, p_text, salt_hex, expected_hex = encoded.split("$")
        n, r, p = int(n_text), int(r_text), int(p_text)
        if algorithm != "scrypt" or n < 1 << 14 or n > 1 << 18 or r > 16 or p > 4:
            return False
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(expected_hex)
        if len(salt) != 16 or len(expected) != 32:
            return False
        actual = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=n,
            r=r,
            p=p,
            dklen=32,
            maxmem=128 * 1024 * 1024,
        )
        return hmac.compare_digest(actual, expected)
    except ValueError, UnicodeEncodeError, MemoryError:
        return False


def _user_document(record: OperatorUserRecord) -> OperatorUser:
    return OperatorUser(
        id=record.id,
        username=record.username,
        role=OperatorRole(record.role),
        enabled=record.enabled,
        must_change_password=record.must_change_password,
        created_at=record.created_at,
    )


class OperatorAuthService:
    """PostgreSQL-backed Workspace Operator login, sessions, RBAC and audit."""

    def __init__(
        self,
        session_factory: SessionFactory,
        *,
        clock: Clock = _now,
        session_ttl: timedelta = SESSION_TTL,
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock
        self._session_ttl = session_ttl

    async def bootstrap_first_owner(self, username: str, password: str) -> OperatorUser:
        normalized = normalize_username(username)
        validate_password(password)
        now = self._clock()
        async with self._session_factory() as session, session.begin():
            workspace = await self._lock_workspace(session)
            existing = await session.scalar(
                select(OperatorUserRecord.id).where(
                    OperatorUserRecord.workspace_id == workspace.workspace_id
                )
            )
            if existing is not None:
                raise OperatorAuthError("OPERATOR_BOOTSTRAP_ALREADY_COMPLETED")
            record = OperatorUserRecord(
                id=uuid4(),
                workspace_id=workspace.workspace_id,
                username=normalized,
                role=OperatorRole.OWNER.value,
                password_hash=_password_hash(password),
                enabled=True,
                must_change_password=False,
                created_at=now,
                updated_at=now,
            )
            session.add(record)
            self._audit(
                session,
                workspace.workspace_id,
                record.id,
                record.username,
                "operator.owner_bootstrapped",
                "operator_user",
                str(record.id),
                {},
                now,
            )
            await session.flush()
            return _user_document(record)

    async def login(self, username: str, password: str) -> IssuedOperatorSession | None:
        try:
            normalized = normalize_username(username)
        except OperatorAuthError:
            _password_matches(password, _DUMMY_PASSWORD_HASH)
            return None
        now = self._clock()
        async with self._session_factory() as session, session.begin():
            user = await session.scalar(
                select(OperatorUserRecord)
                .where(OperatorUserRecord.username == normalized)
                .with_for_update()
            )
            if user is None:
                _password_matches(password, _DUMMY_PASSWORD_HASH)
                return None

            throttle = await session.get(OperatorLoginThrottleRecord, user.id, with_for_update=True)
            encoded_hash = user.password_hash
            password_matches = _password_matches(password, encoded_hash)
            if (
                throttle is not None
                and throttle.locked_until is not None
                and throttle.locked_until > now
            ):
                return None
            if not user.enabled or not password_matches:
                if user.enabled:
                    self._record_login_failure(session, user.id, now, throttle)
                return None

            await session.execute(
                delete(OperatorLoginThrottleRecord).where(
                    OperatorLoginThrottleRecord.operator_user_id == user.id
                )
            )
            token = secrets.token_urlsafe(32)
            expires_at = now + self._session_ttl
            session.add(
                OperatorSessionRecord(
                    id=uuid4(),
                    operator_user_id=user.id,
                    token_digest=hashlib.sha256(token.encode("ascii")).hexdigest(),
                    created_at=now,
                    expires_at=expires_at,
                )
            )
            self._audit(
                session,
                user.workspace_id,
                user.id,
                user.username,
                "operator.login_succeeded",
                "operator_user",
                str(user.id),
                {},
                now,
            )
            principal = OperatorPrincipal(
                user_id=user.id,
                username=user.username,
                role=OperatorRole(user.role),
                enabled=user.enabled,
                must_change_password=user.must_change_password,
                expires_at=expires_at,
            )
            return IssuedOperatorSession(token, expires_at, principal)

    async def authenticate(self, token: str | None) -> OperatorPrincipal | None:
        if not token:
            return None
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        now = self._clock()
        async with self._session_factory() as session:
            result = await session.execute(
                select(OperatorSessionRecord, OperatorUserRecord)
                .join(
                    OperatorUserRecord,
                    OperatorUserRecord.id == OperatorSessionRecord.operator_user_id,
                )
                .where(
                    OperatorSessionRecord.token_digest == digest,
                    OperatorSessionRecord.revoked_at.is_(None),
                    OperatorSessionRecord.expires_at > now,
                    OperatorUserRecord.enabled.is_(True),
                )
            )
            pair = result.one_or_none()
            if pair is None:
                return None
            session_record, user = pair
            return OperatorPrincipal(
                user_id=user.id,
                username=user.username,
                role=OperatorRole(user.role),
                enabled=user.enabled,
                must_change_password=user.must_change_password,
                expires_at=session_record.expires_at,
            )

    async def logout(self, token: str | None) -> None:
        if not token:
            return
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        now = self._clock()
        async with self._session_factory() as session, session.begin():
            pair = await self._session_pair(session, digest, now, lock=True)
            if pair is None:
                return
            session_record, user = pair
            session_record.revoked_at = now
            self._audit(
                session,
                user.workspace_id,
                user.id,
                user.username,
                "operator.logout",
                "operator_session",
                str(session_record.id),
                {},
                now,
            )

    async def list_users(self, token: str) -> list[OperatorUser]:
        async with self._session_factory() as session, session.begin():
            pair = await self._session_pair(session, self._token_digest(token), self._clock())
            if pair is None:
                raise OperatorAuthError("OPERATOR_SESSION_INVALID")
            _, actor = pair
            self._require_password_changed(actor)
            if actor.role not in {OperatorRole.OWNER.value, OperatorRole.ADMIN.value}:
                raise OperatorAuthError("OPERATOR_FORBIDDEN")
            records = await session.scalars(
                select(OperatorUserRecord)
                .where(OperatorUserRecord.workspace_id == actor.workspace_id)
                .order_by(OperatorUserRecord.username)
            )
            return [_user_document(record) for record in records]

    async def create_user(
        self, token: str, username: str, role: OperatorRole
    ) -> CreatedOperatorUser:
        normalized = normalize_username(username)
        temporary_password = secrets.token_urlsafe(24)
        now = self._clock()
        async with self._session_factory() as session, session.begin():
            workspace = await self._lock_workspace(session)
            pair = await self._session_pair(session, self._token_digest(token), now, lock=True)
            if pair is None:
                raise OperatorAuthError("OPERATOR_SESSION_INVALID")
            _, actor = pair
            self._require_password_changed(actor)
            required_action = (
                OperatorAction.MANAGE_OWNER_ADMIN
                if role in {OperatorRole.OWNER, OperatorRole.ADMIN}
                else OperatorAction.MANAGE_OPERATOR_VIEWER
            )
            self._require_action(OperatorRole(actor.role), required_action)
            record = OperatorUserRecord(
                id=uuid4(),
                workspace_id=workspace.workspace_id,
                username=normalized,
                role=role.value,
                password_hash=_password_hash(temporary_password),
                enabled=True,
                must_change_password=True,
                created_at=now,
                updated_at=now,
            )
            session.add(record)
            await session.flush()
            self._audit(
                session,
                workspace.workspace_id,
                actor.id,
                actor.username,
                "operator.user_created",
                "operator_user",
                str(record.id),
                {"role": role.value},
                now,
            )
            return CreatedOperatorUser(_user_document(record), temporary_password)

    async def update_user(
        self,
        token: str,
        user_id: UUID,
        *,
        role: OperatorRole | None,
        enabled: bool | None,
    ) -> OperatorUser:
        if role is None and enabled is None:
            raise OperatorAuthError("OPERATOR_USER_CHANGE_REQUIRED")
        now = self._clock()
        async with self._session_factory() as session, session.begin():
            workspace = await self._lock_workspace(session)
            pair = await self._session_pair(session, self._token_digest(token), now, lock=True)
            if pair is None:
                raise OperatorAuthError("OPERATOR_SESSION_INVALID")
            _, actor = pair
            self._require_password_changed(actor)
            target = await session.scalar(
                select(OperatorUserRecord)
                .where(
                    OperatorUserRecord.id == user_id,
                    OperatorUserRecord.workspace_id == workspace.workspace_id,
                )
                .with_for_update()
            )
            if target is None:
                raise OperatorAuthError("OPERATOR_USER_NOT_FOUND")
            is_privileged_target = target.role in {
                OperatorRole.OWNER.value,
                OperatorRole.ADMIN.value,
            } or role in {OperatorRole.OWNER, OperatorRole.ADMIN}
            self._require_action(
                OperatorRole(actor.role),
                OperatorAction.MANAGE_OWNER_ADMIN
                if is_privileged_target
                else OperatorAction.MANAGE_OPERATOR_VIEWER,
            )
            old_role, old_enabled = target.role, target.enabled
            if role is not None:
                target.role = role.value
            if enabled is not None:
                target.enabled = enabled
            target.updated_at = now
            await session.flush()
            owner_count = await session.scalar(
                select(func.count())
                .select_from(OperatorUserRecord)
                .where(
                    OperatorUserRecord.workspace_id == workspace.workspace_id,
                    OperatorUserRecord.role == OperatorRole.OWNER.value,
                    OperatorUserRecord.enabled.is_(True),
                )
            )
            if not owner_count:
                raise OperatorAuthError("LAST_ENABLED_OWNER_REQUIRED")
            if old_enabled and not target.enabled:
                await session.execute(
                    update(OperatorSessionRecord)
                    .where(
                        OperatorSessionRecord.operator_user_id == target.id,
                        OperatorSessionRecord.revoked_at.is_(None),
                    )
                    .values(revoked_at=now)
                )
            self._audit(
                session,
                workspace.workspace_id,
                actor.id,
                actor.username,
                "operator.user_updated",
                "operator_user",
                str(target.id),
                {
                    "role_changed": old_role != target.role,
                    "enabled_changed": old_enabled != target.enabled,
                },
                now,
            )
            return _user_document(target)

    async def change_password(self, token: str, new_password: str) -> OperatorPrincipal:
        validate_password(new_password)
        now = self._clock()
        digest = self._token_digest(token)
        async with self._session_factory() as session, session.begin():
            pair = await self._session_pair(session, digest, now, lock=True)
            if pair is None:
                raise OperatorAuthError("OPERATOR_SESSION_INVALID")
            session_record, user = pair
            user.password_hash = _password_hash(new_password)
            user.must_change_password = False
            user.updated_at = now
            await session.execute(
                update(OperatorSessionRecord)
                .where(
                    OperatorSessionRecord.operator_user_id == user.id,
                    OperatorSessionRecord.id != session_record.id,
                    OperatorSessionRecord.revoked_at.is_(None),
                )
                .values(revoked_at=now)
            )
            self._audit(
                session,
                user.workspace_id,
                user.id,
                user.username,
                "operator.password_changed",
                "operator_user",
                str(user.id),
                {},
                now,
            )
            return OperatorPrincipal(
                user_id=user.id,
                username=user.username,
                role=OperatorRole(user.role),
                enabled=user.enabled,
                must_change_password=False,
                expires_at=session_record.expires_at,
            )

    async def record_action(
        self,
        token: str,
        event_type: str,
        target_type: str | None = None,
        target_id: str | None = None,
        details: Mapping[str, str | int | bool | None] | None = None,
    ) -> None:
        now = self._clock()
        async with self._session_factory() as session, session.begin():
            pair = await self._session_pair(session, self._token_digest(token), now)
            if pair is None:
                raise OperatorAuthError("OPERATOR_SESSION_INVALID")
            _, actor = pair
            self._audit(
                session,
                actor.workspace_id,
                actor.id,
                actor.username,
                event_type,
                target_type,
                target_id,
                dict(details or {}),
                now,
            )

    async def audit_events(self, *, limit: int = 100) -> list[WorkspaceAuditEventRecord]:
        async with self._session_factory() as session:
            records = await session.scalars(
                select(WorkspaceAuditEventRecord)
                .order_by(WorkspaceAuditEventRecord.created_at.desc())
                .limit(limit)
            )
            return list(records)

    async def _lock_workspace(self, session: AsyncSession) -> WorkspaceRecord:
        workspace = await session.scalar(
            select(WorkspaceRecord)
            .where(WorkspaceRecord.singleton_key == WORKSPACE_SINGLETON_KEY)
            .with_for_update()
        )
        if workspace is None:
            raise OperatorAuthError("WORKSPACE_NOT_INITIALIZED")
        return workspace

    async def _session_pair(
        self, session: AsyncSession, digest: str, now: datetime, *, lock: bool = False
    ) -> tuple[OperatorSessionRecord, OperatorUserRecord] | None:
        statement = (
            select(OperatorSessionRecord, OperatorUserRecord)
            .join(
                OperatorUserRecord,
                OperatorUserRecord.id == OperatorSessionRecord.operator_user_id,
            )
            .where(
                OperatorSessionRecord.token_digest == digest,
                OperatorSessionRecord.revoked_at.is_(None),
                OperatorSessionRecord.expires_at > now,
                OperatorUserRecord.enabled.is_(True),
            )
        )
        if lock:
            statement = statement.with_for_update(of=OperatorSessionRecord, read=True)
        result = await session.execute(statement)
        return result.one_or_none()

    def _record_login_failure(
        self,
        session: AsyncSession,
        user_id: UUID,
        now: datetime,
        throttle: OperatorLoginThrottleRecord | None,
    ) -> None:
        if throttle is None:
            session.add(
                OperatorLoginThrottleRecord(
                    operator_user_id=user_id,
                    failed_attempts=1,
                    window_started_at=now,
                )
            )
            return
        if now - throttle.window_started_at >= LOGIN_FAILURE_WINDOW:
            throttle.failed_attempts = 0
            throttle.window_started_at = now
            throttle.locked_until = None
        throttle.failed_attempts = min(throttle.failed_attempts + 1, LOGIN_FAILURE_LIMIT)
        if throttle.failed_attempts >= LOGIN_FAILURE_LIMIT:
            throttle.locked_until = now + LOGIN_LOCK_DURATION

    @staticmethod
    def _require_password_changed(user: OperatorUserRecord) -> None:
        if user.must_change_password:
            raise OperatorAuthError("OPERATOR_PASSWORD_CHANGE_REQUIRED")

    @staticmethod
    def _require_action(role: OperatorRole, action: OperatorAction) -> None:
        if not role_allows(role, action):
            raise OperatorAuthError("OPERATOR_FORBIDDEN")

    @staticmethod
    def _token_digest(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    @staticmethod
    def _audit(
        session: AsyncSession,
        workspace_id: UUID,
        actor_user_id: UUID | None,
        actor_username: str,
        event_type: str,
        target_type: str | None,
        target_id: str | None,
        details: dict[str, object],
        now: datetime,
    ) -> None:
        session.add(
            WorkspaceAuditEventRecord(
                id=uuid4(),
                workspace_id=workspace_id,
                actor_user_id=actor_user_id,
                actor_username=actor_username,
                event_type=event_type,
                target_type=target_type,
                target_id=target_id,
                details=details,
                created_at=now,
            )
        )
