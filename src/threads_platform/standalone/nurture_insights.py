"""Read-only, account-relative feedback from immutable Threads post insights."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID

from threads_platform.application.ports.threads import (
    THREAD_POST_INSIGHT_ORDER,
    ThreadPostInsightName,
    ThreadPostInsights,
)
from threads_platform.standalone.accounts import LocalAccount
from threads_platform.standalone.api import LocalThreadsApiRuntime, StandaloneApiError
from threads_platform.standalone.mutations import LocalOperationStore, StandaloneMutationError
from threads_platform.standalone.nurture import NurturePresetV1
from threads_platform.standalone.nurture_content import content_fingerprint
from threads_platform.standalone.nurture_store import (
    MAX_NURTURE_INSIGHTS_SNAPSHOTS_PER_CONTENT,
    NurtureAccountLock,
    NurtureContentRecordV1,
    NurtureInsightsSnapshotV1,
    NurtureStateError,
    NurtureStore,
)

MAX_FETCHES_PER_REFRESH = 5
MIN_SNAPSHOT_SPACING = timedelta(hours=6)
BASELINE_WINDOW = 20
MIN_BASELINE_SAMPLE = 10

_SAFE_CODE = re.compile(r"[A-Z][A-Z0-9_]{0,63}\Z")
_REMOTE_MEDIA_ID = re.compile(r"[A-Za-z0-9._:-]{1,255}\Z")
_BUCKETS = ("BELOW_BASELINE", "BASELINE", "ABOVE_BASELINE", "TOP_BUCKET")
PerformanceBucket = Literal[
    "INSUFFICIENT_DATA", "BELOW_BASELINE", "BASELINE", "ABOVE_BASELINE", "TOP_BUCKET"
]


class NurtureInsightsError(StandaloneApiError):
    """Bounded refresh failure without remote or local sensitive data."""

    code: str

    def __init__(self, code: str) -> None:
        if _SAFE_CODE.fullmatch(code) is None:
            code = "INSIGHTS_REFRESH_FAILED"
        super().__init__(code)

    def __repr__(self) -> str:
        return f"NurtureInsightsError(code={self.code})"


@dataclass(frozen=True, slots=True)
class NurtureContentPerformance:
    content_fingerprint: str
    category: str
    interaction_score: int | None
    bucket: PerformanceBucket


@dataclass(frozen=True, slots=True)
class NurtureInsightsResult:
    refreshed: int
    skipped_spacing: int
    scoreable: int
    insufficient: int
    below: int
    baseline: int
    above: int
    top: int
    performances: tuple[NurtureContentPerformance, ...]


@dataclass(frozen=True, slots=True, repr=False)
class _LinkedContent:
    record: NurtureContentRecordV1
    fingerprint: str
    media_id: str
    operation_id: UUID
    snapshots: tuple[NurtureInsightsSnapshotV1, ...]

    def __repr__(self) -> str:
        return (
            f"_LinkedContent(fingerprint={self.fingerprint[:12]}…, snapshots={len(self.snapshots)})"
        )


def insights_snapshot_id(content_fp: str, observed_at: datetime) -> str:
    """Deterministic identity for one immutable observation instant."""
    timestamp = _utc(observed_at).isoformat(timespec="microseconds").replace("+00:00", "Z")
    return hashlib.sha256(
        b"threads-nurture-insights-snapshot:v1\x00" + f"{content_fp}\n{timestamp}".encode("ascii")
    ).hexdigest()


def score_interactions(snapshot: NurtureInsightsSnapshotV1) -> int | None:
    """Score only complete four-metric snapshots; null is never zero."""
    values = (snapshot.likes, snapshot.replies, snapshot.reposts, snapshot.quotes)
    if any(value is None for value in values):
        return None
    return sum(value for value in values if value is not None)


def classify_performance(
    records: tuple[NurtureContentRecordV1, ...],
    snapshots: tuple[NurtureInsightsSnapshotV1, ...],
    *,
    account_id: UUID,
    preset_id: str,
) -> tuple[NurtureContentPerformance, ...]:
    """Classify items against the latest 20 same-account, same-preset items."""
    latest: dict[str, NurtureInsightsSnapshotV1] = {}
    for snapshot in snapshots:
        if snapshot.account_id != account_id or snapshot.preset_id != preset_id:
            continue
        current = latest.get(snapshot.content_fingerprint)
        if current is None or (snapshot.observed_at, snapshot.snapshot_id) > (
            current.observed_at,
            current.snapshot_id,
        ):
            latest[snapshot.content_fingerprint] = snapshot

    eligible = [record for record in records if record.publication_state == "PUBLISHED"]
    eligible.sort(
        key=lambda item: content_fingerprint(item.source_fingerprint, item.draft_fingerprint)
    )
    eligible.sort(
        key=lambda item: _utc(item.last_action_at) if item.last_action_at else _MIN_TIME,
        reverse=True,
    )
    baseline_scores = [
        score
        for record in eligible[:BASELINE_WINDOW]
        if (
            snapshot := latest.get(
                content_fingerprint(record.source_fingerprint, record.draft_fingerprint)
            )
        )
        is not None
        and (score := score_interactions(snapshot)) is not None
    ]
    baseline_ready = len(baseline_scores) >= MIN_BASELINE_SAMPLE

    results: list[NurtureContentPerformance] = []
    for record in eligible:
        fingerprint = content_fingerprint(record.source_fingerprint, record.draft_fingerprint)
        snapshot = latest.get(fingerprint)
        score = None if snapshot is None else score_interactions(snapshot)
        if not baseline_ready or score is None:
            bucket: PerformanceBucket = "INSUFFICIENT_DATA"
        else:
            bucket = _bucket(score, baseline_scores)
        results.append(NurtureContentPerformance(fingerprint, record.category, score, bucket))
    return tuple(results)


class NurtureInsightsService:
    def __init__(
        self,
        api: LocalThreadsApiRuntime,
        nurture_store: NurtureStore,
        operation_store: LocalOperationStore,
    ) -> None:
        self._api = api
        self._nurture_store = nurture_store
        self._operation_store = operation_store

    async def refresh(
        self,
        account: LocalAccount,
        preset: NurturePresetV1,
        *,
        now: datetime | None = None,
    ) -> NurtureInsightsResult:
        captured_now = _utc(datetime.now(UTC) if now is None else now)
        try:
            with self._nurture_store.acquire_account_lock(account.id) as owner:
                linked, skipped_spacing = self._load_eligible(owner, account, preset, captured_now)
                fetchable = [
                    item
                    for item in linked
                    if len(item.snapshots) < MAX_NURTURE_INSIGHTS_SNAPSHOTS_PER_CONTENT
                    and self._is_fetchable(item, captured_now)
                ]
                fetchable.sort(
                    key=lambda item: (
                        item.snapshots != (),
                        item.snapshots[-1].observed_at if item.snapshots else _MIN_TIME,
                        item.fingerprint,
                    )
                )
                selected: list[_LinkedContent] = []
                seen_media: set[str] = set()
                for item in fetchable:
                    if item.media_id in seen_media:
                        continue
                    seen_media.add(item.media_id)
                    selected.append(item)
                    if len(selected) == MAX_FETCHES_PER_REFRESH:
                        break

                refreshed = 0
                snapshots = [snapshot for item in linked for snapshot in item.snapshots]
                for item in selected:
                    metrics = await self._read_metrics(account.alias, item.media_id)
                    snapshot = NurtureInsightsSnapshotV1(
                        version=1,
                        snapshot_id=insights_snapshot_id(item.fingerprint, captured_now),
                        account_id=account.id,
                        preset_id=preset.id,
                        content_fingerprint=item.fingerprint,
                        source_fingerprint=item.record.source_fingerprint,
                        draft_fingerprint=item.record.draft_fingerprint,
                        category=item.record.category,
                        observed_at=captured_now,
                        likes=metrics[0],
                        replies=metrics[1],
                        reposts=metrics[2],
                        quotes=metrics[3],
                        operation_id=item.operation_id,
                    )
                    try:
                        stored = owner.append_insights_snapshot(preset, snapshot)
                    except NurtureStateError as error:
                        raise NurtureInsightsError(error.code) from None
                    if stored.snapshot_id == snapshot.snapshot_id:
                        refreshed += 1
                        snapshots.append(stored)

                performances = classify_performance(
                    tuple(item.record for item in linked),
                    tuple(snapshots),
                    account_id=account.id,
                    preset_id=preset.id,
                )
                counts = {bucket: 0 for bucket in ("INSUFFICIENT_DATA", *_BUCKETS)}
                scoreable = 0
                for performance in performances:
                    counts[performance.bucket] += 1
                    if performance.interaction_score is not None:
                        scoreable += 1
                return NurtureInsightsResult(
                    refreshed=refreshed,
                    skipped_spacing=skipped_spacing,
                    scoreable=scoreable,
                    insufficient=counts["INSUFFICIENT_DATA"],
                    below=counts["BELOW_BASELINE"],
                    baseline=counts["BASELINE"],
                    above=counts["ABOVE_BASELINE"],
                    top=counts["TOP_BUCKET"],
                    performances=performances,
                )
        except NurtureInsightsError:
            raise
        except NurtureStateError as error:
            raise NurtureInsightsError(error.code) from None

    def _load_eligible(
        self,
        owner: NurtureAccountLock,
        account: LocalAccount,
        preset: NurturePresetV1,
        now: datetime,
    ) -> tuple[list[_LinkedContent], int]:
        state = owner.get_content_state(preset)
        linked: list[_LinkedContent] = []
        skipped_spacing = 0
        for record in state.candidates:
            if record.publication_state != "PUBLISHED":
                continue
            if (
                record.operation_id is None
                or record.last_action_at is None
                or _utc(record.first_seen_at) > now
                or _utc(record.last_action_at) > now
            ):
                continue
            media_id = self._resolve_media_id(account.id, record)
            fingerprint = content_fingerprint(record.source_fingerprint, record.draft_fingerprint)
            snapshots = owner.get_insights_snapshots(preset, fingerprint)
            if snapshots and snapshots[-1].observed_at > now:
                skipped_spacing += 1
                continue
            assert record.operation_id is not None
            linked.append(
                _LinkedContent(record, fingerprint, media_id, record.operation_id, snapshots)
            )
            if snapshots and now - snapshots[-1].observed_at < MIN_SNAPSHOT_SPACING:
                skipped_spacing += 1
        return linked, skipped_spacing

    def _resolve_media_id(self, account_id: UUID, record: NurtureContentRecordV1) -> str:
        try:
            assert record.operation_id is not None
            operation = self._operation_store.get(record.operation_id)
        except StandaloneMutationError:
            raise NurtureInsightsError("INSIGHTS_OPERATION_INVALID") from None
        if (
            operation.id != record.operation_id
            or operation.account_id != account_id
            or operation.kind != "POST_TEXT"
            or operation.phase != "PUBLISHED"
            or type(operation.media_id) is not str
            or _REMOTE_MEDIA_ID.fullmatch(operation.media_id) is None
        ):
            raise NurtureInsightsError("INSIGHTS_OPERATION_INVALID")
        return operation.media_id

    @staticmethod
    def _is_fetchable(item: _LinkedContent, now: datetime) -> bool:
        if not item.snapshots:
            return True
        latest = item.snapshots[-1]
        return now >= latest.observed_at and now - latest.observed_at >= MIN_SNAPSHOT_SPACING

    async def _read_metrics(self, alias: str, media_id: str) -> tuple[int | None, ...]:
        try:
            result = await self._api.post_insights(alias, media_id)
        except Exception as error:
            code = getattr(error, "code", None)
            safe_code = code if type(code) is str and _SAFE_CODE.fullmatch(code) else None
            raise NurtureInsightsError(safe_code or "INSIGHTS_REFRESH_FAILED") from None
        if (
            type(result) is not ThreadPostInsights
            or result.media_id != media_id
            or result.period != "lifetime"
            or type(result.metrics) is not tuple
            or tuple(metric.name for metric in result.metrics) != THREAD_POST_INSIGHT_ORDER
        ):
            raise NurtureInsightsError("THREADS_CONTRACT_INVALID")
        values: list[int | None] = []
        for metric in result.metrics:
            if (
                type(metric.name) is not ThreadPostInsightName
                or (metric.value is not None and type(metric.value) is not int)
                or (metric.value is not None and metric.value < 0)
            ):
                raise NurtureInsightsError("THREADS_CONTRACT_INVALID")
            values.append(metric.value)
        return tuple(values)


_MIN_TIME = datetime.min.replace(tzinfo=UTC)


def _utc(value: datetime | None) -> datetime:
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        raise NurtureInsightsError("INSIGHTS_TIMESTAMP_INVALID")
    return value.astimezone(UTC)


def _bucket(score: int, baseline_scores: list[int]) -> PerformanceBucket:
    lower = sum(value < score for value in baseline_scores)
    equal = sum(value == score for value in baseline_scores)
    numerator = 100 * (2 * lower + equal)
    denominator = 2 * len(baseline_scores)
    if numerator < 40 * denominator:
        return "BELOW_BASELINE"
    if numerator < 60 * denominator:
        return "BASELINE"
    if numerator < 80 * denominator:
        return "ABOVE_BASELINE"
    return "TOP_BUCKET"
