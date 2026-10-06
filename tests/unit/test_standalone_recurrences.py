from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest

import threads_platform.standalone.recurrences as recurrence_module
from threads_platform.standalone.accounts import LocalAccountStore
from threads_platform.standalone.recurrences import (
    LocalRecurrenceRunner,
    LocalRecurrenceStore,
    RecurrenceRecord,
    StandaloneRecurrenceError,
    next_anchored_boundary,
)
from threads_platform.standalone.workflows import (
    StandaloneWorkflowError,
    WorkflowPlan,
    WorkflowStep,
    is_read_only_workflow,
)


def _fixture_store(tmp_path: Path) -> tuple[LocalRecurrenceStore, LocalAccountStore]:
    accounts = LocalAccountStore(tmp_path)
    accounts.add("alice")
    return LocalRecurrenceStore(tmp_path, accounts), accounts


def _write_workflow(path: Path, steps: list[object], *, account: str = "alice") -> Path:
    path.write_text(
        json.dumps({"version": 1, "account": account, "steps": steps}),
        encoding="utf-8",
    )
    return path


def _simple_workflow(path: Path) -> Path:
    return _write_workflow(path, [{"action": "quota"}])


def _record_path(root: Path, record: RecurrenceRecord) -> Path:
    return root / "recurrences" / record.id / "record.json"


@dataclass
class FakeTiming:
    current: datetime
    monotonic_value: float = 0.0
    interrupt_on_sleep: int | None = None
    sleep_observer: Callable[[int], None] | None = None

    def __post_init__(self) -> None:
        self.sleep_calls: list[float] = []

    def utc_now(self) -> datetime:
        return self.current

    def monotonic(self) -> float:
        return self.monotonic_value

    async def sleep(self, seconds: float) -> None:
        self.sleep_calls.append(seconds)
        if self.sleep_observer is not None:
            self.sleep_observer(len(self.sleep_calls))
        if self.interrupt_on_sleep == len(self.sleep_calls):
            raise KeyboardInterrupt
        self.advance(seconds)

    def advance(self, seconds: float) -> None:
        self.current += timedelta(seconds=seconds)
        self.monotonic_value += seconds


def test_create_snapshots_exact_bytes_and_round_trips_strict_record(tmp_path: Path) -> None:
    store, _ = _fixture_store(tmp_path)
    source = tmp_path / "operator-source.json"
    exact = b'{ "version": 1, "account": "alice", "steps": [{"action":"quota"}] }\n'
    source.write_bytes(exact)

    record = store.create(source, 300, now=datetime(2026, 1, 1, tzinfo=UTC))

    assert record.interval_seconds == 300
    assert record.status == "ENABLED"
    assert record.anchor_at == datetime(2026, 1, 1, tzinfo=UTC)
    assert record.next_due_at == record.anchor_at + timedelta(seconds=300)
    assert (tmp_path / "recurrences" / record.id / "workflow.json").read_bytes() == exact
    assert source.name not in _record_path(tmp_path, record).read_text(encoding="utf-8")
    assert store.get(record.id) == record
    assert store.list() == (record,)


@pytest.mark.parametrize("interval", [300, 2_592_000])
def test_interval_endpoints_are_accepted(tmp_path: Path, interval: int) -> None:
    store, _ = _fixture_store(tmp_path)
    record = store.create(_simple_workflow(tmp_path / "workflow.json"), interval)
    assert record.interval_seconds == interval


@pytest.mark.parametrize("interval", [299, 2_592_001, True, 300.0, "300"])
def test_invalid_intervals_are_rejected(tmp_path: Path, interval: object) -> None:
    store, _ = _fixture_store(tmp_path)
    source = _simple_workflow(tmp_path / "workflow.json")
    with pytest.raises(StandaloneRecurrenceError) as caught:
        store.create(source, cast(int, interval))
    assert caught.value.code == "RECURRENCE_INTERVAL_INVALID"


def test_failed_staged_create_is_never_listed(tmp_path: Path) -> None:
    store, _ = _fixture_store(tmp_path)
    source = _write_workflow(
        tmp_path / "workflow.json",
        [{"action": "post_text", "text": "never expose this mutation text"}],
    )

    with pytest.raises(StandaloneRecurrenceError) as caught:
        store.create(source, 300)

    assert caught.value.code == "RECURRENCE_WORKFLOW_NOT_READ_ONLY"
    assert store.list() == ()
    assert list((tmp_path / "recurrences").iterdir()) == []


def test_staged_snapshot_is_parsed_before_commit(tmp_path: Path) -> None:
    store, _ = _fixture_store(tmp_path)
    source = tmp_path / "workflow.json"
    source.write_text("{invalid", encoding="utf-8")

    with pytest.raises(StandaloneWorkflowError) as caught:
        store.create(source, 300)

    assert caught.value.code == "INVALID_WORKFLOW"
    assert store.list() == ()
    assert list((tmp_path / "recurrences").iterdir()) == []


def test_all_current_read_step_types_are_accepted(tmp_path: Path) -> None:
    store, _ = _fixture_store(tmp_path)
    steps: list[object] = [
        {"action": "quota"},
        {"action": "media", "media_id": "media-1"},
        {"action": "replies", "thread_id": "thread-1"},
        {"action": "conversation", "thread_id": "thread-1"},
        {"action": "feed", "limit": 3},
        {"action": "profile", "username": "alice"},
        {"action": "thread", "thread_ref": "/@alice/post/post-1"},
        {"action": "public_profile", "username": "alice"},
        {"action": "profile_posts", "username": "alice", "limit": 10},
        {
            "action": "search",
            "query": "do not print this query",
            "mode": "KEYWORD",
            "type": "TOP",
            "limit": 10,
        },
        {"action": "mentions", "limit": 10},
    ]
    record = store.create(_write_workflow(tmp_path / "workflow.json", steps), 300)

    plan = store.load_workflow(record)
    assert len(plan.steps) == len(steps)
    assert is_read_only_workflow(plan)


def test_future_unclassified_step_is_default_denied() -> None:
    @dataclass(frozen=True)
    class FutureWorkflowStep:
        pass

    plan = WorkflowPlan(1, "alice", (cast(WorkflowStep, FutureWorkflowStep()),))
    assert not is_read_only_workflow(plan)


def test_source_path_is_not_persisted_in_record(tmp_path: Path) -> None:
    store, _ = _fixture_store(tmp_path)
    source = _simple_workflow(tmp_path / "source-path-sentinel.json")
    record = store.create(source, 300)
    document = _record_path(tmp_path, record).read_text(encoding="utf-8")
    assert str(source) not in document
    assert record.workflow_file == "workflow.json"


def test_record_update_is_atomic_on_replace_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, _ = _fixture_store(tmp_path)
    record = store.create(_simple_workflow(tmp_path / "workflow.json"), 300)

    def fail_replace(_source: Path, _destination: Path) -> None:
        raise OSError("sentinel detail is not surfaced")

    monkeypatch.setattr(recurrence_module.os, "replace", fail_replace)
    with pytest.raises(StandaloneRecurrenceError) as caught:
        store.disable(record.id)

    assert caught.value.code == "RECURRENCE_STATE_INVALID"
    assert store.get(record.id).status == "ENABLED"
    assert not list((tmp_path / "recurrences" / record.id).glob("*.tmp"))


def test_path_escape_and_malformed_ids_fail_closed(tmp_path: Path) -> None:
    store, _ = _fixture_store(tmp_path)
    with pytest.raises(StandaloneRecurrenceError) as caught:
        store.get("../outside")
    assert caught.value.code == "RECURRENCE_NOT_FOUND"


def test_symlink_recurrences_directory_fails_closed(tmp_path: Path) -> None:
    store, _ = _fixture_store(tmp_path)
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    link = tmp_path / "recurrences"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation is unavailable on this Windows host")
    with pytest.raises(StandaloneRecurrenceError) as caught:
        store.list()
    assert caught.value.code == "RECURRENCE_STATE_INVALID"


def test_symlink_snapshot_fails_closed(tmp_path: Path) -> None:
    store, _ = _fixture_store(tmp_path)
    record = store.create(_simple_workflow(tmp_path / "workflow.json"), 300)
    snapshot = tmp_path / "recurrences" / record.id / "workflow.json"
    snapshot.unlink()
    outside = tmp_path / "outside.json"
    _simple_workflow(outside)
    try:
        snapshot.symlink_to(outside)
    except OSError:
        pytest.skip("symlink creation is unavailable on this Windows host")
    with pytest.raises(StandaloneRecurrenceError) as caught:
        store.load_workflow(record)
    assert caught.value.code == "RECURRENCE_STATE_INVALID"


def test_next_due_uses_fixed_anchor_without_completion_drift() -> None:
    anchor = datetime(2026, 1, 1, 10, tzinfo=UTC)
    assert next_anchored_boundary(anchor, anchor + timedelta(minutes=17), 300) == (
        anchor + timedelta(minutes=20)
    )
    assert next_anchored_boundary(anchor, anchor + timedelta(minutes=20), 300) == (
        anchor + timedelta(minutes=25)
    )


def test_restart_skips_all_missed_boundaries_and_waits_for_next_anchor(
    tmp_path: Path,
) -> None:
    anchor = datetime(2026, 1, 1, 10, tzinfo=UTC)
    store, _ = _fixture_store(tmp_path)
    record = store.create(_simple_workflow(tmp_path / "workflow.json"), 300, now=anchor)
    timing = FakeTiming(anchor + timedelta(minutes=17))
    calls: list[datetime] = []

    async def execute(_plan: WorkflowPlan) -> object:
        calls.append(timing.utc_now())
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        asyncio.run(LocalRecurrenceRunner(store, timing).run(record.id, execute))

    assert timing.sleep_calls == [180]
    assert calls == [anchor + timedelta(minutes=20)]
    state = store.get(record.id)
    assert state.last_outcome == "INTERRUPTED"
    assert state.next_due_at == anchor + timedelta(minutes=25)


def test_occurrence_start_is_persisted_before_executor_and_failure_is_not_retried(
    tmp_path: Path,
) -> None:
    anchor = datetime(2026, 1, 1, 10, tzinfo=UTC)
    store, _ = _fixture_store(tmp_path)
    record = store.create(_simple_workflow(tmp_path / "workflow.json"), 300, now=anchor)
    timing = FakeTiming(anchor + timedelta(seconds=299))
    calls = 0

    async def execute(_plan: WorkflowPlan) -> object:
        nonlocal calls
        calls += 1
        state = store.get(record.id)
        assert state.last_outcome == "RUNNING"
        assert state.last_started_at == anchor + timedelta(seconds=300)
        assert state.next_due_at == anchor + timedelta(seconds=600)
        raise StandaloneWorkflowError("ACCOUNT_BUSY", 1)

    timing.interrupt_on_sleep = 2
    with pytest.raises(KeyboardInterrupt):
        asyncio.run(LocalRecurrenceRunner(store, timing).run(record.id, execute))

    assert calls == 1
    state = store.get(record.id)
    assert state.last_outcome == "SKIPPED"
    assert state.last_error_code == "ACCOUNT_BUSY"
    assert state.last_step_index == 1
    assert timing.sleep_calls == [1, 300]


def test_later_occurrence_runs_after_account_busy_without_catch_up(tmp_path: Path) -> None:
    anchor = datetime(2026, 1, 1, 10, tzinfo=UTC)
    store, _ = _fixture_store(tmp_path)
    record = store.create(_simple_workflow(tmp_path / "workflow.json"), 300, now=anchor)
    timing = FakeTiming(anchor + timedelta(seconds=299), interrupt_on_sleep=3)
    calls: list[datetime] = []

    def inspect_skip(sleep_number: int) -> None:
        if sleep_number == 2:
            skipped = store.get(record.id)
            assert skipped.last_outcome == "SKIPPED"
            assert skipped.last_error_code == "ACCOUNT_BUSY"

    async def execute(_plan: WorkflowPlan) -> object:
        calls.append(timing.utc_now())
        if len(calls) == 1:
            raise StandaloneWorkflowError("ACCOUNT_BUSY", 2)

    timing.sleep_observer = inspect_skip
    with pytest.raises(KeyboardInterrupt):
        asyncio.run(LocalRecurrenceRunner(store, timing).run(record.id, execute))

    assert calls == [anchor + timedelta(minutes=5), anchor + timedelta(minutes=10)]
    state = store.get(record.id)
    assert state.last_outcome == "SUCCESS"
    assert state.next_due_at == anchor + timedelta(minutes=15)


def test_long_occurrence_recomputes_next_due_from_anchor(tmp_path: Path) -> None:
    anchor = datetime(2026, 1, 1, 10, tzinfo=UTC)
    store, _ = _fixture_store(tmp_path)
    record = store.create(_simple_workflow(tmp_path / "workflow.json"), 300, now=anchor)
    timing = FakeTiming(anchor + timedelta(seconds=299))
    calls: list[datetime] = []

    async def execute(_plan: WorkflowPlan) -> object:
        calls.append(timing.utc_now())
        if len(calls) == 1:
            timing.advance(13 * 60)
            return None
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        asyncio.run(LocalRecurrenceRunner(store, timing).run(record.id, execute))

    assert calls == [anchor + timedelta(minutes=5), anchor + timedelta(minutes=20)]
    assert timing.sleep_calls == [1, 120]


def test_stale_running_is_repaired_only_after_runner_lock_acquisition(tmp_path: Path) -> None:
    anchor = datetime(2026, 1, 1, 10, tzinfo=UTC)
    store, _ = _fixture_store(tmp_path)
    record = store.create(_simple_workflow(tmp_path / "workflow.json"), 300, now=anchor)
    running = replace(
        record,
        updated_at=anchor + timedelta(minutes=4),
        last_started_at=anchor + timedelta(minutes=4),
        last_outcome="RUNNING",
    )
    store.update(running)
    held_lock = store.acquire_runner_lock(record.id)
    timing = FakeTiming(anchor + timedelta(minutes=17), interrupt_on_sleep=1)

    with pytest.raises(StandaloneRecurrenceError) as busy:
        asyncio.run(LocalRecurrenceRunner(store, timing).run(record.id, _never_execute))
    assert busy.value.code == "RECURRENCE_BUSY"
    assert store.get(record.id).last_outcome == "RUNNING"

    held_lock.release()
    with pytest.raises(KeyboardInterrupt):
        asyncio.run(LocalRecurrenceRunner(store, timing).run(record.id, _never_execute))
    repaired = store.get(record.id)
    assert repaired.last_outcome == "INTERRUPTED"
    assert repaired.last_error_code == "INTERRUPTED"
    assert repaired.next_due_at == anchor + timedelta(minutes=20)


async def _never_execute(_plan: WorkflowPlan) -> object:
    raise AssertionError("the runner should still be waiting")


def test_keyboard_interrupt_while_waiting_does_not_create_occurrence(tmp_path: Path) -> None:
    anchor = datetime(2026, 1, 1, 10, tzinfo=UTC)
    store, _ = _fixture_store(tmp_path)
    record = store.create(_simple_workflow(tmp_path / "workflow.json"), 300, now=anchor)
    timing = FakeTiming(anchor, interrupt_on_sleep=1)

    with pytest.raises(KeyboardInterrupt):
        asyncio.run(LocalRecurrenceRunner(store, timing).run(record.id, _never_execute))

    assert store.get(record.id) == record


def test_keyboard_interrupt_during_occurrence_is_recorded_and_not_replayed(tmp_path: Path) -> None:
    anchor = datetime(2026, 1, 1, 10, tzinfo=UTC)
    store, _ = _fixture_store(tmp_path)
    record = store.create(_simple_workflow(tmp_path / "workflow.json"), 300, now=anchor)
    timing = FakeTiming(anchor + timedelta(seconds=299))
    calls = 0

    async def interrupt(_plan: WorkflowPlan) -> object:
        nonlocal calls
        calls += 1
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        asyncio.run(LocalRecurrenceRunner(store, timing).run(record.id, interrupt))

    interrupted = store.get(record.id)
    assert calls == 1
    assert interrupted.last_outcome == "INTERRUPTED"
    assert interrupted.last_finished_at == interrupted.last_started_at
    assert interrupted.next_due_at == anchor + timedelta(minutes=10)

    second_timing = FakeTiming(anchor + timedelta(minutes=5), interrupt_on_sleep=1)
    with pytest.raises(KeyboardInterrupt):
        asyncio.run(LocalRecurrenceRunner(store, second_timing).run(record.id, interrupt))
    assert calls == 1


def test_runner_lock_excludes_second_runner_and_disable(tmp_path: Path) -> None:
    store, _ = _fixture_store(tmp_path)
    record = store.create(_simple_workflow(tmp_path / "workflow.json"), 300)
    held_lock = store.acquire_runner_lock(record.id)
    with pytest.raises(StandaloneRecurrenceError) as busy_run:
        asyncio.run(
            LocalRecurrenceRunner(store, FakeTiming(record.anchor_at)).run(
                record.id, _never_execute
            )
        )
    assert busy_run.value.code == "RECURRENCE_BUSY"
    with pytest.raises(StandaloneRecurrenceError) as busy_disable:
        store.disable(record.id)
    assert busy_disable.value.code == "RECURRENCE_BUSY"
    held_lock.release()


def test_disabled_recurrence_cannot_run(tmp_path: Path) -> None:
    store, _ = _fixture_store(tmp_path)
    record = store.create(_simple_workflow(tmp_path / "workflow.json"), 300)
    disabled = store.disable(record.id)
    assert disabled.status == "DISABLED"
    with pytest.raises(StandaloneRecurrenceError) as caught:
        asyncio.run(
            LocalRecurrenceRunner(store, FakeTiming(record.anchor_at)).run(
                record.id, _never_execute
            )
        )
    assert caught.value.code == "RECURRENCE_DISABLED"


@pytest.mark.parametrize(
    "mutation",
    ["corrupt", "duplicate", "extra", "wrong_type", "newer_version"],
)
def test_corrupt_or_unsupported_record_fails_closed(
    tmp_path: Path,
    mutation: str,
) -> None:
    store, _ = _fixture_store(tmp_path)
    record = store.create(_simple_workflow(tmp_path / "workflow.json"), 300)
    path = _record_path(tmp_path, record)
    if mutation == "corrupt":
        path.write_text("not-json", encoding="utf-8")
        expected = "RECURRENCE_STATE_INVALID"
    elif mutation == "duplicate":
        path.write_text('{"version":1,"version":1}', encoding="utf-8")
        expected = "RECURRENCE_STATE_INVALID"
    else:
        document = json.loads(path.read_text(encoding="utf-8"))
        if mutation == "extra":
            document["unexpected"] = 1
            expected = "RECURRENCE_STATE_INVALID"
        elif mutation == "wrong_type":
            document["interval_seconds"] = True
            expected = "RECURRENCE_STATE_INVALID"
        else:
            document["version"] = 2
            expected = "RECURRENCE_VERSION_UNSUPPORTED"
        path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(StandaloneRecurrenceError) as caught:
        store.get(record.id)
    assert caught.value.code == expected


def test_missing_snapshot_fails_closed_before_runner_starts(tmp_path: Path) -> None:
    store, _ = _fixture_store(tmp_path)
    record = store.create(_simple_workflow(tmp_path / "workflow.json"), 300)
    (tmp_path / "recurrences" / record.id / "workflow.json").unlink()
    with pytest.raises(StandaloneRecurrenceError) as caught:
        asyncio.run(
            LocalRecurrenceRunner(store, FakeTiming(record.anchor_at)).run(
                record.id, _never_execute
            )
        )
    assert caught.value.code == "RECURRENCE_STATE_INVALID"


def test_mutated_snapshot_is_read_only_checked_again_before_runner_starts(
    tmp_path: Path,
) -> None:
    store, _ = _fixture_store(tmp_path)
    record = store.create(_simple_workflow(tmp_path / "workflow.json"), 300)
    snapshot = tmp_path / "recurrences" / record.id / "workflow.json"
    _write_workflow(
        snapshot,
        [{"action": "post_text", "text": "must not run"}],
    )
    with pytest.raises(StandaloneRecurrenceError) as caught:
        store.load_workflow(store.get(record.id))
    assert caught.value.code == "RECURRENCE_WORKFLOW_NOT_READ_ONLY"


def test_record_only_contains_bounded_metadata_not_workflow_query(tmp_path: Path) -> None:
    store, _ = _fixture_store(tmp_path)
    query_sentinel = "private-query-sentinel"
    source = _write_workflow(
        tmp_path / "workflow.json",
        [
            {
                "action": "search",
                "query": query_sentinel,
                "mode": "KEYWORD",
                "type": "TOP",
                "limit": 1,
            }
        ],
    )
    record = store.create(source, 300)
    metadata = _record_path(tmp_path, record).read_text(encoding="utf-8")

    assert query_sentinel not in metadata
    assert str(source) not in metadata
    assert "credential_ref" not in metadata
    assert "token" not in metadata.lower()
