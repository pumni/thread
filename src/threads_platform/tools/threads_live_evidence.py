"""Strict offline validation and opaque-value fingerprinting for TP-002 evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import timedelta
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, BinaryIO, NoReturn, TextIO, cast
from urllib.parse import parse_qsl, urlsplit

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    field_validator,
    model_validator,
)

_MAX_PACKET_BYTES = 1_000_000
_MAX_DIAGNOSTICS = 50
_OFFICIAL_SOURCE_HOSTS = frozenset(
    {"developers.facebook.com", "developers.meta.com", "postman.com", "www.postman.com"}
)
_SAFE_FIELD_NAMES = frozenset(
    {
        "schema_version",
        "packet_classification",
        "observed_at_utc",
        "operator_alias",
        "reviewer_alias",
        "environment_classification",
        "official_source_references_reviewed",
        "api_host",
        "api_version",
        "effective_scope_names",
        "token_metadata",
        "token_class",
        "expires_at_utc",
        "expires_in_seconds",
        "valid",
        "cases",
        "case_id",
        "evidence_class",
        "phase",
        "capability_family",
        "http_method",
        "endpoint_template",
        "request_shape_observation",
        "parameter_names",
        "after_parameter_present",
        "safe_header_observations",
        "name",
        "observation",
        "http_status",
        "response_shape_observation",
        "root_field_names",
        "data_item_field_names",
        "item_count",
        "paging_present",
        "error_object_present",
        "owner_id_available",
        "media_state",
        "cursor_page_observation",
        "after_value_fingerprint",
        "returned_after_fingerprint",
        "terminal_page",
        "repeated_cursor",
        "same_cursor_across_runs",
        "endpoint_limit",
        "scrubbed_error_observation",
        "error_category",
        "error_type_present",
        "error_code_present",
        "error_subcode_present",
        "message_present",
        "is_transient",
        "retry_after_present",
        "result",
        "notes",
        "critical_path_readiness",
        "token_provider_ready_evidence",
        "polling_cursor_ready_evidence",
        "full_tp002_ready",
        "reviewed_at_utc",
        "review_note",
    }
)
_SENSITIVE_FIELD_PARTS = (
    "accesstoken",
    "refreshtoken",
    "appsecret",
    "clientsecret",
    "secret",
    "authorizationcode",
    "oauthcode",
    "tokenvalue",
    "apikey",
    "privatekey",
    "credential",
    "password",
    "accountid",
    "userid",
    "threadid",
    "mediaid",
    "appid",
    "clientid",
    "username",
)
_SECRET_QUERY_NAMES = frozenset(
    {
        "access_token",
        "app_secret",
        "authorization_code",
        "client_secret",
        "client_id",
        "code",
        "app_id",
        "account_id",
        "user_id",
        "oauth_token",
        "password",
        "refresh_token",
        "secret",
        "signature",
        "sig",
        "token",
    }
)
_SAFE_CURSOR_FIELD_NAMES = frozenset(
    {
        "cursorpageobservation",
        "pollingcursorreadyevidence",
        "repeatedcursor",
        "samecursoracrossruns",
    }
)


class PacketClassification(StrEnum):
    TEMPLATE_ONLY_NOT_LIVE_EVIDENCE = "TEMPLATE_ONLY_NOT_LIVE_EVIDENCE"
    SCRUBBED_LIVE_EVIDENCE = "SCRUBBED_LIVE_EVIDENCE"


class EvidenceClass(StrEnum):
    TEMPLATE_ONLY_NOT_LIVE_EVIDENCE = "TEMPLATE_ONLY_NOT_LIVE_EVIDENCE"
    LIVE_SCRUBBED = "LIVE_SCRUBBED"


class EnvironmentClassification(StrEnum):
    NOT_RECORDED = "NOT_RECORDED"
    META_DEVELOPMENT_APP_DEDICATED_TEST_ACCOUNT = "META_DEVELOPMENT_APP_DEDICATED_TEST_ACCOUNT"


class Phase(StrEnum):
    A = "A"
    B = "B"
    C = "C"
    D = "D"


class HttpMethod(StrEnum):
    GET = "GET"
    POST = "POST"


class CaseResult(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    BLOCKED = "BLOCKED"
    NOT_AVAILABLE = "NOT_AVAILABLE"


class HeaderObservation(StrEnum):
    PRESENT = "PRESENT"
    ABSENT = "ABSENT"
    VALUE_REDACTED = "VALUE_REDACTED"


class SafeHeaderName(StrEnum):
    CONTENT_TYPE = "content-type"
    RETRY_AFTER = "retry-after"
    X_APP_USAGE = "x-app-usage"
    X_BUSINESS_USE_CASE_USAGE = "x-business-use-case-usage"
    X_FB_REQUEST_ID = "x-fb-request-id"


class ErrorCategory(StrEnum):
    AUTHENTICATION = "AUTHENTICATION"
    PERMISSION = "PERMISSION"
    VALIDATION = "VALIDATION"
    RATE_LIMIT = "RATE_LIMIT"
    QUOTA = "QUOTA"
    SERVER = "SERVER"
    OTHER = "OTHER"


class MediaState(StrEnum):
    IN_PROGRESS = "IN_PROGRESS"
    PUBLISHED = "PUBLISHED"
    ERROR = "ERROR"
    EXPIRED = "EXPIRED"
    FINISHED = "FINISHED"
    UNKNOWN = "UNKNOWN"


class OAuthTokenClass(StrEnum):
    SHORT_LIVED = "SHORT_LIVED"
    LONG_LIVED = "LONG_LIVED"
    UNKNOWN = "UNKNOWN"


class _MatrixCase:
    __slots__ = ("case_id", "phase", "family", "method", "endpoint")

    case_id: str
    phase: Phase
    family: str
    method: HttpMethod
    endpoint: str

    def __init__(
        self,
        case_id: str,
        phase: Phase,
        family: str,
        method: HttpMethod,
        endpoint: str,
    ) -> None:
        self.case_id = case_id
        self.phase = phase
        self.family = family
        self.method = method
        self.endpoint = endpoint


_CASE_MATRIX = (
    _MatrixCase(
        "A01", Phase.A, "authorization_code_exchange", HttpMethod.POST, "/oauth/access_token"
    ),
    _MatrixCase("A02", Phase.A, "long_lived_token_exchange", HttpMethod.GET, "/access_token"),
    _MatrixCase("A03", Phase.A, "token_refresh", HttpMethod.GET, "/refresh_access_token"),
    _MatrixCase("A04", Phase.A, "token_debugger_effective_scopes", HttpMethod.GET, "/debug_token"),
    _MatrixCase("A05", Phase.A, "token_expiry_validity", HttpMethod.GET, "/debug_token"),
    _MatrixCase("A06", Phase.A, "own_profile", HttpMethod.GET, "/me"),
    _MatrixCase("B01", Phase.B, "keyword_tag_search", HttpMethod.GET, "/keyword_search"),
    _MatrixCase("B02", Phase.B, "public_profile_lookup", HttpMethod.GET, "/profile_lookup"),
    _MatrixCase("B03", Phase.B, "profile_posts", HttpMethod.GET, "/profile_posts"),
    _MatrixCase("B04", Phase.B, "mentions", HttpMethod.GET, "/me/mentions"),
    _MatrixCase("B05", Phase.B, "replies", HttpMethod.GET, "/{thread_id}/replies"),
    _MatrixCase(
        "B06", Phase.B, "flattened_conversation", HttpMethod.GET, "/{thread_id}/conversation"
    ),
    _MatrixCase("B07", Phase.B, "after_request_parameter", HttpMethod.GET, "/profile_posts"),
    _MatrixCase("B08", Phase.B, "paging_after_cursor", HttpMethod.GET, "/keyword_search"),
    _MatrixCase("B09", Phase.B, "terminal_page_behavior", HttpMethod.GET, "/keyword_search"),
    _MatrixCase("B10", Phase.B, "repeated_cursor_behavior", HttpMethod.GET, "/keyword_search"),
    _MatrixCase("B11", Phase.B, "cross_run_cursor_behavior", HttpMethod.GET, "/keyword_search"),
    _MatrixCase("B12", Phase.B, "owner_id_availability", HttpMethod.GET, "/keyword_search"),
    _MatrixCase("B13", Phase.B, "endpoint_limits", HttpMethod.GET, "/keyword_search"),
    _MatrixCase("C01", Phase.C, "text_publish", HttpMethod.POST, "/me/threads"),
    _MatrixCase("C02", Phase.C, "image_publish", HttpMethod.POST, "/me/threads"),
    _MatrixCase("C03", Phase.C, "video_publish", HttpMethod.POST, "/me/threads"),
    _MatrixCase("C04", Phase.C, "published_media_retrieval", HttpMethod.GET, "/{thread_id}"),
    _MatrixCase("C05", Phase.C, "reply", HttpMethod.POST, "/{thread_id}/replies"),
    _MatrixCase("C06", Phase.C, "reply_to_reply", HttpMethod.POST, "/{thread_id}/replies"),
    _MatrixCase("C07", Phase.C, "container_media_state", HttpMethod.GET, "/{container_id}"),
    _MatrixCase("C08", Phase.C, "publishing_quota", HttpMethod.GET, "/me/threads_publishing_limit"),
    _MatrixCase("C09", Phase.C, "moderation_permissions", HttpMethod.GET, "/{thread_id}/replies"),
    _MatrixCase(
        "C10", Phase.C, "ambiguous_publish_reconciliation", HttpMethod.GET, "/{container_id}"
    ),
    _MatrixCase("D01", Phase.D, "authentication_failure", HttpMethod.GET, "/me"),
    _MatrixCase("D02", Phase.D, "permission_failure", HttpMethod.GET, "/me/mentions"),
    _MatrixCase("D03", Phase.D, "validation_failure", HttpMethod.POST, "/me/threads"),
    _MatrixCase(
        "D04", Phase.D, "rate_or_quota_failure", HttpMethod.GET, "/me/threads_publishing_limit"
    ),
    _MatrixCase("D05", Phase.D, "naturally_observed_server_failure", HttpMethod.GET, "/me"),
)
_MATRIX_BY_ID = {case.case_id: case for case in _CASE_MATRIX}
_TOKEN_READINESS_CASES = frozenset({"A01", "A02", "A03", "A04", "A05", "A06"})
_POLLING_READINESS_CASES = frozenset(case.case_id for case in _CASE_MATRIX if case.phase is Phase.B)
_FULL_READINESS_CASES = frozenset(case.case_id for case in _CASE_MATRIX)

Alias = Annotated[
    str,
    StringConstraints(min_length=1, max_length=32, pattern=r"^[A-Za-z][A-Za-z0-9_-]*$"),
]
Fingerprint = Annotated[str, StringConstraints(pattern=r"^sha256:[0-9a-f]{64}$")]
ObservedFieldName = Annotated[
    str,
    StringConstraints(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]*$"),
]
RequestParameterName = Annotated[
    str,
    StringConstraints(min_length=1, max_length=64, pattern=r"^[A-Za-z][A-Za-z0-9_]*$"),
]


class StrictEvidenceModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class TokenMetadata(StrictEvidenceModel):
    token_class: OAuthTokenClass | None = None
    expires_at_utc: AwareDatetime | None = None
    expires_in_seconds: int | None = Field(default=None, ge=0, le=315_576_000)
    valid: bool | None = None

    @field_validator("expires_at_utc")
    @classmethod
    def expiry_is_utc(cls, value: AwareDatetime | None) -> AwareDatetime | None:
        return _require_utc(value)


class RequestShapeObservation(StrictEvidenceModel):
    parameter_names: list[RequestParameterName] = Field(default_factory=list, max_length=40)
    after_parameter_present: bool | None = None


class SafeHeaderObservation(StrictEvidenceModel):
    name: SafeHeaderName
    observation: HeaderObservation


class ResponseShapeObservation(StrictEvidenceModel):
    root_field_names: list[ObservedFieldName] = Field(default_factory=list, max_length=40)
    data_item_field_names: list[ObservedFieldName] = Field(default_factory=list, max_length=40)
    item_count: int | None = Field(default=None, ge=0, le=1_000_000)
    paging_present: bool | None = None
    error_object_present: bool | None = None
    owner_id_available: bool | None = None
    media_state: MediaState | None = None


class CursorPageObservation(StrictEvidenceModel):
    after_parameter_present: bool | None = None
    paging_after_present: bool | None = None
    terminal_page: bool | None = None
    repeated_cursor: bool | None = None
    same_cursor_across_runs: bool | None = None
    endpoint_limit: int | None = Field(default=None, ge=0, le=1_000_000)
    page_item_count: int | None = Field(default=None, ge=0, le=1_000_000)
    after_value_fingerprint: Fingerprint | None = None
    returned_after_fingerprint: Fingerprint | None = None


class ScrubbedErrorObservation(StrictEvidenceModel):
    error_category: ErrorCategory
    error_type_present: bool | None = None
    error_code_present: bool | None = None
    error_subcode_present: bool | None = None
    message_present: bool | None = None
    is_transient: bool | None = None
    retry_after_present: bool | None = None


def _empty_safe_header_observations() -> list[SafeHeaderObservation]:
    return []


class ValidationCase(StrictEvidenceModel):
    case_id: str = Field(pattern=r"^[A-D][0-9]{2}$", max_length=3)
    evidence_class: EvidenceClass
    phase: Phase
    capability_family: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]*$")
    http_method: HttpMethod
    endpoint_template: str = Field(min_length=1, max_length=80)
    request_shape_observation: RequestShapeObservation | None = None
    http_status: int | None = Field(default=None, ge=100, le=599)
    safe_header_observations: list[SafeHeaderObservation] = Field(
        default_factory=_empty_safe_header_observations, max_length=20
    )
    response_shape_observation: ResponseShapeObservation | None = None
    cursor_page_observation: CursorPageObservation | None = None
    scrubbed_error_observation: ScrubbedErrorObservation | None = None
    result: CaseResult
    notes: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def match_canonical_case(self) -> ValidationCase:
        expected = _MATRIX_BY_ID.get(self.case_id)
        if expected is None:
            raise ValueError("case_id is not part of the TP-002 Phase A-D matrix")
        if (
            self.phase != expected.phase
            or self.capability_family != expected.family
            or self.http_method != expected.method
            or self.endpoint_template != expected.endpoint
        ):
            raise ValueError("case shape does not match the canonical endpoint template")
        if self.result in {CaseResult.PASS, CaseResult.FAIL} and self.http_status is None:
            raise ValueError("PASS and FAIL cases require an observed HTTP status")
        return self


class CriticalPathReadiness(StrictEvidenceModel):
    token_provider_ready_evidence: bool = False
    polling_cursor_ready_evidence: bool = False
    full_tp002_ready: bool = False
    reviewed_at_utc: AwareDatetime | None = None
    review_note: str | None = Field(default=None, max_length=500)

    @field_validator("reviewed_at_utc")
    @classmethod
    def review_time_is_utc(cls, value: AwareDatetime | None) -> AwareDatetime | None:
        return _require_utc(value)


class ThreadsLiveEvidencePacket(StrictEvidenceModel):
    schema_version: str = Field(pattern=r"^threads-live-evidence-v1$")
    packet_classification: PacketClassification
    observed_at_utc: AwareDatetime | None
    operator_alias: Alias
    reviewer_alias: Alias | None = None
    environment_classification: EnvironmentClassification
    official_source_references_reviewed: list[str] = Field(default_factory=list, max_length=20)
    api_host: str | None = None
    api_version: str | None = None
    effective_scope_names: list[str] = Field(default_factory=list, max_length=40)
    token_metadata: TokenMetadata
    cases: list[ValidationCase] = Field(min_length=len(_CASE_MATRIX), max_length=len(_CASE_MATRIX))
    critical_path_readiness: CriticalPathReadiness

    @field_validator("observed_at_utc")
    @classmethod
    def observed_time_is_utc(cls, value: AwareDatetime | None) -> AwareDatetime | None:
        return _require_utc(value)

    @field_validator("official_source_references_reviewed")
    @classmethod
    def source_references_are_safe(cls, values: list[str]) -> list[str]:
        for value in values:
            _validate_official_source_url(value)
        return values

    @field_validator("api_host")
    @classmethod
    def api_host_is_safe(cls, value: str | None) -> str | None:
        if value is not None and value != "graph.threads.net":
            raise ValueError("api_host must be a safe host name")
        return value

    @field_validator("api_version")
    @classmethod
    def api_version_is_safe(cls, value: str | None) -> str | None:
        if value is not None and re.fullmatch(r"v[0-9]+\.[0-9]+", value) is None:
            raise ValueError("api_version must use a version label")
        return value

    @field_validator("effective_scope_names")
    @classmethod
    def scopes_are_names_only(cls, values: list[str]) -> list[str]:
        if any(re.fullmatch(r"threads_[a-z_]+", value) is None for value in values):
            raise ValueError("effective scopes must be Threads scope names")
        return values

    @model_validator(mode="after")
    def packet_consistency(self) -> ThreadsLiveEvidencePacket:
        case_ids = [case.case_id for case in self.cases]
        if case_ids != [case.case_id for case in _CASE_MATRIX]:
            raise ValueError("cases must contain the complete Phase A-D matrix once and in order")

        readiness = self.critical_path_readiness
        readiness_claimed = any(
            (
                readiness.token_provider_ready_evidence,
                readiness.polling_cursor_ready_evidence,
                readiness.full_tp002_ready,
            )
        )
        if self.packet_classification is PacketClassification.TEMPLATE_ONLY_NOT_LIVE_EVIDENCE:
            if (
                self.observed_at_utc is not None
                or self.operator_alias != "TEMPLATE"
                or self.reviewer_alias is not None
                or self.environment_classification is not EnvironmentClassification.NOT_RECORDED
                or self.api_host is not None
                or self.api_version is not None
                or self.effective_scope_names
                or self.official_source_references_reviewed
                or any(
                    value is not None
                    for value in (
                        self.token_metadata.token_class,
                        self.token_metadata.expires_at_utc,
                        self.token_metadata.expires_in_seconds,
                        self.token_metadata.valid,
                    )
                )
                or readiness_claimed
                or readiness.reviewed_at_utc is not None
                or readiness.review_note is not None
                or any(
                    case.evidence_class is not EvidenceClass.TEMPLATE_ONLY_NOT_LIVE_EVIDENCE
                    or case.result is not CaseResult.NOT_AVAILABLE
                    or case.http_status is not None
                    or case.request_shape_observation is not None
                    or case.safe_header_observations
                    or case.response_shape_observation is not None
                    or case.cursor_page_observation is not None
                    or case.scrubbed_error_observation is not None
                    or case.notes is not None
                    for case in self.cases
                )
            ):
                raise ValueError("template classification cannot contain live observations")
            return self

        if (
            self.observed_at_utc is None
            or self.operator_alias == "TEMPLATE"
            or self.reviewer_alias is None
            or self.environment_classification
            is not EnvironmentClassification.META_DEVELOPMENT_APP_DEDICATED_TEST_ACCOUNT
            or not self.official_source_references_reviewed
            or any(case.evidence_class is not EvidenceClass.LIVE_SCRUBBED for case in self.cases)
        ):
            raise ValueError(
                "live evidence requires UTC observation, aliases, source references, "
                "and a dedicated test account"
            )

        if not any(
            case.http_status is not None
            or case.request_shape_observation is not None
            or case.safe_header_observations
            or case.response_shape_observation is not None
            or case.cursor_page_observation is not None
            or case.scrubbed_error_observation is not None
            or case.notes is not None
            for case in self.cases
        ):
            raise ValueError("live evidence packet requires at least one scrubbed observation")

        if readiness_claimed and (
            readiness.reviewed_at_utc is None
            or readiness.review_note is None
            or not readiness.review_note.strip()
        ):
            raise ValueError(
                "readiness classifications require a reviewer, review time and rationale"
            )

        results = {case.case_id: case.result for case in self.cases}
        if readiness.token_provider_ready_evidence and not _cases_pass(
            results, _TOKEN_READINESS_CASES
        ):
            raise ValueError("token-provider readiness requires all Phase A critical cases to pass")
        if readiness.polling_cursor_ready_evidence and not _cases_pass(
            results, _POLLING_READINESS_CASES
        ):
            raise ValueError("polling readiness requires all Phase B critical cases to pass")
        if readiness.full_tp002_ready and (
            not readiness.token_provider_ready_evidence
            or not readiness.polling_cursor_ready_evidence
            or not _cases_pass(results, _FULL_READINESS_CASES)
        ):
            raise ValueError(
                "full TP-002 readiness requires reviewed Phase A-D critical cases to pass"
            )
        return self


class PacketRejectedError(Exception):
    """A safe-to-display packet rejection with no captured input values."""

    def __init__(self, issues: list[tuple[str, str]]) -> None:
        self.issues = tuple(issues[:_MAX_DIAGNOSTICS])
        super().__init__("evidence packet rejected")


class _DuplicateJSONKeyError(Exception):
    pass


class _SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        del message
        print(
            "invalid command arguments; sensitive values must be supplied only on standard input",
            file=sys.stderr,
        )
        self.print_usage(file=sys.stderr)
        raise SystemExit(2)


def _require_utc(value: AwareDatetime | None) -> AwareDatetime | None:
    if value is not None and value.utcoffset() != timedelta(0):
        raise ValueError("timestamp must be UTC")
    return value


def _cases_pass(results: dict[str, CaseResult], case_ids: frozenset[str]) -> bool:
    return all(results.get(case_id) is CaseResult.PASS for case_id in case_ids)


def _normalise_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.casefold())


def _sensitive_query_name(value: str) -> bool:
    normalised = value.casefold().replace("-", "_")
    compact = _normalise_name(value)
    return (
        normalised in _SECRET_QUERY_NAMES
        or any(
            part in compact
            for part in (
                "token",
                "secret",
                "authorization",
                "password",
                "signature",
                "clientid",
                "appid",
                "accountid",
                "userid",
                "threadid",
                "mediaid",
            )
        )
        or compact.endswith("code")
    )


def _validate_official_source_url(value: str) -> None:
    try:
        parts = urlsplit(value)
        hostname = parts.hostname
        query_pairs = parse_qsl(parts.query, keep_blank_values=True)
    except ValueError as exc:
        raise ValueError("source reference is not a valid URL") from exc
    if (
        parts.scheme != "https"
        or hostname is None
        or hostname.casefold() not in _OFFICIAL_SOURCE_HOSTS
        or parts.username is not None
        or parts.password is not None
        or any(_sensitive_query_name(name) for name, _ in query_pairs)
    ):
        raise ValueError("source reference must be a safe HTTPS official-source URL")


def _safe_location(path: tuple[str | int, ...]) -> str:
    result = "packet"
    for part in path:
        if isinstance(part, int):
            result += f"[{part}]"
        elif part in _SAFE_FIELD_NAMES:
            result += f".{part}"
        else:
            result += ".<field>"
    return result


def _duplicate_rejecting_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJSONKeyError
        result[key] = value
    return result


def _reject_json_constant(_: str) -> Any:
    raise ValueError("non-standard JSON constant")


def _scan_secret_content(document: Any) -> list[tuple[str, str]]:
    issues: list[tuple[str, str]] = []
    bearer_pattern = re.compile(r"\bbearer\s+\S+", re.IGNORECASE)
    threads_token_pattern = re.compile(r"\bTH\|[^\s,;\]\[{}\"']+", re.IGNORECASE)
    secret_assignment_pattern = re.compile(
        r"\b(?:access[_-]?token|refresh[_-]?token|oauth[_-]?token|app[_-]?secret|client[_-]?secret|authorization[_-]?code)\s*[:=]",
        re.IGNORECASE,
    )
    oauth_code_exchange_pattern = re.compile(
        r"(?:/oauth/access_token|/access_token|/refresh_access_token)\?[^\s]*\bcode=[^&\s]+",
        re.IGNORECASE,
    )
    header_pattern = re.compile(r"\b(?:proxy-)?authorization\s*:", re.IGNORECASE)
    cursor_assignment_pattern = re.compile(
        r"\b(?:raw[_-]?)?cursor\s*[:=]\s*[^\s,;]+", re.IGNORECASE
    )
    after_assignment_pattern = re.compile(r"\bafter\s*=\s*[^&\s,;]+", re.IGNORECASE)
    url_pattern = re.compile(r"https?://[^\s<>\"'`]+", re.IGNORECASE)
    account_identifier_pattern = re.compile(
        r"\b(?:account|user|thread|media|app|client)_?id\s*[:=]\s*[A-Za-z0-9_-]+",
        re.IGNORECASE,
    )
    username_pattern = re.compile(r"(?<![A-Za-z0-9_])@[A-Za-z0-9_.]{2,30}\b")
    numeric_identifier_pattern = re.compile(r"\b[0-9]{8,}\b")
    fingerprint_pattern = re.compile(r"^sha256:[0-9a-f]{64}$")

    def add(path: tuple[str | int, ...], kind: str) -> None:
        if len(issues) < _MAX_DIAGNOSTICS:
            issues.append((_safe_location(path), kind))

    def inspect(value: Any, path: tuple[str | int, ...], depth: int) -> None:
        if len(issues) >= _MAX_DIAGNOSTICS:
            return
        if depth > 80:
            add(path, "nested content exceeds the safe inspection limit")
            return
        if isinstance(value, dict):
            for key, child in cast(dict[Any, Any], value).items():
                if not isinstance(key, str):
                    add(path, "object key must be text")
                    continue
                normalised = _normalise_name(key)
                child_path = (*path, key)
                if normalised in {"authorization", "proxyauthorization"}:
                    add((*path, "<header>"), "authorization header name is prohibited")
                if (
                    normalised == "token"
                    or normalised.endswith("token")
                    or any(part in normalised for part in _SENSITIVE_FIELD_PARTS)
                ):
                    add((*path, "<field>"), "credential-bearing field name is prohibited")
                if (
                    "cursor" in normalised
                    or normalised in {"after", "aftervalue", "pagingafter", "nextpage"}
                ) and not (
                    normalised.endswith("fingerprint") or normalised in _SAFE_CURSOR_FIELD_NAMES
                ):
                    add((*path, "<field>"), "raw cursor field is prohibited")
                inspect(child, child_path, depth + 1)
        elif isinstance(value, list):
            for index, child in enumerate(cast(list[Any], value)):
                inspect(child, (*path, index), depth + 1)
        elif isinstance(value, str):
            folded = value.casefold()
            bare_field_name = value.strip()
            if re.fullmatch(r"[A-Za-z_-]+", bare_field_name) and _normalise_name(
                bare_field_name
            ) in {
                "accesstoken",
                "appsecret",
                "clientsecret",
                "authorizationcode",
                "refreshtoken",
            }:
                add(path, "credential-bearing field name is prohibited")
            if folded.strip() in {"authorization", "proxy-authorization"}:
                add(path, "authorization header name is prohibited")
            if bearer_pattern.search(value):
                add(path, "bearer credential pattern is prohibited")
            if threads_token_pattern.search(value):
                add(path, "Threads token-like prefix is prohibited")
            if secret_assignment_pattern.search(value):
                add(path, "credential assignment pattern is prohibited")
            if oauth_code_exchange_pattern.search(value):
                add(path, "OAuth exchange URL with secret query is prohibited")
            if header_pattern.search(value):
                add(path, "authorization header text is prohibited")
            if cursor_assignment_pattern.search(value) or after_assignment_pattern.search(value):
                add(path, "raw cursor assignment is prohibited")
            if account_identifier_pattern.search(value):
                add(path, "raw account-specific identifier is prohibited")
            if username_pattern.search(value):
                add(path, "account-specific username is prohibited")
            if (
                "official_source_references_reviewed" not in path
                and numeric_identifier_pattern.search(value)
                and not fingerprint_pattern.fullmatch(value)
            ):
                add(path, "long numeric identifier is prohibited")
            for match in url_pattern.finditer(value):
                candidate = match.group(0).rstrip(".,;:!?)]}")
                in_source_references = "official_source_references_reviewed" in path
                if not in_source_references:
                    add(path, "URL content outside official source references is prohibited")
                    continue
                try:
                    query_pairs = parse_qsl(urlsplit(candidate).query, keep_blank_values=True)
                except ValueError:
                    add(path, "malformed source URL is prohibited")
                    continue
                if any(_sensitive_query_name(name) for name, _ in query_pairs):
                    add(path, "secret-bearing source URL query is prohibited")

    inspect(document, (), 0)
    return issues


def _duplicate_case_ids(document: Any) -> bool:
    if not isinstance(document, dict):
        return False
    cases = cast(dict[Any, Any], document).get("cases")
    if not isinstance(cases, list):
        return False
    seen: set[str] = set()
    for case in cast(list[Any], cases):
        if not isinstance(case, dict):
            continue
        case_id = cast(dict[Any, Any], case).get("case_id")
        if not isinstance(case_id, str):
            continue
        if case_id in seen:
            return True
        seen.add(case_id)
    return False


def validate_packet_json(packet_text: str) -> ThreadsLiveEvidencePacket:
    """Validate a scrubbed JSON packet without including input values in errors."""
    if len(packet_text.encode("utf-8", errors="replace")) > _MAX_PACKET_BYTES:
        raise PacketRejectedError([("packet", "packet exceeds the inspection size limit")])
    try:
        document = json.loads(
            packet_text,
            object_pairs_hook=_duplicate_rejecting_object,
            parse_constant=_reject_json_constant,
        )
    except _DuplicateJSONKeyError:
        raise PacketRejectedError([("packet", "duplicate JSON object key")]) from None
    except json.JSONDecodeError, UnicodeError, ValueError, RecursionError:
        raise PacketRejectedError([("packet", "invalid JSON document")]) from None

    issues = _scan_secret_content(document)
    if issues:
        raise PacketRejectedError(issues)
    if _duplicate_case_ids(document):
        raise PacketRejectedError([("packet.cases", "duplicate case ID")])

    try:
        return ThreadsLiveEvidencePacket.model_validate_json(packet_text, strict=True)
    except ValidationError as exc:
        safe_issues: list[tuple[str, str]] = []
        for error in exc.errors(include_input=False, include_url=False):
            context = error.get("ctx")
            custom_error = context.get("error") if isinstance(context, dict) else None
            kind = str(custom_error) if isinstance(custom_error, ValueError) else str(error["type"])
            location = _safe_location(error["loc"])
            safe_issues.append((location, kind))
        raise PacketRejectedError(safe_issues) from None


def fingerprint_opaque_value(value: str | bytes) -> str:
    """Return a SHA-256 comparison fingerprint; this is not authentication material."""
    raw_value = value.encode("utf-8") if isinstance(value, str) else value
    return f"sha256:{hashlib.sha256(raw_value).hexdigest()}"


def fingerprint_from_stdin(stdin: BinaryIO, stdout: TextIO) -> int:
    raw_value = stdin.read()
    if raw_value.endswith(b"\r\n"):
        raw_value = raw_value[:-2]
    elif raw_value.endswith(b"\n"):
        raw_value = raw_value[:-1]
    if not raw_value:
        print("fingerprint requires a non-empty value on standard input", file=sys.stderr)
        return 2
    stdout.write(fingerprint_opaque_value(raw_value) + "\n")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = _SafeArgumentParser(description="Offline Threads live evidence tooling")
    commands = parser.add_subparsers(dest="command", required=True)
    validate_parser = commands.add_parser("validate", help="validate a scrubbed evidence packet")
    validate_parser.add_argument("packet", type=Path)
    commands.add_parser("fingerprint", help="fingerprint opaque bytes read from standard input")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.command == "fingerprint":
        if sys.stdin.isatty():
            print(
                "fingerprint requires redirected standard input; do not type opaque values "
                "into a terminal",
                file=sys.stderr,
            )
            return 2
        return fingerprint_from_stdin(sys.stdin.buffer, sys.stdout)

    try:
        packet_bytes = args.packet.read_bytes()
    except OSError:
        print("invalid evidence packet: unable to read input file", file=sys.stderr)
        return 2
    if len(packet_bytes) > _MAX_PACKET_BYTES:
        print("invalid evidence packet: packet exceeds the inspection size limit", file=sys.stderr)
        return 2
    try:
        packet_text = packet_bytes.decode("utf-8")
    except UnicodeDecodeError:
        print("invalid evidence packet: packet must be UTF-8 JSON", file=sys.stderr)
        return 2
    try:
        packet = validate_packet_json(packet_text)
    except PacketRejectedError as exc:
        print("invalid evidence packet:", file=sys.stderr)
        for location, kind in exc.issues:
            print(f"- {location}: {kind}", file=sys.stderr)
        return 1

    print(f"Valid {packet.schema_version} packet: {packet.packet_classification.value}")
    if packet.packet_classification is PacketClassification.TEMPLATE_ONLY_NOT_LIVE_EVIDENCE:
        print("This template is not live evidence and proves no endpoint was tested.")
    else:
        print(
            "Packet contains only schema-approved scrubbed observations; "
            "no live readiness was derived."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
