from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast
from uuid import UUID, uuid4

import pytest

import threads_platform.workers.media_local_upload as media_worker_module
from threads_platform.application.ports.browser import (
    BrowserAdapterError,
    BrowserContractError,
    BrowserNetworkProtocol,
    BrowserNetworkRoute,
    BrowserNetworkRouteUnsupported,
    BrowserProcessCrashed,
    BrowserSurface,
    MediaUploadFailed,
    PreparedMediaComposer,
    RemoteSessionStateUncertain,
)
from threads_platform.application.ports.worker_agent import (
    LocalRecoveryEntry,
    LocalSessionState,
    WorkerAccountContext,
    WorkerControlClientError,
    WorkerJobSnapshot,
    WorkerLocalState,
)
from threads_platform.domain.worker_jobs import WorkerJobRetrySafety, WorkerJobStatus
from threads_platform.domain.workers import BrowserSessionState
from threads_platform.infrastructure.worker_agent.local_media import (
    LocalMediaFileResolver,
)
from threads_platform.infrastructure.worker_agent.local_state import LocalDataRoot
from threads_platform.workers.browser import WorkerBrowserSession
from threads_platform.workers.media_local_upload import (
    MEDIA_LOCAL_UPLOAD_ALLOWED_FAILURE_CODES,
    MEDIA_LOCAL_UPLOAD_CAPABILITY_NAME,
    BrowserLocalMediaUploadWorker,
    parse_media_ref,
)
from threads_platform.workers.sessions import BrowserSessionOpenResult, WorkerNetworkRoute


def test_media_ref_is_one_bounded_logical_filename() -> None:
    assert parse_media_ref({"media_ref": "one-image.webp"}) == "one-image.webp"
    for payload in (
        {},
        {"media_ref": "..\\secret.jpg"},
        {"media_ref": "C:\\media\\image.jpg"},
        {"media_ref": "image.jpg", "path": "C:\\media\\image.jpg"},
    ):
        assert parse_media_ref(payload) is None


@pytest.mark.asyncio
async def test_worker_stages_one_image_inside_fenced_boundary_without_path_leak(
    tmp_path: Path,
) -> None:
    worker_id, account_id = uuid4(), uuid4()
    media_file = _image_file(tmp_path)
    control = _MemoryControl(worker_id, account_id)
    manager = _MemorySessionManager(worker_id, account_id)
    local_state = _MemoryLocalState()
    worker = _worker(worker_id, control, manager, tmp_path, local_state)

    await worker(control.snapshot)

    assert control.snapshot.status is WorkerJobStatus.SUCCEEDED
    assert control.completed == {
        "result_version": 1,
        "media_kind": "IMAGE",
        "byte_size": media_file.stat().st_size,
        "staged": True,
    }
    assert manager.engine.events == [
        "composer_prepared",
        "network_observer_armed",
        "file_selected",
        "upload_response_200",
        "preview_verified",
    ]
    assert manager.engine.selection_count == 1
    assert control.checkpoint_phases == [
        "BEFORE_LOCAL_STAGE",
        "MUTATION_PENDING",
        "MUTATION_CONFIRMED",
        "LOCAL_STAGE_COMPLETE",
    ]
    assert local_state.saved_phases == [
        "MUTATION_PENDING",
        "MUTATION_STARTED",
        "MUTATION_CONFIRMED",
    ]
    assert local_state.cleared_job_ids == [control.snapshot.job_id]
    assert str(media_file) not in repr(control.snapshot.input_data)
    assert control.snapshot.input_data == {"media_ref": "asset.png"}
    assert str(media_file) not in repr(control.completed)


@pytest.mark.asyncio
async def test_no_composer_requests_operator_confirmation_before_mutation(tmp_path: Path) -> None:
    worker_id, account_id = uuid4(), uuid4()
    _image_file(tmp_path)
    control = _MemoryControl(worker_id, account_id)
    manager = _MemorySessionManager(worker_id, account_id, has_composer=False)
    local_state = _MemoryLocalState()

    await _worker(worker_id, control, manager, tmp_path, local_state)(control.snapshot)

    assert control.snapshot.status is WorkerJobStatus.WAITING_INTERVENTION
    assert control.interventions == [("OPERATOR_CONFIRMATION_REQUIRED", "COMPOSER_OPEN_REQUIRED")]
    assert manager.engine.selection_count == 0
    assert manager.engine.events == []
    assert local_state.saved_phases == []


@pytest.mark.asyncio
async def test_malformed_composer_fails_closed_before_irreversible_boundary(
    tmp_path: Path,
) -> None:
    worker_id, account_id = uuid4(), uuid4()
    _image_file(tmp_path)
    control = _MemoryControl(worker_id, account_id)
    manager = _MemorySessionManager(worker_id, account_id, composer_error=BrowserContractError())

    await _worker(worker_id, control, manager, tmp_path, _MemoryLocalState())(control.snapshot)

    assert control.failures == [("BROWSER_CONTRACT_MISMATCH", False)]
    assert manager.engine.selection_count == 0
    assert control.interventions == []


@pytest.mark.parametrize(
    ("failure", "expected_code"),
    [
        ("unsupported_capability", "UNSUPPORTED_BROWSER_CAPABILITY"),
        ("retry_safety", "WORKER_JOB_RETRY_SAFETY_MISMATCH"),
        ("invalid_input", "WORKER_JOB_INPUT_INVALID"),
        ("session_unavailable", "BROWSER_SESSION_UNAVAILABLE"),
        ("unsupported_network_route", "BROWSER_NETWORK_ROUTE_UNSUPPORTED"),
    ],
)
@pytest.mark.asyncio
async def test_direct_worker_failures_are_declared_and_pre_mutation(
    tmp_path: Path,
    failure: str,
    expected_code: str,
) -> None:
    worker_id, account_id = uuid4(), uuid4()
    _image_file(tmp_path)
    control = _MemoryControl(worker_id, account_id)
    manager = _MemorySessionManager(worker_id, account_id)
    if failure == "unsupported_capability":
        control.snapshot = replace(control.snapshot, capability_version=2)
    elif failure == "retry_safety":
        control.snapshot = replace(
            control.snapshot, retry_safety=WorkerJobRetrySafety.SAFE_TO_RETRY
        )
    elif failure == "invalid_input":
        control.snapshot = replace(
            control.snapshot, input_data={"media_ref": "asset.png", "path": "x"}
        )
    elif failure == "session_unavailable":
        manager.open_error = ValueError("session unavailable")
    else:
        manager.open_error = BrowserNetworkRouteUnsupported()

    await _worker(worker_id, control, manager, tmp_path, _MemoryLocalState())(control.snapshot)

    assert expected_code in MEDIA_LOCAL_UPLOAD_ALLOWED_FAILURE_CODES
    assert control.failures == [(expected_code, expected_code == "BROWSER_SESSION_UNAVAILABLE")]
    assert manager.engine.selection_count == 0
    assert control.interventions == []


@pytest.mark.parametrize(
    ("media_ref", "file_bytes", "resolver_limit", "expected_code"),
    [
        ("missing.jpg", None, None, "MEDIA_FILE_UNAVAILABLE"),
        ("movie.mp4", b"video", None, "MEDIA_FILE_REJECTED"),
        ("asset.png", b"too-large", 2, "MEDIA_FILE_TOO_LARGE"),
        ("..\\outside.jpg", None, None, "WORKER_JOB_INPUT_INVALID"),
    ],
)
@pytest.mark.asyncio
async def test_invalid_or_video_media_fails_before_file_selection(
    tmp_path: Path,
    media_ref: str,
    file_bytes: bytes | None,
    resolver_limit: int | None,
    expected_code: str,
) -> None:
    worker_id, account_id = uuid4(), uuid4()
    root = LocalDataRoot(tmp_path)
    root.prepare()
    if file_bytes is not None:
        (root.child("media") / media_ref).write_bytes(file_bytes)
    control = _MemoryControl(worker_id, account_id, media_ref=media_ref)
    manager = _MemorySessionManager(worker_id, account_id)
    resolver = LocalMediaFileResolver(root, max_file_bytes=resolver_limit or 50 * 1024 * 1024)
    worker = BrowserLocalMediaUploadWorker(
        worker_id,
        cast(media_worker_module.MediaLocalUploadControlClient, control),
        cast(media_worker_module.MediaLocalUploadSessionManager, manager),
        resolver,
        cast(WorkerLocalState, _MemoryLocalState()),
    )

    await worker(control.snapshot)

    assert expected_code in MEDIA_LOCAL_UPLOAD_ALLOWED_FAILURE_CODES
    assert control.failures == [(expected_code, False)]
    assert manager.engine.selection_count == 0
    assert control.interventions == []


@pytest.mark.parametrize(
    ("failure", "expected_intervention"),
    [
        ("non_200", ("AMBIGUOUS_OUTCOME", "ACTION_OUTCOME_REQUIRES_RECONCILIATION")),
        ("missing_preview", ("AMBIGUOUS_OUTCOME", "ACTION_OUTCOME_REQUIRES_RECONCILIATION")),
        ("duplicate_request", ("AMBIGUOUS_OUTCOME", "ACTION_OUTCOME_REQUIRES_RECONCILIATION")),
        ("network_ambiguity", ("AMBIGUOUS_OUTCOME", "ACTION_OUTCOME_REQUIRES_RECONCILIATION")),
        ("missing_completion", ("AMBIGUOUS_OUTCOME", "ACTION_OUTCOME_REQUIRES_RECONCILIATION")),
        ("timeout", ("AMBIGUOUS_OUTCOME", "ACTION_OUTCOME_REQUIRES_RECONCILIATION")),
        ("crash", ("AMBIGUOUS_OUTCOME", "ACTION_OUTCOME_REQUIRES_RECONCILIATION")),
        ("off_origin", ("AMBIGUOUS_OUTCOME", "ACTION_OUTCOME_REQUIRES_RECONCILIATION")),
    ],
)
@pytest.mark.asyncio
async def test_post_selection_uncertainty_is_ambiguous_without_retry(
    tmp_path: Path,
    failure: str,
    expected_intervention: tuple[str, str],
) -> None:
    worker_id, account_id = uuid4(), uuid4()
    _image_file(tmp_path)
    control = _MemoryControl(worker_id, account_id)
    manager = _MemorySessionManager(worker_id, account_id, stage_failure=failure)

    await _worker(worker_id, control, manager, tmp_path, _MemoryLocalState())(control.snapshot)

    assert control.snapshot.status is WorkerJobStatus.WAITING_INTERVENTION
    assert control.interventions == [expected_intervention]
    assert manager.engine.selection_count == 1
    assert control.completed is None


@pytest.mark.parametrize(
    "requeued_phase",
    ["MUTATION_PENDING", "MUTATION_STARTED", "MUTATION_CONFIRMED", "LOCAL_STAGE_COMPLETE"],
)
@pytest.mark.asyncio
async def test_requeued_ambiguous_upload_checkpoint_never_selects_again(
    tmp_path: Path,
    requeued_phase: str,
) -> None:
    worker_id, account_id = uuid4(), uuid4()
    _image_file(tmp_path)
    control = _MemoryControl(worker_id, account_id)
    manager = _MemorySessionManager(worker_id, account_id, stage_failure="non_200")
    local_state = _MemoryLocalState()
    worker = _worker(worker_id, control, manager, tmp_path, local_state)

    await worker(control.snapshot)

    assert control.interventions == [
        ("AMBIGUOUS_OUTCOME", "ACTION_OUTCOME_REQUIRES_RECONCILIATION")
    ]
    assert manager.engine.selection_count == 1
    assert control.snapshot.checkpoint == {"phase": "MUTATION_PENDING"}

    control.snapshot = replace(
        control.snapshot,
        status=WorkerJobStatus.RUNNING,
        lease_worker_id=worker_id,
        lease_token=uuid4(),
        lease_expires_at=datetime.now(UTC) + timedelta(minutes=2),
        checkpoint={"phase": requeued_phase},
    )
    control.interventions.clear()
    await worker(control.snapshot)

    assert control.interventions == [
        ("AMBIGUOUS_OUTCOME", "ACTION_OUTCOME_REQUIRES_RECONCILIATION")
    ]
    assert manager.engine.selection_count == 1
    assert manager.engine.events.count("composer_prepared") == 1


@pytest.mark.asyncio
async def test_stale_lease_before_boundary_prevents_file_selection(tmp_path: Path) -> None:
    worker_id, account_id = uuid4(), uuid4()
    _image_file(tmp_path)
    control = _MemoryControl(worker_id, account_id)
    control.lose_lease_on_checkpoint_phase = "MUTATION_PENDING"
    manager = _MemorySessionManager(worker_id, account_id)
    local_state = _MemoryLocalState()

    await _worker(worker_id, control, manager, tmp_path, local_state)(control.snapshot)

    assert manager.engine.selection_count == 0
    assert control.completed is None
    assert control.interventions == []
    assert local_state.saved_phases == ["MUTATION_PENDING"]


@pytest.mark.asyncio
async def test_lease_loss_during_upload_cancels_wait_and_requires_reconciliation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker_id, account_id = uuid4(), uuid4()
    _image_file(tmp_path)
    control = _MemoryControl(worker_id, account_id)
    control.lose_lease_on_renew_call = 4
    manager = _MemorySessionManager(worker_id, account_id, stage_failure="wait")
    local_state = _MemoryLocalState()
    monkeypatch.setattr(media_worker_module, "MEDIA_UPLOAD_LEASE_RENEW_INTERVAL_SECONDS", 0.01)

    await _worker(worker_id, control, manager, tmp_path, local_state)(control.snapshot)

    assert control.snapshot.status is WorkerJobStatus.WAITING_INTERVENTION
    assert control.interventions == [
        ("AMBIGUOUS_OUTCOME", "ACTION_OUTCOME_REQUIRES_RECONCILIATION")
    ]
    assert manager.engine.selection_count == 1
    assert local_state.saved_phases == ["MUTATION_PENDING", "MUTATION_STARTED"]


@pytest.mark.asyncio
async def test_worker_requires_explicit_media_upload_opt_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for variable in (
        "THREADS_WORKER_FEED_BROWSE_ENABLED",
        "THREADS_WORKER_THREAD_OPEN_ENABLED",
        "THREADS_WORKER_PROFILE_OPEN_ENABLED",
        "THREADS_WORKER_MEDIA_LOCAL_UPLOAD_ENABLED",
    ):
        monkeypatch.delenv(variable, raising=False)
    assert media_worker_module.MEDIA_LOCAL_UPLOAD_CAPABILITY_NAME == (
        MEDIA_LOCAL_UPLOAD_CAPABILITY_NAME
    )
    from threads_platform.workers.__main__ import enabled_browser_capabilities

    assert enabled_browser_capabilities() == ()
    monkeypatch.setenv("THREADS_WORKER_MEDIA_LOCAL_UPLOAD_ENABLED", "true")
    assert enabled_browser_capabilities() == ((MEDIA_LOCAL_UPLOAD_CAPABILITY_NAME, 1),)


def _image_file(tmp_path: Path) -> Path:
    root = LocalDataRoot(tmp_path)
    root.prepare()
    path = root.child("media", "asset.png")
    path.write_bytes(b"synthetic-image")
    return path


def _worker(
    worker_id: UUID,
    control: _MemoryControl,
    manager: _MemorySessionManager,
    tmp_path: Path,
    local_state: _MemoryLocalState,
) -> BrowserLocalMediaUploadWorker:
    return BrowserLocalMediaUploadWorker(
        worker_id,
        cast(media_worker_module.MediaLocalUploadControlClient, control),
        cast(media_worker_module.MediaLocalUploadSessionManager, manager),
        LocalMediaFileResolver(LocalDataRoot(tmp_path)),
        cast(WorkerLocalState, local_state),
    )


class _MemoryControl:
    def __init__(
        self,
        worker_id: UUID,
        account_id: UUID,
        *,
        media_ref: str = "asset.png",
    ) -> None:
        self.worker_id = worker_id
        self.account_id = account_id
        self.snapshot = WorkerJobSnapshot(
            job_id=uuid4(),
            capability_name=MEDIA_LOCAL_UPLOAD_CAPABILITY_NAME,
            capability_version=1,
            status=WorkerJobStatus.RUNNING,
            account_id=account_id,
            assigned_worker_id=worker_id,
            lease_worker_id=worker_id,
            lease_token=uuid4(),
            lease_expires_at=datetime.now(UTC) + timedelta(minutes=2),
            retry_safety=WorkerJobRetrySafety.RECONCILIATION_REQUIRED,
            checkpoint=None,
            input_data={"media_ref": media_ref},
        )
        self.context = WorkerAccountContext(account_id, worker_id, "profile-main", None)
        self.completed: dict[str, object] | None = None
        self.failures: list[tuple[str, bool]] = []
        self.interventions: list[tuple[str, str]] = []
        self.checkpoint_phases: list[str] = []
        self.renew_calls = 0
        self.lose_lease_on_checkpoint_phase: str | None = None
        self.lose_lease_on_renew_call: int | None = None

    async def account_context(self, account_id: UUID) -> WorkerAccountContext:
        assert account_id == self.account_id
        return self.context

    async def renew_job(self, job_id: UUID, lease_token: UUID) -> WorkerJobSnapshot:
        self._verify(job_id, lease_token)
        self.renew_calls += 1
        if self.renew_calls == self.lose_lease_on_renew_call:
            raise WorkerControlClientError("WORKER_JOB_LEASE_LOST")
        return self.snapshot

    async def checkpoint_job(
        self, job_id: UUID, lease_token: UUID, checkpoint: dict[str, object]
    ) -> WorkerJobSnapshot:
        self._verify(job_id, lease_token)
        phase = checkpoint.get("phase")
        if isinstance(phase, str):
            self.checkpoint_phases.append(phase)
        if phase == self.lose_lease_on_checkpoint_phase:
            raise WorkerControlClientError("WORKER_JOB_LEASE_LOST")
        self.snapshot = replace(self.snapshot, checkpoint=checkpoint)
        return self.snapshot

    async def cancel_job(
        self,
        job_id: UUID,
        lease_token: UUID,
        *,
        cancel_request_id: UUID,
        generation: int,
        checkpoint_phase: str,
    ) -> WorkerJobSnapshot:
        self._verify(job_id, lease_token)
        _ = (cancel_request_id, generation, checkpoint_phase)
        self.snapshot = replace(
            self.snapshot,
            status=WorkerJobStatus.CANCELLED,
            lease_worker_id=None,
            lease_token=None,
            lease_expires_at=None,
        )
        return self.snapshot

    async def complete_job(
        self, job_id: UUID, lease_token: UUID, result: dict[str, object]
    ) -> WorkerJobSnapshot:
        self._verify(job_id, lease_token)
        self.completed = result
        self.snapshot = replace(
            self.snapshot,
            status=WorkerJobStatus.SUCCEEDED,
            lease_worker_id=None,
            lease_token=None,
            lease_expires_at=None,
        )
        return self.snapshot

    async def fail_job(
        self,
        job_id: UUID,
        lease_token: UUID,
        *,
        error_code: str,
        retryable: bool,
        outcome_ambiguous: bool = False,
    ) -> WorkerJobSnapshot:
        self._verify(job_id, lease_token)
        _ = outcome_ambiguous
        self.failures.append((error_code, retryable))
        self.snapshot = replace(
            self.snapshot,
            status=(
                WorkerJobStatus.FAILED_RETRYABLE if retryable else WorkerJobStatus.FAILED_FINAL
            ),
            lease_worker_id=None,
            lease_token=None,
            lease_expires_at=None,
        )
        return self.snapshot

    async def request_intervention(
        self,
        job_id: UUID,
        lease_token: UUID,
        *,
        intervention_type: str,
        detail_code: str,
    ) -> WorkerJobSnapshot:
        self._verify(job_id, lease_token)
        self.interventions.append((intervention_type, detail_code))
        self.snapshot = replace(
            self.snapshot,
            status=WorkerJobStatus.WAITING_INTERVENTION,
            lease_worker_id=None,
            lease_token=None,
            lease_expires_at=None,
        )
        return self.snapshot

    def _verify(self, job_id: UUID, lease_token: UUID) -> None:
        if job_id != self.snapshot.job_id or lease_token != self.snapshot.lease_token:
            raise WorkerControlClientError("WORKER_JOB_LEASE_LOST")


class _MemorySessionManager:
    def __init__(
        self,
        worker_id: UUID,
        account_id: UUID,
        *,
        session_state: BrowserSessionState = BrowserSessionState.AUTHENTICATED,
        has_composer: bool = True,
        composer_error: BrowserAdapterError | None = None,
        stage_failure: str | None = None,
    ) -> None:
        now = datetime.now(UTC)
        self.context = WorkerAccountContext(account_id, worker_id, "profile-main", None)
        self.opened = BrowserSessionOpenResult(
            LocalSessionState(
                account_id,
                "profile-main",
                uuid4(),
                session_state,
                1,
                now,
            ),
            WorkerNetworkRoute(
                account_id,
                BrowserNetworkRoute(BrowserNetworkProtocol.DIRECT, None, None),
            ),
        )
        self.engine = _MemoryEngineSession(
            has_composer=has_composer,
            composer_error=composer_error,
            stage_failure=stage_failure,
        )
        self.open_error: Exception | None = None
        self.session = WorkerBrowserSession(
            account_id,
            self.opened,
            self.engine,
            self.transition,
            self.close,
            None,
            lambda _: None,
        )

    async def open(self, context: WorkerAccountContext) -> BrowserSessionOpenResult:
        self.context = context
        if self.open_error is not None:
            raise self.open_error
        return self.opened

    def browser_session(self, account_id: UUID) -> WorkerBrowserSession | None:
        return self.session if account_id == self.context.account_id else None

    async def transition(self, account_id: UUID, state: BrowserSessionState) -> LocalSessionState:
        assert account_id == self.context.account_id
        self.opened = replace(self.opened, state=replace(self.opened.state, state=state))
        return self.opened.state

    async def close(self, account_id: UUID) -> LocalSessionState:
        assert account_id == self.context.account_id
        return await self.transition(account_id, BrowserSessionState.STOPPED)


class _MemoryEngineSession:
    def __init__(
        self,
        *,
        has_composer: bool,
        composer_error: BrowserAdapterError | None,
        stage_failure: str | None,
    ) -> None:
        self.has_composer = has_composer
        self.composer_error = composer_error
        self.stage_failure = stage_failure
        self.events: list[str] = []
        self.selection_count = 0

    async def navigate(self, url: str, *, allowed_origins: frozenset[str]) -> None:
        raise AssertionError("media upload must not navigate or open the composer")

    async def inspect_surface(self) -> BrowserSurface:
        raise AssertionError("media upload must use the existing authenticated session state")

    async def prepare_media_composer(self) -> PreparedMediaComposer | None:
        if self.composer_error is not None:
            raise self.composer_error
        if not self.has_composer:
            return None
        self.events.append("composer_prepared")
        return PreparedMediaComposer(uuid4())

    async def stage_local_media(self, composer: PreparedMediaComposer, file_path: Path) -> None:
        _ = (composer, file_path)
        self.events.append("network_observer_armed")
        self.selection_count += 1
        self.events.append("file_selected")
        if self.stage_failure == "wait":
            await asyncio.Event().wait()
        if self.stage_failure == "non_200":
            raise MediaUploadFailed()
        if self.stage_failure == "missing_preview":
            raise MediaUploadFailed()
        if self.stage_failure in {"network_ambiguity", "missing_completion", "duplicate_request"}:
            raise MediaUploadFailed()
        if self.stage_failure == "timeout":
            raise TimeoutError
        if self.stage_failure == "crash":
            raise BrowserProcessCrashed()
        if self.stage_failure == "off_origin":
            raise RemoteSessionStateUncertain()
        self.events.append("upload_response_200")
        self.events.append("preview_verified")

    async def discard_media_composer(self, composer: PreparedMediaComposer) -> None:
        _ = composer

    async def close(self) -> None:
        return None


class _MemoryLocalState:
    def __init__(self) -> None:
        self.active: dict[UUID, LocalRecoveryEntry] = {}
        self.saved_phases: list[str] = []
        self.cleared_job_ids: list[UUID] = []

    def save_recovery_entry(self, entry: LocalRecoveryEntry) -> None:
        self.active[entry.worker_job_id] = entry
        self.saved_phases.append(entry.phase)

    def recovery_entries(self) -> list[LocalRecoveryEntry]:
        return list(self.active.values())

    def clear_recovery_entry(self, worker_job_id: UUID) -> None:
        self.active.pop(worker_job_id, None)
        self.cleared_job_ids.append(worker_job_id)
