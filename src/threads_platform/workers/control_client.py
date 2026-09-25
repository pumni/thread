import base64
import os
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import cast
from urllib.parse import urlsplit
from uuid import UUID

import httpx

from threads_platform.application.ports.worker_agent import (
    LocalSessionState,
    WorkerAccountContext,
    WorkerAgentPresence,
    WorkerControlClientError,
    WorkerJobSnapshot,
)
from threads_platform.domain.worker_jobs import WorkerJobRetrySafety, WorkerJobStatus
from threads_platform.domain.workers import NetworkProfile, NetworkProtocol, WorkerStatus
from threads_platform.infrastructure.security.worker_auth import challenge_message
from threads_platform.workers.key_store import WorkerDeviceIdentity


@dataclass(frozen=True, slots=True)
class _Challenge:
    challenge_id: UUID
    nonce: str


class HttpWorkerControlClient:
    def __init__(
        self,
        control_plane_url: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout_seconds: float = 10.0,
    ) -> None:
        parsed = urlsplit(control_plane_url)
        if (
            parsed.scheme.lower() != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Worker Control Plane URL must be an HTTPS origin without credentials")
        self._client = httpx.AsyncClient(
            base_url=control_plane_url.rstrip("/"),
            timeout=httpx.Timeout(timeout_seconds),
            follow_redirects=False,
            transport=transport,
        )
        self._access_token: str | None = None
        self._expires_at: datetime | None = None
        self._worker_id: UUID | None = None

    @property
    def access_token_expires_at(self) -> datetime | None:
        return self._expires_at

    async def authenticate(
        self,
        worker_id: UUID,
        identity: WorkerDeviceIdentity,
        *,
        enrollment_pending: bool,
        enrollment_code: str | None,
        display_name: str,
        hostname: str,
        max_concurrent_jobs: int,
        max_browser_sessions: int,
    ) -> None:
        self._worker_id = worker_id
        try:
            challenge = await self._create_challenge(worker_id)
        except WorkerControlClientError as error:
            if (
                error.code != "WORKER_NOT_AUTHENTICATABLE"
                or not enrollment_pending
                or not enrollment_code
            ):
                raise
            await self._enroll(
                worker_id,
                identity.public_key_bytes,
                enrollment_code=enrollment_code,
                display_name=display_name,
                hostname=hostname,
                max_concurrent_jobs=max_concurrent_jobs,
                max_browser_sessions=max_browser_sessions,
            )
            challenge = await self._create_challenge(worker_id)

        signature = identity.sign(challenge_message(challenge.challenge_id, challenge.nonce))
        session = await self._request(
            "POST",
            "/v1/workers/auth/sessions",
            json={
                "challenge_id": str(challenge.challenge_id),
                "signature": base64.b64encode(signature).decode("ascii"),
            },
        )
        if session is None:
            raise WorkerControlClientError("WORKER_PROTOCOL_INVALID_RESPONSE")
        access_token = _text_field(session, "access_token")
        expires_at = _datetime_field(session, "expires_at")
        self._access_token = access_token
        self._expires_at = expires_at

    async def _create_challenge(self, worker_id: UUID) -> _Challenge:
        response = await self._request(
            "POST", "/v1/workers/auth/challenges", json={"worker_id": str(worker_id)}
        )
        if response is None:
            raise WorkerControlClientError("WORKER_PROTOCOL_INVALID_RESPONSE")
        return _Challenge(
            _uuid_field(response, "challenge_id"),
            _text_field(response, "nonce"),
        )

    async def _enroll(
        self,
        worker_id: UUID,
        public_key: bytes,
        *,
        enrollment_code: str,
        display_name: str,
        hostname: str,
        max_concurrent_jobs: int,
        max_browser_sessions: int,
    ) -> None:
        platform = "windows" if os.name == "nt" else "worker-test"
        await self._request(
            "POST",
            "/v1/workers/enroll",
            json={
                "enrollment_code": enrollment_code,
                "worker_id": str(worker_id),
                "display_name": display_name,
                "hostname": hostname,
                "platform": platform,
                "public_key": base64.b64encode(public_key).decode("ascii"),
                "max_concurrent_jobs": max_concurrent_jobs,
                "max_browser_sessions": max_browser_sessions,
            },
        )

    async def hello(
        self,
        worker_id: UUID,
        *,
        agent_version: str,
        capabilities: Sequence[tuple[str, int]],
        max_concurrent_jobs: int,
        max_browser_sessions: int,
        active_browser_sessions: int,
    ) -> WorkerAgentPresence:
        response = await self._request(
            "POST",
            "/v1/workers/hello",
            json={
                "protocol_version": 2,
                "agent_version": agent_version,
                "capabilities_schema_version": 1,
                "capabilities": [
                    {"capability_name": name, "capability_version": version}
                    for name, version in capabilities
                ],
                "display_name": None,
                "max_concurrent_jobs": max_concurrent_jobs,
                "max_browser_sessions": max_browser_sessions,
                "active_browser_sessions": active_browser_sessions,
            },
            authenticated=True,
        )
        if response is None:
            raise WorkerControlClientError("WORKER_PROTOCOL_INVALID_RESPONSE")
        return _presence(worker_id, response)

    async def heartbeat(self, active_browser_sessions: int) -> WorkerAgentPresence:
        if self._worker_id is None:
            raise WorkerControlClientError("WORKER_IDENTITY_UNAVAILABLE")
        response = await self._request(
            "POST",
            "/v1/workers/heartbeat",
            json={"healthy": True, "active_browser_sessions": active_browser_sessions},
            authenticated=True,
        )
        if response is None:
            raise WorkerControlClientError("WORKER_PROTOCOL_INVALID_RESPONSE")
        return _presence(self._worker_id, response)

    async def reconcile(self) -> tuple[WorkerJobSnapshot, ...]:
        response = await self._request("GET", "/v1/workers/jobs/reconcile", authenticated=True)
        if response is None:
            raise WorkerControlClientError("WORKER_PROTOCOL_INVALID_RESPONSE")
        jobs = response.get("jobs")
        if not isinstance(jobs, list):
            raise WorkerControlClientError("WORKER_PROTOCOL_INVALID_RESPONSE")
        typed_jobs = cast(list[object], jobs)
        if any(not isinstance(item, dict) for item in typed_jobs):
            raise WorkerControlClientError("WORKER_PROTOCOL_INVALID_RESPONSE")
        return tuple(_job_snapshot(cast(dict[str, object], item)) for item in typed_jobs)

    async def claim_next(self) -> WorkerJobSnapshot | None:
        response = await self._request(
            "POST", "/v1/workers/jobs/claim", allow_no_content=True, authenticated=True
        )
        return None if response is None else _job_snapshot(response)

    async def renew_job(self, job_id: UUID, lease_token: UUID) -> WorkerJobSnapshot:
        response = await self._request(
            "POST",
            f"/v1/workers/jobs/{job_id}/renew",
            json={"lease_token": str(lease_token)},
            authenticated=True,
        )
        if response is None:
            raise WorkerControlClientError("WORKER_PROTOCOL_INVALID_RESPONSE")
        return _job_snapshot(response)

    async def checkpoint_job(
        self, job_id: UUID, lease_token: UUID, checkpoint: dict[str, object]
    ) -> WorkerJobSnapshot:
        response = await self._request(
            "POST",
            f"/v1/workers/jobs/{job_id}/checkpoint",
            json={"lease_token": str(lease_token), "checkpoint": checkpoint},
            authenticated=True,
        )
        if response is None:
            raise WorkerControlClientError("WORKER_PROTOCOL_INVALID_RESPONSE")
        return _job_snapshot(response)

    async def complete_job(
        self, job_id: UUID, lease_token: UUID, result: dict[str, object]
    ) -> WorkerJobSnapshot:
        response = await self._request(
            "POST",
            f"/v1/workers/jobs/{job_id}/complete",
            json={"lease_token": str(lease_token), "result": result},
            authenticated=True,
        )
        if response is None:
            raise WorkerControlClientError("WORKER_PROTOCOL_INVALID_RESPONSE")
        return _job_snapshot(response)

    async def fail_job(
        self,
        job_id: UUID,
        lease_token: UUID,
        *,
        error_code: str,
        retryable: bool,
        outcome_ambiguous: bool = False,
    ) -> WorkerJobSnapshot:
        response = await self._request(
            "POST",
            f"/v1/workers/jobs/{job_id}/fail",
            json={
                "lease_token": str(lease_token),
                "error_code": error_code,
                "retryable": retryable,
                "outcome_ambiguous": outcome_ambiguous,
            },
            authenticated=True,
        )
        if response is None:
            raise WorkerControlClientError("WORKER_PROTOCOL_INVALID_RESPONSE")
        return _job_snapshot(response)

    async def request_intervention(
        self,
        job_id: UUID,
        lease_token: UUID,
        *,
        intervention_type: str,
        detail_code: str,
    ) -> WorkerJobSnapshot:
        response = await self._request(
            "POST",
            f"/v1/workers/jobs/{job_id}/interventions",
            json={
                "lease_token": str(lease_token),
                "intervention_type": intervention_type,
                "detail_code": detail_code,
            },
            authenticated=True,
        )
        if response is None:
            raise WorkerControlClientError("WORKER_PROTOCOL_INVALID_RESPONSE")
        return _job_snapshot(response)

    async def account_context(self, account_id: UUID) -> WorkerAccountContext:
        response = await self._request(
            "GET",
            f"/v1/workers/accounts/{account_id}/context",
            authenticated=True,
        )
        if response is None:
            raise WorkerControlClientError("WORKER_PROTOCOL_INVALID_RESPONSE")
        resolved_account = _uuid_field(response, "account_id")
        worker_id = _uuid_field(response, "worker_id")
        profile_ref = _text_field(response, "profile_ref")
        network_data = response.get("network_profile")
        network_profile: NetworkProfile | None = None
        if network_data is not None:
            if not isinstance(network_data, dict):
                raise WorkerControlClientError("WORKER_PROTOCOL_INVALID_RESPONSE")
            network_payload = cast(dict[str, object], network_data)
            try:
                network_profile = NetworkProfile(
                    account_id=resolved_account,
                    id=_uuid_field(network_payload, "id"),
                    name="assigned network route",
                    protocol=NetworkProtocol(_text_field(network_payload, "protocol")),
                    host=_optional_text_field(network_payload, "host"),
                    port=_optional_int_field(network_payload, "port"),
                    credential_ref=_optional_text_field(network_payload, "credential_ref"),
                )
            except (ValueError, TypeError) as error:
                raise WorkerControlClientError("WORKER_PROTOCOL_INVALID_RESPONSE") from error
        if resolved_account != account_id:
            raise WorkerControlClientError("ACCOUNT_CONTEXT_MISMATCH")
        return WorkerAccountContext(resolved_account, worker_id, profile_ref, network_profile)

    async def report_session_state(self, session: LocalSessionState) -> None:
        await self._request(
            "PUT",
            f"/v1/workers/accounts/{session.account_id}/session",
            json={
                "profile_ref": session.profile_ref,
                "session_id": str(session.session_id),
                "state": session.state.value,
                "revision": session.revision,
            },
            authenticated=True,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, object] | None = None,
        authenticated: bool = False,
        allow_no_content: bool = False,
    ) -> dict[str, object] | None:
        headers: dict[str, str] = {}
        if authenticated:
            if self._access_token is None:
                raise WorkerControlClientError("WORKER_SESSION_REQUIRED")
            headers["Authorization"] = f"Bearer {self._access_token}"
        try:
            response = await self._client.request(method, path, json=json, headers=headers)
        except httpx.HTTPError as error:
            raise WorkerControlClientError("CONTROL_PLANE_UNAVAILABLE") from error
        if allow_no_content and response.status_code == 204:
            return None
        if response.status_code < 200 or response.status_code >= 300:
            raise WorkerControlClientError(_error_code(response), status_code=response.status_code)
        try:
            payload = response.json()
        except ValueError as error:
            raise WorkerControlClientError("WORKER_PROTOCOL_INVALID_RESPONSE") from error
        if not isinstance(payload, dict):
            raise WorkerControlClientError("WORKER_PROTOCOL_INVALID_RESPONSE")
        return cast(dict[str, object], payload)


def _error_code(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return "CONTROL_PLANE_HTTP_ERROR"
    if isinstance(body, dict):
        object_body = cast(dict[str, object], body)
        detail = object_body.get("detail")
        if isinstance(detail, dict):
            object_detail = cast(dict[str, object], detail)
            code = object_detail.get("code")
            if isinstance(code, str) and re.fullmatch(r"[A-Z0-9_]{1,120}", code):
                return code
    return "CONTROL_PLANE_HTTP_ERROR"


def _presence(worker_id: UUID, payload: dict[str, object] | None) -> WorkerAgentPresence:
    if payload is None:
        raise WorkerControlClientError("WORKER_PROTOCOL_INVALID_RESPONSE")
    try:
        return WorkerAgentPresence(
            worker_id=worker_id,
            status=WorkerStatus(_text_field(payload, "status")),
            protocol_compatible=payload.get("protocol_compatible") is True,
            max_browser_sessions=_int_field(payload, "max_browser_sessions"),
            active_browser_sessions=_int_field(payload, "active_browser_sessions"),
        )
    except (ValueError, TypeError) as error:
        raise WorkerControlClientError("WORKER_PROTOCOL_INVALID_RESPONSE") from error


def _job_snapshot(payload: dict[str, object]) -> WorkerJobSnapshot:
    try:
        return WorkerJobSnapshot(
            job_id=_uuid_field(payload, "id"),
            capability_name=_text_field(payload, "capability_name"),
            capability_version=_int_field(payload, "capability_version"),
            status=WorkerJobStatus(_text_field(payload, "status")),
            account_id=_optional_uuid_field(payload, "account_id"),
            assigned_worker_id=_optional_uuid_field(payload, "assigned_worker_id"),
            lease_worker_id=_optional_uuid_field(payload, "lease_worker_id"),
            lease_token=_optional_uuid_field(payload, "lease_token"),
            lease_expires_at=_optional_datetime_field(payload, "lease_expires_at"),
            retry_safety=WorkerJobRetrySafety(_text_field(payload, "retry_safety")),
            checkpoint=_optional_object_field(payload, "checkpoint"),
        )
    except (ValueError, TypeError) as error:
        raise WorkerControlClientError("WORKER_PROTOCOL_INVALID_RESPONSE") from error


def _text_field(payload: dict[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise WorkerControlClientError("WORKER_PROTOCOL_INVALID_RESPONSE")
    return value


def _int_field(payload: dict[str, object], key: str) -> int:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise WorkerControlClientError("WORKER_PROTOCOL_INVALID_RESPONSE")
    return value


def _uuid_field(payload: dict[str, object], key: str) -> UUID:
    try:
        return UUID(_text_field(payload, key))
    except ValueError as error:
        raise WorkerControlClientError("WORKER_PROTOCOL_INVALID_RESPONSE") from error


def _optional_uuid_field(payload: dict[str, object], key: str) -> UUID | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise WorkerControlClientError("WORKER_PROTOCOL_INVALID_RESPONSE")
    try:
        return UUID(value)
    except ValueError as error:
        raise WorkerControlClientError("WORKER_PROTOCOL_INVALID_RESPONSE") from error


def _datetime_field(payload: dict[str, object], key: str) -> datetime:
    value = _text_field(payload, key)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise WorkerControlClientError("WORKER_PROTOCOL_INVALID_RESPONSE") from error
    if parsed.tzinfo is None:
        raise WorkerControlClientError("WORKER_PROTOCOL_INVALID_RESPONSE")
    return parsed


def _optional_datetime_field(payload: dict[str, object], key: str) -> datetime | None:
    if payload.get(key) is None:
        return None
    return _datetime_field(payload, key)


def _optional_text_field(payload: dict[str, object], key: str) -> str | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise WorkerControlClientError("WORKER_PROTOCOL_INVALID_RESPONSE")
    return value


def _optional_int_field(payload: dict[str, object], key: str) -> int | None:
    value = payload.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise WorkerControlClientError("WORKER_PROTOCOL_INVALID_RESPONSE")
    return value


def _optional_object_field(payload: dict[str, object], key: str) -> dict[str, object] | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, dict):
        raise WorkerControlClientError("WORKER_PROTOCOL_INVALID_RESPONSE")
    return cast(dict[str, object], value)
