from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID, uuid4

import pytest

from threads_platform.application.ports.browser import (
    BROWSER_FEED_ORIGIN,
    BrowserContractError,
    BrowserNetworkProtocol,
    BrowserNetworkRoute,
    BrowserSurface,
    FeedAncestorObservation,
    FeedCandidateObservation,
    RemoteSessionStateUncertain,
)
from threads_platform.application.ports.worker_agent import (
    LocalSessionState,
    WorkerAccountContext,
    WorkerJobCancelSnapshot,
    WorkerJobSnapshot,
)
from threads_platform.domain.worker_jobs import WorkerJobRetrySafety, WorkerJobStatus
from threads_platform.domain.workers import BrowserSessionState
from threads_platform.workers.browser import (
    BrowserNavigationPolicy,
    WorkerBrowserSession,
    WorkerJobExecution,
    WorkerJobLeaseLost,
)
from threads_platform.workers.control_client import WorkerControlClientError
from threads_platform.workers.feed_browse import (
    FEED_ANCESTOR_BOUND,
    FEED_CANDIDATE_BOUND,
    FEED_CAPABILITY_NAME,
    FEED_CAPABILITY_VERSION,
    FEED_ITERATION_BOUND,
    BrowserFeedBrowseWorker,
    FeedBrowserSessionManager,
    FeedWorkerControlClient,
    normalize_feed_candidates,
)
from threads_platform.workers.sessions import BrowserSessionOpenResult, WorkerNetworkRoute


def test_permalink_pivot_uses_nearest_unique_bounded_ancestor() -> None:
    item = _candidate("post-1", excerpt="  Hello   feed  ")
    farther = FeedAncestorObservation(
        hrefs=("/@alice/post/post-1", "/@alice/"),
        text_regions=("farther text",),
    )
    observed = replace(item, ancestors=(item.ancestors[0], farther))

    normalized = normalize_feed_candidates((observed,), max_items=5)

    assert len(normalized) == 1
    assert normalized[0].thread_ref == "https://www.threads.com/@alice/post/post-1"
    assert normalized[0].author_username == "alice"
    assert normalized[0].text_excerpt == "Hello feed"
    two_items = normalize_feed_candidates(
        (item, _candidate("post-2", username="bob", excerpt="second item")),
        max_items=5,
    )
    assert len(two_items) == 2
    assert two_items[1].position == 1
    assert two_items[1].author_username == "bob"


@pytest.mark.parametrize(
    "hrefs",
    [
        ("/@alice/post/post-1", "/@alice/post/neighbor", "/@alice/"),
        ("/@alice/post/post-1", "/@alice/post/post-1", "/@alice/"),
        ("/@alice/post/post-1", "/@alice/", "/@alice/"),
    ],
)
def test_ambiguous_or_cross_post_association_fails_closed(
    hrefs: tuple[str, ...],
) -> None:
    candidate = _candidate("post-1", hrefs=hrefs)
    with pytest.raises(BrowserContractError):
        normalize_feed_candidates((candidate,), max_items=5)


def test_missing_or_over_bound_ancestor_fails_closed() -> None:
    candidate = _candidate("post-1")
    no_association = replace(
        candidate,
        ancestors=tuple(FeedAncestorObservation((), ("text",)) for _ in range(FEED_ANCESTOR_BOUND))
        + candidate.ancestors,
    )

    with pytest.raises(BrowserContractError):
        normalize_feed_candidates((no_association,), max_items=5)

    truncated = replace(
        candidate,
        ancestors=(replace(candidate.ancestors[0], links_truncated=True),),
    )
    with pytest.raises(BrowserContractError):
        normalize_feed_candidates((truncated,), max_items=5)

    no_text = replace(
        candidate,
        ancestors=(replace(candidate.ancestors[0], text_regions=()),),
    )
    with pytest.raises(BrowserContractError):
        normalize_feed_candidates((no_text,), max_items=5)


def test_candidate_bounds_and_normalized_deduplication() -> None:
    candidate = _candidate("post-1")
    with pytest.raises(BrowserContractError):
        normalize_feed_candidates(
            tuple(candidate for _ in range(FEED_CANDIDATE_BOUND + 1)), max_items=20
        )
    normalized = normalize_feed_candidates(
        (candidate, replace(candidate, permalink_href="/@ALICE/post/post-1/")),
        max_items=5,
    )
    assert len(normalized) == 1


@pytest.mark.parametrize(
    "input_data",
    [
        {"max_items": True},
        {"max_items": 21},
        {"max_items": 5, "url": "https://example.test"},
    ],
)
@pytest.mark.asyncio
async def test_feed_worker_rejects_invalid_or_unbounded_input(
    input_data: dict[str, object],
) -> None:
    worker_id, account_id = uuid4(), uuid4()
    client = _MemoryControl(worker_id, account_id)
    client.snapshot = replace(client.snapshot, input_data=input_data)
    manager = _MemorySessionManager(worker_id, account_id, batches=[])
    handler = BrowserFeedBrowseWorker(
        worker_id,
        cast(FeedWorkerControlClient, client),
        cast(FeedBrowserSessionManager, manager),
    )

    await handler(client.snapshot)

    assert client.snapshot.status is WorkerJobStatus.FAILED_FINAL
    assert client.failures == [("WORKER_JOB_INPUT_INVALID", False)]
    assert manager.open_count == 0


@pytest.mark.asyncio
async def test_feed_worker_enforces_wall_clock_deadline() -> None:
    worker_id, account_id = uuid4(), uuid4()
    client = _MemoryControl(worker_id, account_id)
    manager = _MemorySessionManager(worker_id, account_id, batches=[])
    ticks = iter((0.0, 31.0))
    handler = BrowserFeedBrowseWorker(
        worker_id,
        cast(FeedWorkerControlClient, client),
        cast(FeedBrowserSessionManager, manager),
        monotonic=lambda: next(ticks),
    )

    await handler(client.snapshot)

    assert client.snapshot.status is WorkerJobStatus.FAILED_RETRYABLE
    assert client.failures == [("BROWSER_NAVIGATION_TIMEOUT", True)]
    assert manager.open_count == 0


@pytest.mark.asyncio
async def test_feed_worker_deduplicates_across_scrolls_and_enforces_iteration_bound() -> None:
    worker_id, account_id = uuid4(), uuid4()
    candidate = _candidate("post-1")
    client = _MemoryControl(worker_id, account_id)
    manager = _MemorySessionManager(
        worker_id,
        account_id,
        batches=[(candidate,), (candidate,)],
    )
    handler = BrowserFeedBrowseWorker(
        worker_id,
        cast(FeedWorkerControlClient, client),
        cast(FeedBrowserSessionManager, manager),
    )

    await handler(client.snapshot)

    assert client.snapshot.status is WorkerJobStatus.SUCCEEDED
    assert manager.session.collect_count == 2
    assert manager.session.scroll_count == 1
    assert client.completed_result is not None
    assert len(cast(list[object], client.completed_result["observations"])) == 1
    assert client.completed_result["truncated"] is True
    await handler.aclose()
    assert manager.closed_accounts == [account_id]

    client = _MemoryControl(worker_id, account_id, max_items=20)
    manager = _MemorySessionManager(
        worker_id,
        account_id,
        batches=[(_candidate(f"post-{index}", username=f"author{index}"),) for index in range(8)],
    )
    handler = BrowserFeedBrowseWorker(
        worker_id,
        cast(FeedWorkerControlClient, client),
        cast(FeedBrowserSessionManager, manager),
    )

    await handler(client.snapshot)

    assert manager.session.collect_count == FEED_ITERATION_BOUND
    assert manager.session.scroll_count == FEED_ITERATION_BOUND - 1
    assert client.completed_result is not None
    assert len(cast(list[object], client.completed_result["observations"])) == FEED_ITERATION_BOUND
    assert client.completed_result["truncated"] is True
    await handler.aclose()

    client = _MemoryControl(worker_id, account_id, max_items=10)
    first_page = tuple(_candidate(f"post-{index}", username=f"first{index}") for index in range(9))
    second_page = tuple(
        _candidate(f"post-{index}", username=f"second{index}") for index in range(9)
    )
    manager = _MemorySessionManager(
        worker_id,
        account_id,
        batches=[first_page, second_page],
    )
    handler = BrowserFeedBrowseWorker(
        worker_id,
        cast(FeedWorkerControlClient, client),
        cast(FeedBrowserSessionManager, manager),
    )

    await handler(client.snapshot)

    assert manager.session.collect_count == 2
    assert client.completed_result is not None
    assert len(cast(list[object], client.completed_result["observations"])) == 10
    await handler.aclose()


@pytest.mark.parametrize("phase", ("BEFORE_NAVIGATION", "FEED_READY", "ITEM_BATCH"))
@pytest.mark.asyncio
async def test_feed_worker_acknowledges_cancel_at_each_safe_checkpoint_without_later_browser_work(
    phase: str,
) -> None:
    worker_id, account_id = uuid4(), uuid4()
    client = _MemoryControl(worker_id, account_id, max_items=20)
    client.cancel_after_checkpoint_phase = phase
    lease_token = client.snapshot.lease_token
    manager = _MemorySessionManager(
        worker_id,
        account_id,
        batches=[(_candidate("post-1"),), (_candidate("post-2", username="bob"),)],
    )
    handler = BrowserFeedBrowseWorker(
        worker_id,
        cast(FeedWorkerControlClient, client),
        cast(FeedBrowserSessionManager, manager),
    )

    await handler(client.snapshot)

    assert client.snapshot.status is WorkerJobStatus.CANCELLED
    assert client.completed_result is None
    assert client.failures == []
    assert client.cancellation_acks == [
        (
            client.snapshot.job_id,
            lease_token,
            client.cancel_request_id,
            client.cancel_generation,
            phase,
        )
    ]
    if phase == "BEFORE_NAVIGATION":
        assert manager.session.navigated == []
        assert manager.session.collect_count == manager.session.scroll_count == 0
    elif phase == "FEED_READY":
        assert len(manager.session.navigated) == 1
        assert manager.session.collect_count == 1
        assert manager.session.scroll_count == 0
    else:
        assert len(manager.session.navigated) == 1
        assert manager.session.collect_count == 2
        assert manager.session.scroll_count == 1


@pytest.mark.asyncio
async def test_feed_worker_requires_authenticated_session_and_account_affinity() -> None:
    worker_id, account_id = uuid4(), uuid4()
    client = _MemoryControl(worker_id, account_id)
    manager = _MemorySessionManager(worker_id, account_id, batches=[])
    client.context = replace(client.context, worker_id=uuid4())
    handler = BrowserFeedBrowseWorker(
        worker_id,
        cast(FeedWorkerControlClient, client),
        cast(FeedBrowserSessionManager, manager),
    )

    await handler(client.snapshot)

    assert client.snapshot.status is WorkerJobStatus.FAILED_FINAL
    assert client.failures[-1] == ("BROWSER_ACCOUNT_AFFINITY_MISMATCH", False)
    assert manager.open_count == 0


@pytest.mark.parametrize(
    ("session_state", "intervention_type"),
    [
        (BrowserSessionState.LOGIN_REQUIRED, "LOGIN_REQUIRED"),
        (BrowserSessionState.SESSION_EXPIRED, "SESSION_EXPIRED"),
        (BrowserSessionState.CHALLENGE_REQUIRED, "CHALLENGE_REQUIRED"),
    ],
)
@pytest.mark.asyncio
async def test_feed_worker_routes_session_states_to_intervention(
    session_state: BrowserSessionState,
    intervention_type: str,
) -> None:
    worker_id, account_id = uuid4(), uuid4()
    client = _MemoryControl(worker_id, account_id)
    manager = _MemorySessionManager(
        worker_id,
        account_id,
        batches=[(_candidate("post-1"),)],
        session_state=session_state,
    )
    handler = BrowserFeedBrowseWorker(
        worker_id,
        cast(FeedWorkerControlClient, client),
        cast(FeedBrowserSessionManager, manager),
    )

    await handler(client.snapshot)

    assert client.snapshot.status is WorkerJobStatus.WAITING_INTERVENTION
    assert client.interventions == [(intervention_type, "SESSION_REQUIRES_OPERATOR")]
    assert manager.session.navigated == []
    await handler.aclose()


@pytest.mark.asyncio
async def test_feed_worker_intervenes_on_session_transition_after_navigation() -> None:
    worker_id, account_id = uuid4(), uuid4()
    client = _MemoryControl(worker_id, account_id)
    manager = _MemorySessionManager(
        worker_id,
        account_id,
        batches=[()],
        transition_after_navigation=True,
    )
    handler = BrowserFeedBrowseWorker(
        worker_id,
        cast(FeedWorkerControlClient, client),
        cast(FeedBrowserSessionManager, manager),
    )

    await handler(client.snapshot)

    assert client.snapshot.status is WorkerJobStatus.WAITING_INTERVENTION
    assert client.interventions == [("REMOTE_STATE_UNCERTAIN", "REMOTE_STATE_UNCERTAIN")]
    assert manager.session.navigated == [f"{BROWSER_FEED_ORIGIN}/"]
    assert manager.session.collect_count == 1
    assert manager.session.scroll_count == 0
    assert client.failures == []
    assert client.completed_result is None
    await handler.aclose()


@pytest.mark.parametrize("operation", ("navigate", "collect", "scroll"))
@pytest.mark.asyncio
async def test_browser_operation_does_not_start_after_worker_job_lease_loss(
    operation: str,
) -> None:
    worker_id, account_id = uuid4(), uuid4()
    client = _MemoryControl(worker_id, account_id)
    client.lose_renew = True
    execution = WorkerJobExecution(
        client.snapshot, worker_id, cast(FeedWorkerControlClient, client)
    )
    opened = BrowserSessionOpenResult(
        LocalSessionState(
            account_id,
            "profile-main",
            uuid4(),
            BrowserSessionState.AUTHENTICATED,
            1,
            datetime.now(UTC),
        ),
        WorkerNetworkRoute(
            account_id,
            BrowserNetworkRoute(BrowserNetworkProtocol.DIRECT, None, None),
        ),
    )
    engine = _MemoryEngineSession()

    async def transition(_: UUID, state: BrowserSessionState) -> LocalSessionState:
        return replace(opened.state, state=state)

    async def close(_: UUID) -> LocalSessionState:
        return replace(opened.state, state=BrowserSessionState.STOPPED)

    session = WorkerBrowserSession(
        account_id,
        opened,
        engine,
        transition,
        close,
        execution,
        lambda _: None,
    )

    with pytest.raises(WorkerJobLeaseLost):
        if operation == "navigate":
            await session.navigate(
                f"{BROWSER_FEED_ORIGIN}/",
                BrowserNavigationPolicy(frozenset({BROWSER_FEED_ORIGIN})),
            )
        elif operation == "collect":
            await session.collect_feed_candidates(ancestor_bound=FEED_ANCESTOR_BOUND)
        else:
            await session.scroll_feed()
    assert engine.navigation_count == engine.collect_count == engine.scroll_count == 0


def _candidate(
    post_id: str,
    *,
    username: str = "alice",
    excerpt: str = "visible text",
    hrefs: tuple[str, ...] | None = None,
) -> FeedCandidateObservation:
    permalink = f"/@{username}/post/{post_id}"
    return FeedCandidateObservation(
        permalink_href=permalink,
        ancestors=(
            FeedAncestorObservation(
                hrefs=hrefs or (permalink, f"/@{username}/"),
                text_regions=(excerpt,),
            ),
        ),
    )


class _MemoryControl:
    def __init__(self, worker_id: UUID, account_id: UUID, *, max_items: int = 10) -> None:
        self.worker_id = worker_id
        self.account_id = account_id
        self.snapshot = WorkerJobSnapshot(
            job_id=uuid4(),
            capability_name=FEED_CAPABILITY_NAME,
            capability_version=FEED_CAPABILITY_VERSION,
            status=WorkerJobStatus.RUNNING,
            account_id=account_id,
            assigned_worker_id=worker_id,
            lease_worker_id=worker_id,
            lease_token=uuid4(),
            lease_expires_at=datetime.now(UTC) + timedelta(minutes=2),
            retry_safety=WorkerJobRetrySafety.SAFE_TO_RETRY,
            checkpoint=None,
            input_data={"max_items": max_items},
        )
        self.context = WorkerAccountContext(account_id, worker_id, "profile-main", None)
        self.completed_result: dict[str, object] | None = None
        self.interventions: list[tuple[str, str]] = []
        self.failures: list[tuple[str, bool]] = []
        self.lose_renew = False
        self.cancel_after_checkpoint_phase: str | None = None
        self.cancel_request_id = uuid4()
        self.cancel_generation = 7
        self.cancellation_acks: list[tuple[UUID, UUID, UUID, int, str]] = []

    async def account_context(self, account_id: UUID) -> WorkerAccountContext:
        assert account_id == self.account_id
        return self.context

    async def renew_job(self, job_id: UUID, lease_token: UUID) -> WorkerJobSnapshot:
        self._verify(job_id, lease_token)
        if self.lose_renew:
            raise WorkerControlClientError("WORKER_JOB_LEASE_LOST")
        return self.snapshot

    async def checkpoint_job(
        self, job_id: UUID, lease_token: UUID, checkpoint: dict[str, object]
    ) -> WorkerJobSnapshot:
        self._verify(job_id, lease_token)
        self.snapshot = replace(self.snapshot, checkpoint=checkpoint)
        if checkpoint.get("phase") == self.cancel_after_checkpoint_phase:
            self.snapshot = replace(
                self.snapshot,
                pending_cancel=WorkerJobCancelSnapshot(
                    self.cancel_request_id,
                    self.cancel_generation,
                    "OPERATOR_REQUESTED",
                    datetime.now(UTC),
                ),
            )
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
        self.cancellation_acks.append(
            (job_id, lease_token, cancel_request_id, generation, checkpoint_phase)
        )
        self.snapshot = replace(
            self.snapshot,
            status=WorkerJobStatus.CANCELLED,
            lease_worker_id=None,
            lease_token=None,
            lease_expires_at=None,
            pending_cancel=None,
        )
        return self.snapshot

    async def complete_job(
        self, job_id: UUID, lease_token: UUID, result: dict[str, object]
    ) -> WorkerJobSnapshot:
        self._verify(job_id, lease_token)
        self.completed_result = result
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


class _MemorySession:
    def __init__(
        self,
        account_id: UUID,
        profile_ref: str,
        batches: list[tuple[FeedCandidateObservation, ...]],
        state: BrowserSessionState,
        *,
        transition_after_navigation: bool = False,
    ) -> None:
        self.account_id = account_id
        self.profile_ref = profile_ref
        self.session_state = state
        self.batches = batches
        self.collect_count = 0
        self.scroll_count = 0
        self.navigated: list[str] = []
        self.execution: object | None = None
        self.transition_after_navigation = transition_after_navigation

    def bind_worker_job(self, execution: object) -> None:
        self.execution = execution

    async def navigate(self, url: str, policy: object) -> None:
        _ = policy
        self.navigated.append(url)

    async def collect_feed_candidates(
        self, *, ancestor_bound: int
    ) -> tuple[FeedCandidateObservation, ...]:
        assert ancestor_bound == FEED_ANCESTOR_BOUND
        index = self.collect_count
        self.collect_count += 1
        if self.transition_after_navigation and self.navigated:
            execution = cast(WorkerJobExecution, self.execution)
            await execution.request_intervention("REMOTE_STATE_UNCERTAIN", "REMOTE_STATE_UNCERTAIN")
            raise RemoteSessionStateUncertain()
        return self.batches[index] if index < len(self.batches) else ()

    async def scroll_feed(self) -> None:
        self.scroll_count += 1


class _MemorySessionManager:
    def __init__(
        self,
        worker_id: UUID,
        account_id: UUID,
        *,
        batches: list[tuple[FeedCandidateObservation, ...]],
        session_state: BrowserSessionState = BrowserSessionState.AUTHENTICATED,
        transition_after_navigation: bool = False,
    ) -> None:
        self.context = WorkerAccountContext(account_id, worker_id, "profile-main", None)
        now = datetime.now(UTC)
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
        self.session = _MemorySession(
            account_id,
            "profile-main",
            batches,
            session_state,
            transition_after_navigation=transition_after_navigation,
        )
        self.open_count = 0
        self.closed_accounts: list[UUID] = []

    async def open(self, context: WorkerAccountContext) -> BrowserSessionOpenResult:
        self.open_count += 1
        self.context = context
        return self.opened

    def browser_session(self, account_id: UUID) -> object | None:
        return self.session if account_id == self.context.account_id else None

    async def close(self, account_id: UUID) -> object:
        self.closed_accounts.append(account_id)
        return None


class _MemoryEngineSession:
    def __init__(self) -> None:
        self.navigation_count = 0
        self.collect_count = 0
        self.scroll_count = 0

    async def navigate(self, url: str, *, allowed_origins: frozenset[str]) -> None:
        _ = (url, allowed_origins)
        self.navigation_count += 1

    async def inspect_surface(self) -> BrowserSurface:
        return BrowserSurface(None, None, None, False)

    async def collect_feed_candidates(
        self, *, ancestor_bound: int
    ) -> tuple[FeedCandidateObservation, ...]:
        _ = ancestor_bound
        self.collect_count += 1
        return ()

    async def scroll_feed(self) -> None:
        self.scroll_count += 1

    async def close(self) -> None:
        return None
