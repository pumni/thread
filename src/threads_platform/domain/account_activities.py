import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from enum import StrEnum
from types import MappingProxyType
from typing import cast
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from threads_platform.domain.time import normalize_utc, utc_now

MAX_ACTIVITY_CONFIGURATION_BYTES = 16 * 1024
MAX_ACTIVITY_NAME_LENGTH = 120
MAX_ACTIVITY_REASON_LENGTH = 240

type _JsonScalar = str | int | float | bool | None
type _JsonValue = _JsonScalar | list[_JsonValue] | dict[str, _JsonValue]
type _FrozenJsonValue = _JsonScalar | tuple[_FrozenJsonValue, ...] | Mapping[str, _FrozenJsonValue]


class AccountActivityPlanStatus(StrEnum):
    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"
    DISABLED = "DISABLED"


class ActivityPriority(StrEnum):
    LOW = "LOW"
    NORMAL = "NORMAL"
    HIGH = "HIGH"

    @property
    def worker_job_priority(self) -> int:
        return {
            ActivityPriority.LOW: -100,
            ActivityPriority.NORMAL: 0,
            ActivityPriority.HIGH: 100,
        }[self]

    @classmethod
    def from_worker_job_priority(cls, priority: int) -> ActivityPriority:
        if type(priority) is not int:
            raise ValueError("WorkerJob priority must be an integer")
        try:
            return {-100: cls.LOW, 0: cls.NORMAL, 100: cls.HIGH}[priority]
        except KeyError as error:
            raise ValueError(f"unsupported WorkerJob activity priority: {priority}") from error


class ActivityRecurrenceKind(StrEnum):
    NONE = "NONE"
    FIXED_INTERVAL = "FIXED_INTERVAL"


MIN_ACTIVITY_RECURRENCE_INTERVAL_SECONDS = 900
MAX_ACTIVITY_RECURRENCE_INTERVAL_SECONDS = 2_592_000
RECURRING_ACTIVITY_TYPES = frozenset(
    {
        "threads.browser.feed.browse",
        "threads.browser.thread.open",
        "threads.browser.profile.open",
    }
)


class ScheduledActivityMaterializationStatus(StrEnum):
    PENDING = "PENDING"
    MATERIALIZED = "MATERIALIZED"
    NON_MATERIALIZABLE = "NON_MATERIALIZABLE"


_PLAN_TRANSITIONS: frozenset[tuple[AccountActivityPlanStatus, AccountActivityPlanStatus]] = (
    frozenset(
        {
            (AccountActivityPlanStatus.ACTIVE, AccountActivityPlanStatus.PAUSED),
            (AccountActivityPlanStatus.ACTIVE, AccountActivityPlanStatus.DISABLED),
            (AccountActivityPlanStatus.PAUSED, AccountActivityPlanStatus.ACTIVE),
            (AccountActivityPlanStatus.PAUSED, AccountActivityPlanStatus.DISABLED),
        }
    )
)


@dataclass(slots=True)
class AccountActivityPlan:
    account_id: UUID
    name: str
    id: UUID = field(default_factory=uuid4)
    status: AccountActivityPlanStatus = AccountActivityPlanStatus.ACTIVE
    revision: int = 1
    status_reason: str | None = None
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        self.name = _bounded_text(self.name, "plan name", MAX_ACTIVITY_NAME_LENGTH)
        self.status = AccountActivityPlanStatus(self.status)
        if self.revision < 1:
            raise ValueError("activity plan revision must be positive")
        if self.status_reason is not None:
            self.status_reason = _bounded_text(
                self.status_reason, "plan status reason", MAX_ACTIVITY_REASON_LENGTH
            )
        if self.revision > 1 and self.status_reason is None:
            raise ValueError("revised activity plan requires a reason")
        if self.status is not AccountActivityPlanStatus.ACTIVE and self.status_reason is None:
            raise ValueError("paused or disabled activity plan requires a reason")
        self.created_at = normalize_utc(self.created_at)
        self.updated_at = normalize_utc(self.updated_at)
        if self.updated_at < self.created_at:
            raise ValueError("activity plan updated_at cannot precede created_at")

    def transition(
        self,
        target: AccountActivityPlanStatus,
        at: datetime,
        *,
        reason: str,
    ) -> None:
        target = AccountActivityPlanStatus(target)
        if (self.status, target) not in _PLAN_TRANSITIONS:
            raise ValueError(f"invalid activity plan transition: {self.status} -> {target}")
        self._revise(at, reason)
        self.status = target

    def rename(self, name: str, at: datetime, *, reason: str) -> None:
        if self.status is AccountActivityPlanStatus.DISABLED:
            raise ValueError("disabled activity plan is terminal")
        updated_name = _bounded_text(name, "plan name", MAX_ACTIVITY_NAME_LENGTH)
        if updated_name == self.name:
            raise ValueError("activity plan name is unchanged")
        self._revise(at, reason)
        self.name = updated_name

    def _revise(self, at: datetime, reason: str) -> None:
        occurred_at = normalize_utc(at)
        if occurred_at < self.updated_at:
            raise ValueError("activity plan revision time cannot move backwards")
        self.status_reason = _bounded_text(reason, "plan status reason", MAX_ACTIVITY_REASON_LENGTH)
        self.revision += 1
        self.updated_at = occurred_at


@dataclass(frozen=True, slots=True)
class AccountActivityTemplate:
    account_id: UUID
    plan_id: UUID
    name: str
    activity_type: str
    configuration: Mapping[str, object]
    priority: ActivityPriority
    change_reason: str
    id: UUID = field(default_factory=uuid4)
    revision: int = 1
    created_at: datetime = field(default_factory=utc_now)
    recurrence_kind: ActivityRecurrenceKind = ActivityRecurrenceKind.NONE
    anchor_at: datetime | None = None
    interval_seconds: int | None = None

    def __post_init__(self) -> None:
        if self.revision < 1:
            raise ValueError("activity template revision must be positive")
        object.__setattr__(
            self, "name", _bounded_text(self.name, "template name", MAX_ACTIVITY_NAME_LENGTH)
        )
        object.__setattr__(
            self,
            "activity_type",
            _bounded_text(self.activity_type, "activity type", MAX_ACTIVITY_NAME_LENGTH),
        )
        object.__setattr__(self, "priority", ActivityPriority(self.priority))
        object.__setattr__(
            self,
            "change_reason",
            _bounded_text(self.change_reason, "template change reason", MAX_ACTIVITY_REASON_LENGTH),
        )
        object.__setattr__(self, "configuration", _freeze_configuration(self.configuration))
        object.__setattr__(self, "created_at", normalize_utc(self.created_at))
        recurrence_kind = ActivityRecurrenceKind(self.recurrence_kind)
        object.__setattr__(self, "recurrence_kind", recurrence_kind)
        if recurrence_kind is ActivityRecurrenceKind.NONE:
            if self.anchor_at is not None or self.interval_seconds is not None:
                raise ValueError("NONE recurrence cannot have an anchor or interval")
            return

        if self.activity_type not in RECURRING_ACTIVITY_TYPES:
            raise ValueError("fixed interval recurrence is not allowed for this activity type")
        if self.anchor_at is None:
            raise ValueError("fixed interval recurrence requires an anchor")
        anchor_at = normalize_utc(self.anchor_at)
        object.__setattr__(self, "anchor_at", anchor_at)
        if anchor_at < self.created_at:
            raise ValueError("recurrence anchor cannot precede template revision creation")
        if (
            type(self.interval_seconds) is not int
            or not MIN_ACTIVITY_RECURRENCE_INTERVAL_SECONDS
            <= self.interval_seconds
            <= MAX_ACTIVITY_RECURRENCE_INTERVAL_SECONDS
        ):
            raise ValueError(
                "fixed interval recurrence must be between "
                f"{MIN_ACTIVITY_RECURRENCE_INTERVAL_SECONDS} and "
                f"{MAX_ACTIVITY_RECURRENCE_INTERVAL_SECONDS} seconds"
            )


@dataclass(frozen=True, slots=True)
class ScheduledActivity:
    account_id: UUID
    plan_id: UUID
    plan_revision: int
    plan_name_snapshot: str
    plan_status_snapshot: AccountActivityPlanStatus
    plan_status_reason_snapshot: str | None
    template_id: UUID
    template_revision: int
    template_name_snapshot: str
    activity_type_snapshot: str
    configuration_snapshot: Mapping[str, object]
    priority: ActivityPriority
    due_at: datetime
    id: UUID = field(default_factory=uuid4)
    creation_reason: str | None = None
    created_at: datetime = field(default_factory=utc_now)
    materialization_status: ScheduledActivityMaterializationStatus = (
        ScheduledActivityMaterializationStatus.PENDING
    )
    command_id: str | None = None
    materialization_at: datetime | None = None
    materialization_reason: str | None = None

    def __post_init__(self) -> None:
        if self.plan_revision < 1 or self.template_revision < 1:
            raise ValueError("scheduled activity plan and template revisions must be positive")
        object.__setattr__(
            self,
            "plan_name_snapshot",
            _bounded_text(self.plan_name_snapshot, "plan name snapshot", MAX_ACTIVITY_NAME_LENGTH),
        )
        object.__setattr__(
            self, "plan_status_snapshot", AccountActivityPlanStatus(self.plan_status_snapshot)
        )
        if self.plan_status_reason_snapshot is not None:
            object.__setattr__(
                self,
                "plan_status_reason_snapshot",
                _bounded_text(
                    self.plan_status_reason_snapshot,
                    "plan status reason snapshot",
                    MAX_ACTIVITY_REASON_LENGTH,
                ),
            )
        object.__setattr__(
            self,
            "template_name_snapshot",
            _bounded_text(
                self.template_name_snapshot, "template name snapshot", MAX_ACTIVITY_NAME_LENGTH
            ),
        )
        object.__setattr__(
            self,
            "activity_type_snapshot",
            _bounded_text(
                self.activity_type_snapshot, "activity type snapshot", MAX_ACTIVITY_NAME_LENGTH
            ),
        )
        object.__setattr__(
            self, "configuration_snapshot", _freeze_configuration(self.configuration_snapshot)
        )
        object.__setattr__(self, "priority", ActivityPriority(self.priority))
        object.__setattr__(self, "due_at", normalize_utc(self.due_at))
        object.__setattr__(self, "created_at", normalize_utc(self.created_at))
        if self.creation_reason is not None:
            object.__setattr__(
                self,
                "creation_reason",
                _bounded_text(
                    self.creation_reason, "activity creation reason", MAX_ACTIVITY_REASON_LENGTH
                ),
            )
        object.__setattr__(
            self,
            "materialization_status",
            ScheduledActivityMaterializationStatus(self.materialization_status),
        )
        if self.command_id is not None:
            object.__setattr__(
                self,
                "command_id",
                _bounded_text(self.command_id, "materialized command id", 255),
            )
        if self.materialization_at is not None:
            object.__setattr__(self, "materialization_at", normalize_utc(self.materialization_at))
        if self.materialization_reason is not None:
            object.__setattr__(
                self,
                "materialization_reason",
                _bounded_text(
                    self.materialization_reason,
                    "materialization reason",
                    MAX_ACTIVITY_REASON_LENGTH,
                ),
            )
        if self.materialization_status is ScheduledActivityMaterializationStatus.PENDING:
            if any(
                value is not None
                for value in (self.command_id, self.materialization_at, self.materialization_reason)
            ):
                raise ValueError("pending scheduled activity cannot have a materialization outcome")
        elif self.materialization_status is ScheduledActivityMaterializationStatus.MATERIALIZED:
            if self.command_id is None or self.materialization_at is None:
                raise ValueError(
                    "materialized scheduled activity requires command id and timestamp"
                )
            if self.materialization_reason is not None:
                raise ValueError(
                    "materialized scheduled activity cannot have a materialization reason"
                )
        elif (
            self.command_id is not None
            or self.materialization_at is None
            or self.materialization_reason is None
        ):
            raise ValueError(
                "non-materializable scheduled activity requires timestamp and reason only"
            )

    def mark_materialized(self, command_id: str, at: datetime) -> ScheduledActivity:
        if self.materialization_status is not ScheduledActivityMaterializationStatus.PENDING:
            raise ValueError("scheduled activity is no longer pending materialization")
        return replace(
            self,
            command_id=_bounded_text(command_id, "materialized command id", 255),
            materialization_at=normalize_utc(at),
            materialization_status=ScheduledActivityMaterializationStatus.MATERIALIZED,
        )

    def mark_non_materializable(self, reason: str, at: datetime) -> ScheduledActivity:
        if self.materialization_status is not ScheduledActivityMaterializationStatus.PENDING:
            raise ValueError("scheduled activity is no longer pending materialization")
        return replace(
            self,
            materialization_at=normalize_utc(at),
            materialization_reason=_bounded_text(
                reason, "materialization reason", MAX_ACTIVITY_REASON_LENGTH
            ),
            materialization_status=ScheduledActivityMaterializationStatus.NON_MATERIALIZABLE,
        )

    @classmethod
    def from_plan_template(
        cls,
        plan: AccountActivityPlan,
        template: AccountActivityTemplate,
        due_at: datetime,
        *,
        creation_reason: str | None = None,
        id: UUID | None = None,
        created_at: datetime | None = None,
    ) -> ScheduledActivity:
        if template.account_id != plan.account_id or template.plan_id != plan.id:
            raise ValueError("activity template must belong to the scheduled activity plan account")
        return cls(
            id=id or uuid4(),
            account_id=plan.account_id,
            plan_id=plan.id,
            plan_revision=plan.revision,
            plan_name_snapshot=plan.name,
            plan_status_snapshot=plan.status,
            plan_status_reason_snapshot=plan.status_reason,
            template_id=template.id,
            template_revision=template.revision,
            template_name_snapshot=template.name,
            activity_type_snapshot=template.activity_type,
            configuration_snapshot=template.configuration,
            priority=template.priority,
            due_at=due_at,
            creation_reason=creation_reason,
            created_at=created_at or utc_now(),
        )

    @property
    def identity(self) -> tuple[UUID, int, datetime]:
        return self.template_id, self.template_revision, self.due_at


@dataclass(slots=True)
class AccountActivityRecurrenceState:
    template_id: UUID
    template_revision: int
    next_due_at: datetime
    last_generated_due_at: datetime | None = None
    generated_count: int = 0
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        if self.template_revision < 1:
            raise ValueError("recurrence template revision must be positive")
        if type(self.generated_count) is not int or self.generated_count < 0:
            raise ValueError("recurrence generated count must be a non-negative integer")
        self.next_due_at = normalize_utc(self.next_due_at)
        if self.last_generated_due_at is not None:
            self.last_generated_due_at = normalize_utc(self.last_generated_due_at)
        self.created_at = normalize_utc(self.created_at)
        self.updated_at = normalize_utc(self.updated_at)
        if self.updated_at < self.created_at:
            raise ValueError("recurrence state updated_at cannot precede created_at")
        if (self.generated_count == 0) != (self.last_generated_due_at is None):
            raise ValueError("recurrence cursor count and last generated time disagree")
        if (
            self.last_generated_due_at is not None
            and self.next_due_at <= self.last_generated_due_at
        ):
            raise ValueError("recurrence next due time must follow the last generated time")

    def advance(self, interval_seconds: int, *, at: datetime) -> datetime:
        if (
            type(interval_seconds) is not int
            or not MIN_ACTIVITY_RECURRENCE_INTERVAL_SECONDS
            <= interval_seconds
            <= MAX_ACTIVITY_RECURRENCE_INTERVAL_SECONDS
        ):
            raise ValueError("recurrence interval is outside supported bounds")
        due_at = self.next_due_at
        self.last_generated_due_at = due_at
        self.next_due_at = due_at + timedelta(seconds=interval_seconds)
        self.generated_count += 1
        self.updated_at = max(self.updated_at, normalize_utc(at))
        return due_at


def configuration_document(configuration: Mapping[str, object]) -> dict[str, object]:
    """Return a JSON-compatible copy suitable for persistence."""
    return cast(dict[str, object], _thaw_json(cast(_FrozenJsonValue, configuration)))


def _bounded_text(value: str, label: str, limit: int) -> str:
    normalized = value.strip()
    if not normalized or len(normalized) > limit:
        raise ValueError(f"{label} must contain 1 to {limit} characters")
    return normalized


def _freeze_configuration(configuration: object) -> Mapping[str, object]:
    if not isinstance(configuration, Mapping):
        raise ValueError("activity configuration must be a JSON object")
    copied = _copy_json(cast(Mapping[object, object], configuration))
    if not isinstance(copied, dict):
        raise ValueError("activity configuration must be a JSON object")
    try:
        encoded = json.dumps(copied, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise ValueError("activity configuration must be JSON serializable") from error
    if len(encoded) > MAX_ACTIVITY_CONFIGURATION_BYTES:
        raise ValueError("activity configuration exceeds 16 KiB")
    _reject_sensitive_configuration(copied)
    return cast(Mapping[str, object], _freeze_json(copied))


def _copy_json(value: object) -> _JsonValue:
    if isinstance(value, Mapping):
        copied: dict[str, _JsonValue] = {}
        for key, item in cast(Mapping[object, object], value).items():
            if not isinstance(key, str):
                raise ValueError("activity configuration keys must be strings")
            copied[key] = _copy_json(item)
        return copied
    if isinstance(value, (list, tuple)):
        return [_copy_json(item) for item in cast(tuple[object, ...] | list[object], value)]
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    raise ValueError("activity configuration contains a non-JSON value")


def _freeze_json(value: _JsonValue) -> _FrozenJsonValue:
    if isinstance(value, dict):
        frozen = {key: _freeze_json(item) for key, item in value.items()}
        return MappingProxyType(frozen)
    if isinstance(value, list):
        return tuple(_freeze_json(item) for item in value)
    return value


def _thaw_json(value: _FrozenJsonValue) -> _JsonValue:
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


def _reject_sensitive_configuration(value: _JsonValue) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            normalized_key = re.sub(r"[^a-z]", "", key.lower())
            if any(
                term in normalized_key
                for term in (
                    "password",
                    "token",
                    "secret",
                    "credential",
                    "authorization",
                    "privatekey",
                    "proxy",
                )
            ):
                raise ValueError("activity configuration cannot contain secret-bearing fields")
            _reject_sensitive_configuration(item)
    elif isinstance(value, list):
        for item in value:
            _reject_sensitive_configuration(item)
    elif isinstance(value, str):
        parsed = urlsplit(value)
        if parsed.scheme.lower() in {"http", "https", "socks5", "socks5h"} and (
            parsed.username is not None or parsed.password is not None
        ):
            raise ValueError("activity configuration cannot contain URLs with credentials")
