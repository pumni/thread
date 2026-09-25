from dataclasses import dataclass
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from threads_platform.domain.browser_media import (
    BROWSER_MEDIA_EXTENSIONS,
    MAX_BROWSER_UPLOAD_BYTES,
    BrowserMediaKind,
)
from threads_platform.domain.capabilities import (
    BusinessCapabilityPolicy,
    CapabilityExecutionClass,
    CapabilityExecutor,
    OperationClass,
)
from threads_platform.domain.workers import BrowserSessionState


class BrowserCapabilityStatus(StrEnum):
    BLOCKED_UI_EVIDENCE = "BLOCKED_UI_EVIDENCE"


class BrowserFeedItemResultV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    thread_ref: str | None = Field(default=None, max_length=255)
    author_username: str | None = Field(default=None, max_length=255)
    text_excerpt: str | None = Field(default=None, max_length=500)
    position: int = Field(ge=0, le=19)


class BrowserFeedResultV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    result_version: Literal[1] = 1
    observations: tuple[BrowserFeedItemResultV1, ...] = Field(max_length=20)
    truncated: bool


class BrowserTargetOpenResultV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    result_version: Literal[1] = 1
    target_kind: Literal["THREAD", "PROFILE"]
    target_ref: str = Field(min_length=1, max_length=255)
    recognized: bool


class BrowserMediaStageResultV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    result_version: Literal[1] = 1
    media_kind: BrowserMediaKind
    byte_size: int = Field(ge=1, le=MAX_BROWSER_UPLOAD_BYTES)
    staged: Literal[True] = True


@dataclass(frozen=True, slots=True)
class BrowserCapabilityContract:
    name: str
    version: int
    outcome: str
    operation_class: OperationClass
    required_session_state: BrowserSessionState
    ui_contract_id: str
    ui_contract_version: int
    preemptible: bool
    safe_checkpoints: tuple[str, ...]
    result_schema: str
    result_schema_version: int
    allowed_failure_codes: frozenset[str]
    intervention_types: frozenset[str]
    irreversible_boundary: bool
    status: BrowserCapabilityStatus
    blocked_reason_code: str
    max_items: int | None = None
    max_scroll_iterations: int | None = None
    max_duration_seconds: int | None = None
    max_upload_bytes: int | None = None
    allowed_upload_extensions: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if (
            not self.name.startswith("threads.browser.")
            or self.version < 1
            or not self.outcome.strip()
            or not self.ui_contract_id.strip()
            or self.ui_contract_version < 1
            or not self.result_schema.strip()
            or self.result_schema_version < 1
            or not self.blocked_reason_code.strip()
        ):
            raise ValueError("browser capability contract fields are required")
        if not self.safe_checkpoints or len(set(self.safe_checkpoints)) != len(
            self.safe_checkpoints
        ):
            raise ValueError("browser capability checkpoints must be nonempty and unique")
        if self.status is BrowserCapabilityStatus.BLOCKED_UI_EVIDENCE and (
            self.blocked_reason_code != "BROWSER_UI_EVIDENCE_REQUIRED"
        ):
            raise ValueError("UI evidence blocked capabilities require the matching reason")
        if self.max_items is not None and self.max_items < 1:
            raise ValueError("browser capability item bound must be positive")
        if self.max_scroll_iterations is not None and self.max_scroll_iterations < 1:
            raise ValueError("browser capability scroll bound must be positive")
        if self.max_duration_seconds is not None and self.max_duration_seconds < 1:
            raise ValueError("browser capability duration bound must be positive")
        if self.max_upload_bytes is not None and self.max_upload_bytes < 1:
            raise ValueError("browser capability upload bound must be positive")


COMMON_FAILURES = frozenset(
    {
        "BROWSER_CONTRACT_MISMATCH",
        "BROWSER_REQUIRED_MARKER_NOT_FOUND",
        "UNSUPPORTED_UI_STATE",
        "BROWSER_NAVIGATION_TIMEOUT",
        "BROWSER_PROCESS_CRASHED",
        "WORKER_JOB_LEASE_LOST",
    }
)
SESSION_INTERVENTIONS = frozenset({"LOGIN_REQUIRED", "SESSION_EXPIRED", "CHALLENGE_REQUIRED"})


BROWSER_CAPABILITY_CONTRACTS: tuple[BrowserCapabilityContract, ...] = (
    BrowserCapabilityContract(
        name="threads.browser.feed.browse",
        version=1,
        outcome="Bounded normalized observations from the authenticated account feed.",
        operation_class=OperationClass.READ,
        required_session_state=BrowserSessionState.AUTHENTICATED,
        ui_contract_id="threads.browser.feed",
        ui_contract_version=1,
        preemptible=True,
        safe_checkpoints=("BEFORE_NAVIGATION", "FEED_READY", "ITEM_BATCH"),
        result_schema="BrowserFeedResultV1",
        result_schema_version=1,
        allowed_failure_codes=COMMON_FAILURES,
        intervention_types=SESSION_INTERVENTIONS,
        irreversible_boundary=False,
        status=BrowserCapabilityStatus.BLOCKED_UI_EVIDENCE,
        blocked_reason_code="BROWSER_UI_EVIDENCE_REQUIRED",
        max_items=20,
        max_scroll_iterations=5,
        max_duration_seconds=30,
    ),
    BrowserCapabilityContract(
        name="threads.browser.thread.open",
        version=1,
        outcome="Confirm one explicitly identified Thread and return bounded evidence.",
        operation_class=OperationClass.READ,
        required_session_state=BrowserSessionState.AUTHENTICATED,
        ui_contract_id="threads.browser.thread",
        ui_contract_version=1,
        preemptible=True,
        safe_checkpoints=("BEFORE_NAVIGATION", "THREAD_READY"),
        result_schema="BrowserTargetOpenResultV1",
        result_schema_version=1,
        allowed_failure_codes=COMMON_FAILURES,
        intervention_types=SESSION_INTERVENTIONS,
        irreversible_boundary=False,
        status=BrowserCapabilityStatus.BLOCKED_UI_EVIDENCE,
        blocked_reason_code="BROWSER_UI_EVIDENCE_REQUIRED",
    ),
    BrowserCapabilityContract(
        name="threads.browser.profile.open",
        version=1,
        outcome="Confirm one explicitly identified profile and return bounded evidence.",
        operation_class=OperationClass.READ,
        required_session_state=BrowserSessionState.AUTHENTICATED,
        ui_contract_id="threads.browser.profile",
        ui_contract_version=1,
        preemptible=True,
        safe_checkpoints=("BEFORE_NAVIGATION", "PROFILE_READY"),
        result_schema="BrowserTargetOpenResultV1",
        result_schema_version=1,
        allowed_failure_codes=COMMON_FAILURES,
        intervention_types=SESSION_INTERVENTIONS,
        irreversible_boundary=False,
        status=BrowserCapabilityStatus.BLOCKED_UI_EVIDENCE,
        blocked_reason_code="BROWSER_UI_EVIDENCE_REQUIRED",
    ),
    BrowserCapabilityContract(
        name="threads.browser.media.local_upload",
        version=1,
        outcome="Stage one approved local media file in browser state without submitting.",
        operation_class=OperationClass.MUTATION,
        required_session_state=BrowserSessionState.AUTHENTICATED,
        ui_contract_id="threads.browser.media.local_upload",
        ui_contract_version=1,
        preemptible=True,
        safe_checkpoints=("BEFORE_LOCAL_STAGE", "LOCAL_STAGE_COMPLETE"),
        result_schema="BrowserMediaStageResultV1",
        result_schema_version=1,
        allowed_failure_codes=COMMON_FAILURES | {"MEDIA_FILE_REJECTED", "MEDIA_UPLOAD_FAILED"},
        intervention_types=SESSION_INTERVENTIONS | {"REMOTE_STATE_UNCERTAIN"},
        irreversible_boundary=False,
        status=BrowserCapabilityStatus.BLOCKED_UI_EVIDENCE,
        blocked_reason_code="BROWSER_UI_EVIDENCE_REQUIRED",
        max_upload_bytes=MAX_BROWSER_UPLOAD_BYTES,
        allowed_upload_extensions=frozenset(BROWSER_MEDIA_EXTENSIONS),
    ),
)


def browser_capability_policies() -> tuple[BusinessCapabilityPolicy, ...]:
    return tuple(
        BusinessCapabilityPolicy(
            command_type=contract.name,
            capability_name=contract.name,
            capability_version=contract.version,
            execution_class=CapabilityExecutionClass.BROWSER_ASSISTED,
            operation_class=contract.operation_class,
            preferred_executor=CapabilityExecutor.WORKER,
            worker_capability_name=contract.name,
            worker_capability_version=contract.version,
            blocked_reason_code=contract.blocked_reason_code,
        )
        for contract in BROWSER_CAPABILITY_CONTRACTS
    )
