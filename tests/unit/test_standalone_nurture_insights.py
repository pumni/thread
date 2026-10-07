from __future__ import annotations

import json
import os
import stat
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID, uuid4

import pytest

import threads_platform.standalone.__main__ as cli_module
import threads_platform.standalone.nurture_insights as nurture_insights_module
import threads_platform.standalone.nurture_store as nurture_store_module
from threads_platform.application.ports.threads import (
    THREAD_POST_INSIGHT_ORDER,
    ThreadPostInsightMetric,
    ThreadPostInsights,
)
from threads_platform.standalone.accounts import LocalAccount, LocalAccountStore
from threads_platform.standalone.api import LocalThreadsApiRuntime
from threads_platform.standalone.mutations import LocalOperationStore
from threads_platform.standalone.nurture import NurturePresetV1, get_nurture_preset
from threads_platform.standalone.nurture_content import (
    ContentCandidateV1,
    ContentCategory,
    content_fingerprint,
    draft_fingerprint,
    source_fingerprint,
)
from threads_platform.standalone.nurture_insights import (
    MAX_FETCHES_PER_REFRESH,
    MIN_SNAPSHOT_SPACING,
    NurtureInsightsError,
    NurtureInsightsService,
    classify_performance,
    insights_snapshot_id,
    score_interactions,
)
from threads_platform.standalone.nurture_store import (
    NurtureContentRecordV1,
    NurtureInsightsSnapshotV1,
    NurtureStateError,
    NurtureStore,
)

_NOW = datetime(2026, 10, 7, 4, 0, tzinfo=UTC)
_SOURCE_SENTINEL = "INSIGHTS_SOURCE_SENTINEL"
_DRAFT_SENTINEL = "INSIGHTS_DRAFT_SENTINEL"
_MEDIA_SENTINEL = "INSIGHTS_REMOTE_MEDIA_SENTINEL"
_TOKEN_SENTINEL = "INSIGHTS_TOKEN_SENTINEL"


class _FakeApi:
    def __init__(self, *, values: tuple[int | None, ...] = (1, 2, 3, 4)) -> None:
        self.values = values
        self.calls: list[tuple[str, str]] = []
        self.fail_at: int | None = None

    async def post_insights(self, alias: str, media_id: str) -> ThreadPostInsights:
        self.calls.append((alias, media_id))
        if self.fail_at == len(self.calls):
            raise RuntimeError(_TOKEN_SENTINEL)
        return ThreadPostInsights(
            media_id,
            "lifetime",
            tuple(
                ThreadPostInsightMetric(name, value)
                for name, value in zip(THREAD_POST_INSIGHT_ORDER, self.values, strict=True)
            ),
        )


def _setup(tmp_path: Path) -> tuple[Path, LocalAccount, NurtureStore, NurturePresetV1]:
    root = tmp_path / "standalone"
    account = LocalAccountStore(root).add("alice")
    return root, account, NurtureStore(root), get_nurture_preset("recruitment")


def _published(
    root: Path,
    store: NurtureStore,
    account: LocalAccount,
    preset: NurturePresetV1,
    *,
    source_id: str = _SOURCE_SENTINEL,
    text: str = _DRAFT_SENTINEL,
    media_id: str = "remote-media-1",
    action_at: datetime = _NOW,
    operation_kind: str = "POST_TEXT",
    operation_account_id: UUID | None = None,
    operation_phase: str = "PUBLISHED",
    create_operation: bool = True,
) -> tuple[NurtureContentRecordV1, UUID]:
    operations = LocalOperationStore(root)
    operation_id = uuid4()
    if create_operation:
        operation = operations.create_received(
            operation_account_id or account.id,
            kind=operation_kind,
        )
        operation_id = operation.id
        if operation_phase == "PUBLISHED":
            operation = operations.update(
                replace(operation, phase="CONTAINER_CREATED", container_id="container-1")
            )
            operation = operations.update(replace(operation, phase="PUBLISH_REQUESTED"))
            operation = operations.update(replace(operation, phase="PUBLISHED", media_id=media_id))
        elif operation_phase != "RECEIVED":
            raise AssertionError("unsupported test operation phase")

    candidate = ContentCandidateV1(
        candidate_id=uuid4(),
        account_alias=account.alias,
        preset_id=preset.id,
        source_id=source_id,
        category=cast(ContentCategory, "CAREER_TIP"),
        text=text,
    )
    with store.acquire_account_lock(account.id) as owner:
        run = owner.start_run(preset, now=action_at)
        owner.register_content_candidate(
            preset,
            candidate.candidate_id,
            candidate.source_fingerprint,
            candidate.draft_fingerprint,
            candidate.category,
            run.receipt.id,
            now=action_at,
        )
        owner.reserve_content_candidate(
            preset,
            candidate.source_fingerprint,
            candidate.draft_fingerprint,
            run.receipt.id,
        )
        record = owner.complete_content_candidate(
            preset,
            candidate.source_fingerprint,
            candidate.draft_fingerprint,
            run.receipt.id,
            "PUBLISHED",
            operation_id,
            now=action_at,
        )
        run.finish("SUCCESS", now=action_at)
    return record, operation_id


def _snapshot(
    record: NurtureContentRecordV1,
    account_id: UUID,
    preset_id: str,
    *,
    observed_at: datetime,
    values: tuple[int | None, int | None, int | None, int | None] = (1, 2, 3, 4),
) -> NurtureInsightsSnapshotV1:
    fingerprint = content_fingerprint(record.source_fingerprint, record.draft_fingerprint)
    assert record.operation_id is not None
    return NurtureInsightsSnapshotV1(
        version=1,
        snapshot_id=insights_snapshot_id(fingerprint, observed_at),
        account_id=account_id,
        preset_id=preset_id,
        content_fingerprint=fingerprint,
        source_fingerprint=record.source_fingerprint,
        draft_fingerprint=record.draft_fingerprint,
        category=record.category,
        observed_at=observed_at,
        likes=values[0],
        replies=values[1],
        reposts=values[2],
        quotes=values[3],
        operation_id=record.operation_id,
    )


def _service(root: Path, store: NurtureStore, api: _FakeApi) -> NurtureInsightsService:
    return NurtureInsightsService(
        cast(LocalThreadsApiRuntime, api), store, LocalOperationStore(root)
    )


@pytest.mark.asyncio
async def test_refresh_resolves_only_published_media_through_local_operation_journal(
    tmp_path: Path,
) -> None:
    root, account, store, preset = _setup(tmp_path)
    _published(root, store, account, preset)
    api = _FakeApi()

    result = await _service(root, store, api).refresh(account, preset, now=_NOW)

    assert api.calls == [("alice", "remote-media-1")]
    assert result.refreshed == 1
    assert result.scoreable == 1
    assert result.performances[0].interaction_score == 10
    assert result.performances[0].bucket == "INSUFFICIENT_DATA"


@pytest.mark.parametrize(
    "fault",
    ["missing", "wrong_account", "wrong_kind", "wrong_phase", "missing_media"],
)
@pytest.mark.asyncio
async def test_invalid_operation_link_fails_before_remote_request(
    tmp_path: Path,
    fault: str,
) -> None:
    root, account, store, preset = _setup(tmp_path)
    options: dict[str, object] = {}
    if fault == "missing":
        options["create_operation"] = False
    elif fault == "wrong_account":
        options["operation_account_id"] = uuid4()
    elif fault == "wrong_kind":
        options["operation_kind"] = "POST_IMAGE"
    elif fault == "wrong_phase":
        options["operation_phase"] = "RECEIVED"
    elif fault == "missing_media":
        options["media_id"] = ""
    if fault == "missing_media":
        options["media_id"] = "remote-media-1"
    record, operation_id = _published(root, store, account, preset, **cast(Any, options))
    if fault == "missing_media":
        operation_path = root / "operations" / f"{operation_id}.json"
        document = json.loads(operation_path.read_text(encoding="utf-8"))
        document["media_id"] = None
        operation_path.write_text(json.dumps(document), encoding="utf-8")
    assert record.operation_id == operation_id
    api = _FakeApi()

    with pytest.raises(NurtureInsightsError) as error:
        await _service(root, store, api).refresh(account, preset, now=_NOW)

    assert error.value.code == "INSIGHTS_OPERATION_INVALID"
    assert api.calls == []
    assert _TOKEN_SENTINEL not in str(error.value)
    assert _MEDIA_SENTINEL not in repr(error.value)


@pytest.mark.asyncio
async def test_non_published_content_states_are_never_fetched(tmp_path: Path) -> None:
    root, account, store, preset = _setup(tmp_path)
    with store.acquire_account_lock(account.id) as owner:
        run = owner.start_run(preset, now=_NOW)
        for index, state in enumerate(("NEW", "RESERVED", "AMBIGUOUS", "REJECTED")):
            candidate = ContentCandidateV1(
                uuid4(),
                account.alias,
                preset.id,
                f"source-{index}",
                cast(ContentCategory, "CAREER_TIP"),
                f"draft-{index}",
            )
            record = owner.register_content_candidate(
                preset,
                candidate.candidate_id,
                candidate.source_fingerprint,
                candidate.draft_fingerprint,
                candidate.category,
                run.receipt.id,
                now=_NOW,
            )
            if state in {"RESERVED", "AMBIGUOUS"}:
                owner.reserve_content_candidate(
                    preset,
                    record.source_fingerprint,
                    record.draft_fingerprint,
                    run.receipt.id,
                )
            if state == "AMBIGUOUS":
                owner.complete_content_candidate(
                    preset,
                    record.source_fingerprint,
                    record.draft_fingerprint,
                    run.receipt.id,
                    "AMBIGUOUS",
                    uuid4(),
                    now=_NOW,
                )
            if state == "REJECTED":
                document = owner.get_content_state(preset)
                rejected = replace(record, publication_state="REJECTED")
                store.save_content_state(
                    owner,
                    replace(
                        document,
                        candidates=tuple(
                            rejected if item.candidate_id == record.candidate_id else item
                            for item in document.candidates
                        ),
                    ),
                )
        run.finish("SUCCESS", now=_NOW)
    api = _FakeApi()

    result = await _service(root, store, api).refresh(account, preset, now=_NOW)

    assert api.calls == []
    assert result.refreshed == 0
    assert result.performances == ()


@pytest.mark.asyncio
async def test_refresh_caps_calls_and_uses_no_snapshot_then_oldest_order(tmp_path: Path) -> None:
    root, account, store, preset = _setup(tmp_path)
    records = [
        _published(
            root,
            store,
            account,
            preset,
            source_id=f"source-{index}",
            text=f"draft-{index}",
            media_id=f"media-{index}",
        )[0]
        for index in range(MAX_FETCHES_PER_REFRESH + 2)
    ]
    expected = sorted(
        (
            content_fingerprint(record.source_fingerprint, record.draft_fingerprint),
            f"media-{index}",
        )
        for index, record in enumerate(records)
    )[:MAX_FETCHES_PER_REFRESH]
    api = _FakeApi()

    result = await _service(root, store, api).refresh(account, preset, now=_NOW)

    assert api.calls == [("alice", media_id) for _, media_id in expected]
    assert result.refreshed == MAX_FETCHES_PER_REFRESH


@pytest.mark.asyncio
async def test_snapshot_capped_content_stays_classifiable_and_does_not_block_refresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cap = 2
    monkeypatch.setattr(nurture_insights_module, "MAX_NURTURE_INSIGHTS_SNAPSHOTS_PER_CONTENT", cap)
    monkeypatch.setattr(nurture_store_module, "MAX_NURTURE_INSIGHTS_SNAPSHOTS_PER_CONTENT", cap)
    root, account, store, preset = _setup(tmp_path)
    capped_record, _ = _published(
        root,
        store,
        account,
        preset,
        source_id="capped-source",
        text="capped-draft",
        media_id="capped-media",
    )
    uncapped_record, _ = _published(
        root,
        store,
        account,
        preset,
        source_id="uncapped-source",
        text="uncapped-draft",
        media_id="uncapped-media",
    )
    with store.acquire_account_lock(account.id) as owner:
        for index in range(cap):
            owner.append_insights_snapshot(
                preset,
                _snapshot(
                    capped_record,
                    account.id,
                    preset.id,
                    observed_at=_NOW - timedelta(hours=6 * (cap - index)),
                ),
            )
        snapshots_before = owner.get_insights_snapshots(
            preset,
            content_fingerprint(capped_record.source_fingerprint, capped_record.draft_fingerprint),
        )
        assert len(snapshots_before) == cap
    api = _FakeApi(values=(5, 4, 3, 2))

    result = await _service(root, store, api).refresh(account, preset, now=_NOW)

    assert api.calls == [("alice", "uncapped-media")]
    assert result.refreshed == 1
    with store.acquire_account_lock(account.id) as owner:
        capped_after = owner.get_insights_snapshots(
            preset,
            content_fingerprint(capped_record.source_fingerprint, capped_record.draft_fingerprint),
        )
        uncapped_after = owner.get_insights_snapshots(
            preset,
            content_fingerprint(
                uncapped_record.source_fingerprint, uncapped_record.draft_fingerprint
            ),
        )
    assert capped_after == snapshots_before
    assert len(uncapped_after) == 1
    performances = {item.content_fingerprint: item for item in result.performances}
    capped_fingerprint = content_fingerprint(
        capped_record.source_fingerprint, capped_record.draft_fingerprint
    )
    assert performances[capped_fingerprint].interaction_score == 10
    assert performances[capped_fingerprint].bucket == "INSUFFICIENT_DATA"


@pytest.mark.asyncio
async def test_refresh_enforces_six_hour_spacing_and_boundary_is_eligible(tmp_path: Path) -> None:
    root, account, store, preset = _setup(tmp_path)
    _published(root, store, account, preset)
    api = _FakeApi()
    service = _service(root, store, api)

    first = await service.refresh(account, preset, now=_NOW)
    recent = await service.refresh(
        account, preset, now=_NOW + MIN_SNAPSHOT_SPACING - timedelta(seconds=1)
    )
    due = await service.refresh(account, preset, now=_NOW + MIN_SNAPSHOT_SPACING)

    assert first.refreshed == 1
    assert recent.refreshed == 0 and recent.skipped_spacing == 1
    assert due.refreshed == 1
    assert len(api.calls) == 2


@pytest.mark.asyncio
async def test_future_snapshot_is_suppressed_for_fetch_and_feedback(tmp_path: Path) -> None:
    root, account, store, preset = _setup(tmp_path)
    record, _ = _published(root, store, account, preset)
    future_snapshot = _snapshot(
        record,
        account.id,
        preset.id,
        observed_at=_NOW + timedelta(seconds=1),
    )
    with store.acquire_account_lock(account.id) as owner:
        owner.append_insights_snapshot(preset, future_snapshot)
    api = _FakeApi()

    result = await _service(root, store, api).refresh(account, preset, now=_NOW)

    assert api.calls == []
    assert result.skipped_spacing == 1
    assert result.performances == ()


@pytest.mark.asyncio
async def test_existing_snapshots_refresh_oldest_first(tmp_path: Path) -> None:
    root, account, store, preset = _setup(tmp_path)
    records = [
        _published(
            root,
            store,
            account,
            preset,
            source_id=f"old-order-source-{index}",
            text=f"old-order-draft-{index}",
            media_id=f"old-order-media-{index}",
        )[0]
        for index in range(2)
    ]
    with store.acquire_account_lock(account.id) as owner:
        for record, observed_at in (
            (records[0], _NOW - timedelta(days=1)),
            (records[1], _NOW - timedelta(days=2)),
        ):
            owner.append_insights_snapshot(
                preset,
                _snapshot(record, account.id, preset.id, observed_at=observed_at),
            )
    api = _FakeApi()

    await _service(root, store, api).refresh(account, preset, now=_NOW)

    assert api.calls == [("alice", "old-order-media-1"), ("alice", "old-order-media-0")]


@pytest.mark.asyncio
async def test_duplicate_media_is_requested_once_per_refresh(tmp_path: Path) -> None:
    root, account, store, preset = _setup(tmp_path)
    _published(root, store, account, preset, source_id="source-a", text="draft-a")
    _published(root, store, account, preset, source_id="source-b", text="draft-b")
    api = _FakeApi()

    result = await _service(root, store, api).refresh(account, preset, now=_NOW)

    assert api.calls == [("alice", "remote-media-1")]
    assert result.refreshed == 1


@pytest.mark.asyncio
async def test_api_failure_stops_and_preserves_prior_immutable_snapshot(tmp_path: Path) -> None:
    root, account, store, preset = _setup(tmp_path)
    _published(
        root, store, account, preset, source_id="source-a", text="draft-a", media_id="media-a"
    )
    _published(
        root, store, account, preset, source_id="source-b", text="draft-b", media_id="media-b"
    )
    api = _FakeApi()
    api.fail_at = 2

    with pytest.raises(NurtureInsightsError) as error:
        await _service(root, store, api).refresh(account, preset, now=_NOW)

    assert error.value.code == "INSIGHTS_REFRESH_FAILED"
    assert len(api.calls) == 2
    snapshot_files = list((root / "nurture" / "insights").rglob("*.json"))
    assert len(snapshot_files) == 1
    text = snapshot_files[0].read_text(encoding="utf-8")
    assert _SOURCE_SENTINEL not in text
    assert _DRAFT_SENTINEL not in text
    assert _MEDIA_SENTINEL not in text
    assert _TOKEN_SENTINEL not in text
    assert _TOKEN_SENTINEL not in repr(error.value)


def test_snapshot_append_is_atomic_immutable_and_idempotent(tmp_path: Path) -> None:
    root, account, store, preset = _setup(tmp_path)
    record, _ = _published(root, store, account, preset, media_id=_MEDIA_SENTINEL)
    fingerprint = content_fingerprint(record.source_fingerprint, record.draft_fingerprint)
    snapshot = _snapshot(record, account.id, preset.id, observed_at=_NOW)

    with store.acquire_account_lock(account.id) as owner:
        first = owner.append_insights_snapshot(preset, snapshot)
        path = next((root / "nurture" / "insights").rglob("*.json"))
        contents = path.read_bytes()
        second = owner.append_insights_snapshot(preset, snapshot)
        assert first == second
        assert path.read_bytes() == contents
        assert owner.get_insights_snapshots(preset, fingerprint) == (snapshot,)
        changed = replace(snapshot, likes=999)
        with pytest.raises(NurtureStateError):
            owner.append_insights_snapshot(preset, changed)
    assert _SOURCE_SENTINEL not in contents.decode("utf-8")
    assert _DRAFT_SENTINEL not in contents.decode("utf-8")
    assert _MEDIA_SENTINEL not in contents.decode("utf-8")
    assert _TOKEN_SENTINEL not in contents.decode("utf-8")


def test_snapshot_parser_rejects_duplicate_keys_oversize_and_symlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, account, store, preset = _setup(tmp_path)
    record, _ = _published(root, store, account, preset)
    snapshot = _snapshot(record, account.id, preset.id, observed_at=_NOW)
    fingerprint = snapshot.content_fingerprint
    with store.acquire_account_lock(account.id) as owner:
        owner.append_insights_snapshot(preset, snapshot)
        path = next((root / "nurture" / "insights").rglob("*.json"))
        original = path.read_text(encoding="utf-8")
        path.write_text(
            original.replace('"version": 1', '"version": 1, "version": 1'), encoding="utf-8"
        )
        with pytest.raises(NurtureStateError):
            owner.get_insights_snapshots(preset, fingerprint)

        path.write_text(original.replace('"version": 1', '"version": 2'), encoding="utf-8")
        with pytest.raises(NurtureStateError):
            owner.get_insights_snapshots(preset, fingerprint)
        path.write_text(original.replace('"likes": 1', '"likes": true'), encoding="utf-8")
        with pytest.raises(NurtureStateError):
            owner.get_insights_snapshots(preset, fingerprint)
        path.write_text(
            original.replace('"version": 1', '"version": 1, "extra": 2'), encoding="utf-8"
        )
        with pytest.raises(NurtureStateError):
            owner.get_insights_snapshots(preset, fingerprint)
        path.write_text(original, encoding="utf-8")

        with pytest.raises(NurtureStateError):
            owner.get_insights_snapshots(preset, "../invalid")

        original_lstat = Path.lstat
        symlink_metadata = os.stat_result((stat.S_IFLNK | 0o777, 0, 0, 1, 0, 0, 0, 0, 0, 0))

        def fake_lstat(candidate: Path) -> os.stat_result:
            if candidate == path:
                return symlink_metadata
            return original_lstat(candidate)

        monkeypatch.setattr(Path, "lstat", fake_lstat)
        with pytest.raises(NurtureStateError):
            owner.get_insights_snapshots(preset, fingerprint)
        monkeypatch.setattr(Path, "lstat", original_lstat)

        path.write_bytes(b" " * 4097)
        with pytest.raises(NurtureStateError):
            owner.get_insights_snapshots(preset, fingerprint)


def test_failed_snapshot_write_does_not_leave_partial_document(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, account, store, preset = _setup(tmp_path)
    record, _ = _published(root, store, account, preset)
    snapshot = _snapshot(record, account.id, preset.id, observed_at=_NOW)

    def fail_fsync(_descriptor: int) -> None:
        raise OSError("fsync failure")

    monkeypatch.setattr(nurture_store_module.os, "fsync", fail_fsync)
    with store.acquire_account_lock(account.id) as owner:
        with pytest.raises(NurtureStateError):
            owner.append_insights_snapshot(preset, snapshot)
    insights_root = root / "nurture" / "insights"
    assert not list(insights_root.rglob("*.json"))
    assert not list(insights_root.rglob("*.tmp"))


@pytest.mark.asyncio
async def test_refresh_lock_contention_fails_before_reads_or_receipts(tmp_path: Path) -> None:
    root, account, store, preset = _setup(tmp_path)
    api = _FakeApi()

    with store.acquire_account_lock(account.id):
        with pytest.raises(NurtureInsightsError) as error:
            await _service(root, store, api).refresh(account, preset, now=_NOW)

    assert error.value.code == "NURTURE_BUSY"
    assert api.calls == []
    assert not (root / "nurture" / "runs").exists()


def _score_record(index: int, action_at: datetime) -> NurtureContentRecordV1:
    return NurtureContentRecordV1(
        candidate_id=uuid4(),
        source_fingerprint=source_fingerprint("OPERATOR_SOURCE_ID", f"score-source-{index}"),
        draft_fingerprint=draft_fingerprint(f"score-draft-{index}"),
        category="CAREER_TIP",
        first_seen_at=action_at - timedelta(days=1),
        last_action_at=action_at,
        publication_state="PUBLISHED",
        last_run_id=uuid4(),
        operation_id=uuid4(),
    )


def test_score_requires_all_metrics_and_sums_exact_four() -> None:
    account_id = uuid4()
    preset_id = "recruitment"
    record = _score_record(0, _NOW)
    complete = _snapshot(record, account_id, preset_id, observed_at=_NOW, values=(1, 2, 3, 4))
    incomplete = _snapshot(record, account_id, preset_id, observed_at=_NOW, values=(1, None, 3, 4))

    assert score_interactions(complete) == 10
    assert score_interactions(incomplete) is None


def test_performance_minimum_sample_midpoint_tie_and_account_scope() -> None:
    account_id = uuid4()
    other_account = uuid4()
    preset_id = "recruitment"
    records = tuple(_score_record(index, _NOW + timedelta(seconds=index)) for index in range(10))
    snapshots = tuple(
        _snapshot(record, account_id, preset_id, observed_at=_NOW, values=(index, 0, 0, 0))
        for index, record in enumerate(records)
    )
    midpoint = classify_performance(records, snapshots, account_id=account_id, preset_id=preset_id)
    assert midpoint == classify_performance(
        records, snapshots, account_id=account_id, preset_id=preset_id
    )
    assert midpoint[4].bucket == "BASELINE"  # 4 below + half of the equal observation.

    nine = classify_performance(
        records[:9], snapshots[:9], account_id=account_id, preset_id=preset_id
    )
    assert all(item.bucket == "INSUFFICIENT_DATA" for item in nine)

    all_equal = tuple(
        _snapshot(record, account_id, preset_id, observed_at=_NOW, values=(5, 0, 0, 0))
        for record in records
    )
    equal_result = classify_performance(
        records, all_equal, account_id=account_id, preset_id=preset_id
    )
    assert {item.bucket for item in equal_result} == {"BASELINE"}

    only_other_account = tuple(
        replace(snapshot, account_id=other_account) for snapshot in snapshots
    )
    isolated = classify_performance(
        records, only_other_account, account_id=account_id, preset_id=preset_id
    )
    assert all(item.bucket == "INSUFFICIENT_DATA" for item in isolated)


def test_baseline_uses_latest_twenty_eligible_published_items() -> None:
    account_id = uuid4()
    records = tuple(_score_record(index, _NOW + timedelta(seconds=index)) for index in range(22))
    snapshots = tuple(
        _snapshot(
            record,
            account_id,
            "recruitment",
            observed_at=_NOW,
            values=((0 if index == 0 else index), 0, 0, 0),
        )
        for index, record in enumerate(records)
    )

    results = classify_performance(
        records, snapshots, account_id=account_id, preset_id="recruitment"
    )

    old_first = next(
        result
        for result in results
        if result.content_fingerprint
        == content_fingerprint(records[0].source_fingerprint, records[0].draft_fingerprint)
    )
    assert old_first.bucket == "BELOW_BASELINE"


def test_insights_cli_composes_api_only_and_creates_no_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root, account, store, preset = _setup(tmp_path)
    _published(
        root,
        store,
        account,
        preset,
        source_id=_SOURCE_SENTINEL,
        text=_DRAFT_SENTINEL,
        media_id=_MEDIA_SENTINEL,
    )
    run_receipts_before = tuple((root / "nurture" / "runs").rglob("*.json"))
    calls: list[dict[str, object]] = []

    @asynccontextmanager
    async def fake_build(
        requested_root: Path,
        accounts: LocalAccountStore,
        *,
        include_api: bool = True,
        include_mutations: bool,
        include_browser: bool,
    ):
        calls.append(
            {
                "root": requested_root,
                "accounts": accounts,
                "include_api": include_api,
                "include_mutations": include_mutations,
                "include_browser": include_browser,
            }
        )
        yield SimpleNamespace(api=_FakeApi(), mutations=None, browser=None)

    monkeypatch.setattr(cli_module, "build_standalone_app", fake_build)
    monkeypatch.setattr(cli_module, "resolve_standalone_data_root", lambda: root)

    assert (
        cli_module.main(["nurture", "insights", "refresh", "alice", "--preset", "recruitment"]) == 0
    )

    output = capsys.readouterr().out
    assert output == (
        "nurture-insights refreshed=1 skipped_spacing=0 scoreable=1 "
        "insufficient=1 below=0 baseline=0 above=0 top=0\n"
    )
    assert calls == [
        {
            "root": root,
            "accounts": calls[0]["accounts"],
            "include_api": True,
            "include_mutations": False,
            "include_browser": False,
        }
    ]
    assert tuple((root / "nurture" / "runs").rglob("*.json")) == run_receipts_before
    for sentinel in (_SOURCE_SENTINEL, _DRAFT_SENTINEL, _MEDIA_SENTINEL, _TOKEN_SENTINEL):
        assert sentinel not in output
    snapshot_text = "".join(
        path.read_text(encoding="utf-8") for path in (root / "nurture" / "insights").rglob("*.json")
    )
    for sentinel in (_SOURCE_SENTINEL, _DRAFT_SENTINEL, _MEDIA_SENTINEL, _TOKEN_SENTINEL):
        assert sentinel not in snapshot_text

    assert cli_module.main(["api", "post-insights", "alice", "media-opaque-1"]) == 0
    api_output = capsys.readouterr().out
    assert api_output == "post-insights period=lifetime likes=1 replies=2 reposts=3 quotes=4\n"
    assert "media-opaque-1" not in api_output


def test_unknown_preset_and_account_fail_before_cli_composition(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root, _account, _store, _preset = _setup(tmp_path)
    monkeypatch.setattr(cli_module, "resolve_standalone_data_root", lambda: root)
    compositions: list[bool] = []

    @asynccontextmanager
    async def forbidden_build(*_args: object, **_kwargs: object):
        compositions.append(True)
        raise AssertionError("composition must not happen before invocation validation")
        yield SimpleNamespace(api=None, mutations=None, browser=None)

    monkeypatch.setattr(cli_module, "build_standalone_app", forbidden_build)

    assert cli_module.main(["nurture", "insights", "refresh", "alice", "--preset", "unknown"]) == 1
    assert capsys.readouterr().err == "ERROR PRESET_NOT_FOUND\n"
    assert (
        cli_module.main(["nurture", "insights", "refresh", "unknown", "--preset", "recruitment"])
        == 1
    )
    assert capsys.readouterr().err.startswith("ERROR ACCOUNT_NOT_FOUND")
    assert compositions == []
