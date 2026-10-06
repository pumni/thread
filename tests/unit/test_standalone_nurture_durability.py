from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest

import threads_platform.standalone.nurture_store as nurture_store_module
from threads_platform.infrastructure.local.process_lock import FilesystemProcessLock
from threads_platform.standalone.nurture import NurturePresetV1, get_nurture_preset
from threads_platform.standalone.nurture_store import (
    NurtureStateError,
    NurtureStore,
    fingerprint_remote_thread,
)

_START = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
_DECISION = "RECRUITMENT_MATCH"


def _setup(tmp_path: Path) -> tuple[NurtureStore, UUID, NurturePresetV1]:
    return NurtureStore(tmp_path), uuid4(), get_nurture_preset("recruitment")


def _run_path(root: Path, account_id: UUID, run_id: UUID) -> Path:
    return root / "nurture" / "runs" / str(account_id) / "active" / f"{run_id}.json"


def _completed_run_path(root: Path, account_id: UUID, run_id: UUID) -> Path:
    return root / "nurture" / "runs" / str(account_id) / "completed" / f"{run_id}.json"


def _state_path(root: Path, account_id: UUID, preset: NurturePresetV1) -> Path:
    return root / "nurture" / "state" / str(account_id) / f"{preset.id}.json"


def _assert_code(error: pytest.ExceptionInfo[NurtureStateError], code: str) -> None:
    assert error.value.code == code
    assert str(error.value) == code


def test_run_receipt_is_exact_and_updates_atomically(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, account_id, preset = _setup(tmp_path)
    with store.acquire_account_lock(account_id) as owner:
        run = owner.start_run(preset, now=_START)
        receipt = run.receipt
        path = _run_path(tmp_path, account_id, receipt.id)
        initial = json.loads(path.read_text(encoding="utf-8"))
        assert set(initial) == {
            "version",
            "id",
            "account_id",
            "preset_id",
            "preset_version",
            "started_at",
            "finished_at",
            "outcome",
            "discovered_count",
            "deduped_count",
            "selected_count",
            "enriched_count",
            "replied_count",
            "published_count",
            "skipped_count",
            "decision_codes",
            "operation_ids",
            "error_code",
            "failed_stage",
        }
        assert initial["version"] == 1
        assert initial["outcome"] == "RUNNING"

        run.update(discovered_count=3)
        before_failure = path.read_bytes()

        def fail_replace(_source: Path, _destination: Path) -> None:
            raise OSError("private failure detail")

        monkeypatch.setattr(nurture_store_module.os, "replace", fail_replace)
        with pytest.raises(NurtureStateError) as caught:
            run.update(deduped_count=1)
        _assert_code(caught, "NURTURE_STATE_INVALID")
        assert path.read_bytes() == before_failure
        assert not list(path.parent.glob("*.tmp"))

        monkeypatch.undo()
        finished = run.finish("SUCCESS", now=_START + timedelta(minutes=1))
        assert finished.discovered_count == 3
        assert owner.get_run(receipt.id).outcome == "SUCCESS"


def test_failed_run_creation_leaves_no_partial_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, account_id, preset = _setup(tmp_path)
    with store.acquire_account_lock(account_id) as owner:

        def fail_link(_source: Path, _destination: Path) -> None:
            raise OSError("private failure detail")

        monkeypatch.setattr(nurture_store_module.os, "link", fail_link)
        with pytest.raises(NurtureStateError) as caught:
            owner.start_run(preset, now=_START)
        _assert_code(caught, "NURTURE_STATE_INVALID")
        run_directory = tmp_path / "nurture" / "runs" / str(account_id) / "active"
        assert not list(run_directory.glob("*.json"))
        assert not list(run_directory.glob("*.tmp"))


def test_run_identity_is_immutable_and_boolean_counters_are_rejected(tmp_path: Path) -> None:
    store, account_id, preset = _setup(tmp_path)
    with store.acquire_account_lock(account_id) as owner:
        scope = owner.start_run(preset, now=_START)
        with pytest.raises(NurtureStateError):
            scope.update(id=uuid4())
        with pytest.raises(NurtureStateError):
            scope.update(discovered_count=True)


@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    [
        ("extra", "NURTURE_STATE_INVALID"),
        ("unsupported_version", "NURTURE_VERSION_UNSUPPORTED"),
        ("bool_version", "NURTURE_STATE_INVALID"),
        ("bool_counter", "NURTURE_STATE_INVALID"),
        ("path_account", "NURTURE_STATE_INVALID"),
        ("bad_timestamp", "NURTURE_STATE_INVALID"),
    ],
)
def test_run_receipts_reject_malformed_documents(
    tmp_path: Path,
    mutation: str,
    expected_code: str,
) -> None:
    store, account_id, preset = _setup(tmp_path)
    with store.acquire_account_lock(account_id) as owner:
        run = owner.start_run(preset, now=_START).receipt
        path = _run_path(tmp_path, account_id, run.id)
        data = json.loads(path.read_text(encoding="utf-8"))
        if mutation == "extra":
            data["extra"] = "reject"
        elif mutation == "unsupported_version":
            data["version"] = 2
        elif mutation == "bool_version":
            data["version"] = True
        elif mutation == "bool_counter":
            data["discovered_count"] = True
        elif mutation == "path_account":
            data["account_id"] = "../outside"
        elif mutation == "bad_timestamp":
            data["started_at"] = "yesterday"
        path.write_text(json.dumps(data), encoding="utf-8")
        with pytest.raises(NurtureStateError) as caught:
            owner.get_run(run.id)
        _assert_code(caught, expected_code)


def test_duplicate_json_keys_are_rejected_in_receipts_and_target_state(tmp_path: Path) -> None:
    store, account_id, preset = _setup(tmp_path)
    with store.acquire_account_lock(account_id) as owner:
        run = owner.start_run(preset, now=_START).receipt
        run_path = _run_path(tmp_path, account_id, run.id)
        run_path.write_text('{"version":1,"version":1}', encoding="utf-8")
        with pytest.raises(NurtureStateError) as caught_run:
            owner.get_run(run.id)
        _assert_code(caught_run, "NURTURE_STATE_INVALID")

        run_path.write_text(
            json.dumps(
                {
                    "version": run.version,
                    "id": str(run.id),
                    "account_id": str(run.account_id),
                    "preset_id": run.preset_id,
                    "preset_version": run.preset_version,
                    "started_at": "2026-01-02T03:04:05.000000Z",
                    "finished_at": None,
                    "outcome": "RUNNING",
                    "discovered_count": 0,
                    "deduped_count": 0,
                    "selected_count": 0,
                    "enriched_count": 0,
                    "replied_count": 0,
                    "published_count": 0,
                    "skipped_count": 0,
                    "decision_codes": [],
                    "operation_ids": [],
                    "error_code": None,
                    "failed_stage": None,
                }
            ),
            encoding="utf-8",
        )
        owner.observe_target(
            preset, fingerprint_remote_thread("thread-duplicate"), run.id, _DECISION
        )
        state_path = _state_path(tmp_path, account_id, preset)
        state_path.write_text('{"version":1,"version":1}', encoding="utf-8")
        with pytest.raises(NurtureStateError) as caught_state:
            owner.get_targets(preset)
        _assert_code(caught_state, "NURTURE_STATE_INVALID")


def test_target_state_is_versioned_exact_and_updates_atomically(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, account_id, preset = _setup(tmp_path)
    with store.acquire_account_lock(account_id) as owner:
        run = owner.start_run(preset, now=_START)
        fingerprint = fingerprint_remote_thread("thread-state")
        first = owner.observe_target(preset, fingerprint, run.receipt.id, _DECISION, now=_START)
        path = _state_path(tmp_path, account_id, preset)
        document = json.loads(path.read_text(encoding="utf-8"))
        assert set(document) == {"version", "account_id", "preset_id", "targets"}
        assert document["version"] == 1
        assert set(document["targets"][0]) == {
            "fingerprint",
            "first_seen_at",
            "last_seen_at",
            "last_decision_code",
            "last_action_at",
            "action_state",
            "last_run_id",
            "last_operation_id",
        }

        owner.observe_target(
            preset,
            fingerprint,
            run.receipt.id,
            "STILL_MATCHED",
            now=_START + timedelta(minutes=1),
        )
        before_failure = path.read_bytes()

        def fail_replace(_source: Path, _destination: Path) -> None:
            raise OSError("private failure detail")

        monkeypatch.setattr(nurture_store_module.os, "replace", fail_replace)
        with pytest.raises(NurtureStateError) as caught:
            owner.observe_target(
                preset,
                fingerprint,
                run.receipt.id,
                "STILL_MATCHED",
                now=_START + timedelta(minutes=2),
            )
        _assert_code(caught, "NURTURE_STATE_INVALID")
        assert path.read_bytes() == before_failure
        assert not list(path.parent.glob("*.tmp"))
        assert owner.get_targets(preset)[0].first_seen_at == first.first_seen_at


@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    [
        ("extra", "NURTURE_STATE_INVALID"),
        ("unsupported_version", "NURTURE_VERSION_UNSUPPORTED"),
        ("bool_version", "NURTURE_STATE_INVALID"),
        ("invalid_enum", "NURTURE_STATE_INVALID"),
        ("invalid_uuid", "NURTURE_STATE_INVALID"),
        ("invalid_timestamp", "NURTURE_STATE_INVALID"),
    ],
)
def test_target_state_rejects_malformed_documents(
    tmp_path: Path,
    mutation: str,
    expected_code: str,
) -> None:
    store, account_id, preset = _setup(tmp_path)
    with store.acquire_account_lock(account_id) as owner:
        run = owner.start_run(preset, now=_START)
        owner.observe_target(
            preset,
            fingerprint_remote_thread("malformed-target-state"),
            run.receipt.id,
            _DECISION,
            now=_START,
        )
        path = _state_path(tmp_path, account_id, preset)
        data = json.loads(path.read_text(encoding="utf-8"))
        if mutation == "extra":
            data["extra"] = "reject"
        elif mutation == "unsupported_version":
            data["version"] = 2
        elif mutation == "bool_version":
            data["version"] = True
        elif mutation == "invalid_enum":
            data["targets"][0]["action_state"] = "UNKNOWN"
        elif mutation == "invalid_uuid":
            data["targets"][0]["last_run_id"] = "../outside"
        elif mutation == "invalid_timestamp":
            data["targets"][0]["first_seen_at"] = "yesterday"
        path.write_text(json.dumps(data), encoding="utf-8")
        with pytest.raises(NurtureStateError) as caught:
            owner.get_targets(preset)
        _assert_code(caught, expected_code)


def test_failed_target_creation_leaves_no_partial_document(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, account_id, preset = _setup(tmp_path)
    with store.acquire_account_lock(account_id) as owner:
        run = owner.start_run(preset, now=_START)
        path = _state_path(tmp_path, account_id, preset)

        def fail_link(_source: Path, _destination: Path) -> None:
            raise OSError("private failure detail")

        monkeypatch.setattr(nurture_store_module.os, "link", fail_link)
        with pytest.raises(NurtureStateError) as caught:
            owner.observe_target(
                preset,
                fingerprint_remote_thread("target-create-failure"),
                run.receipt.id,
                _DECISION,
                now=_START,
            )
        _assert_code(caught, "NURTURE_STATE_INVALID")
        assert not path.exists()
        assert not list(path.parent.glob("*.tmp"))


def test_symlinked_run_and_state_files_fail_closed(tmp_path: Path) -> None:
    store, account_id, preset = _setup(tmp_path)
    with store.acquire_account_lock(account_id) as owner:
        run = owner.start_run(preset, now=_START)
        owner.observe_target(
            preset,
            fingerprint_remote_thread("symlink-target"),
            run.receipt.id,
            _DECISION,
            now=_START,
        )
        run.finish("SUCCESS", now=_START)

    outside = tmp_path.parent / f"{tmp_path.name}-outside.json"
    outside.write_text("{}", encoding="utf-8")
    for path in (
        _completed_run_path(tmp_path, account_id, run.receipt.id),
        _state_path(tmp_path, account_id, preset),
    ):
        original = path.read_bytes()
        path.unlink()
        try:
            path.symlink_to(outside)
        except OSError:
            path.write_bytes(original)
            pytest.skip("symlink creation is unavailable on this Windows host")
        with pytest.raises(NurtureStateError) as caught:
            with store.acquire_account_lock(account_id) as owner:
                if path.suffix == ".json" and "runs" in path.parts:
                    owner.get_run(run.receipt.id)
                else:
                    owner.get_targets(preset)
        _assert_code(caught, "NURTURE_STATE_INVALID")
        path.unlink()
        path.write_bytes(original)


def test_symlinked_nurture_directory_is_rejected(tmp_path: Path) -> None:
    store, account_id, _ = _setup(tmp_path)
    outside = tmp_path.parent / f"{tmp_path.name}-outside-dir"
    outside.mkdir()
    link = tmp_path / "nurture"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation is unavailable on this Windows host")
    with pytest.raises(NurtureStateError) as caught:
        store.acquire_account_lock(account_id).acquire()
    _assert_code(caught, "NURTURE_STATE_INVALID")


@pytest.mark.parametrize("document_kind", ["run", "target"])
def test_oversized_documents_fail_closed(tmp_path: Path, document_kind: str) -> None:
    store, account_id, preset = _setup(tmp_path)
    with store.acquire_account_lock(account_id) as owner:
        run = owner.start_run(preset, now=_START)
        if document_kind == "run":
            path = _run_path(tmp_path, account_id, run.receipt.id)
            size = nurture_store_module.MAX_NURTURE_RUN_BYTES + 1
        else:
            owner.observe_target(
                preset,
                fingerprint_remote_thread("oversized-state"),
                run.receipt.id,
                _DECISION,
                now=_START,
            )
            path = _state_path(tmp_path, account_id, preset)
            size = nurture_store_module.MAX_NURTURE_TARGET_STATE_BYTES + 1
        path.write_bytes(b" " * size)
        with pytest.raises(NurtureStateError) as caught:
            if document_kind == "run":
                owner.get_run(run.receipt.id)
            else:
                owner.get_targets(preset)
        _assert_code(caught, "NURTURE_STATE_INVALID")


def test_fingerprint_is_deterministic_and_normalizes_remote_id() -> None:
    first = fingerprint_remote_thread("  thread:Remote-123  ")
    second = fingerprint_remote_thread("thread:Remote-123")
    assert first == second
    assert first != fingerprint_remote_thread("thread:remote-123")
    assert len(first) == 64
    assert all(character in "0123456789abcdef" for character in first)
    with pytest.raises(NurtureStateError):
        fingerprint_remote_thread("full post text is not a remote id")


def test_target_state_contains_only_fingerprint_and_local_policy_data(tmp_path: Path) -> None:
    store, account_id, preset = _setup(tmp_path)
    raw_target = "target-id-secret-sentinel"
    credential_sentinel = "credential-ref-secret-sentinel"
    token_sentinel = "threads-access-token-secret-sentinel"
    fingerprint = fingerprint_remote_thread(raw_target)
    with store.acquire_account_lock(account_id) as owner:
        run = owner.start_run(preset, now=_START)
        owner.observe_target(preset, fingerprint, run.receipt.id, _DECISION, now=_START)
    content = _state_path(tmp_path, account_id, preset).read_text(encoding="utf-8")
    assert fingerprint in content
    for sentinel in (raw_target, credential_sentinel, token_sentinel):
        assert sentinel not in content


def test_retention_is_oldest_first_with_stable_fingerprint_tie_break(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(nurture_store_module, "MAX_NURTURE_TARGETS", 2)
    store, account_id, preset = _setup(tmp_path)
    fingerprints = sorted(fingerprint_remote_thread(f"retention-{index}") for index in range(3))
    with store.acquire_account_lock(account_id) as owner:
        run = owner.start_run(preset, now=_START)
        for fingerprint in fingerprints[:2]:
            owner.observe_target(preset, fingerprint, run.receipt.id, _DECISION, now=_START)
        owner.observe_target(
            preset,
            fingerprints[2],
            run.receipt.id,
            _DECISION,
            now=_START + timedelta(minutes=1),
        )
        targets = owner.get_targets(preset)
    assert {target.fingerprint for target in targets} == set(fingerprints[1:])


def test_pending_and_ambiguous_targets_are_never_evicted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(nurture_store_module, "MAX_NURTURE_TARGETS", 2)
    store, account_id, preset = _setup(tmp_path)
    fingerprints = [fingerprint_remote_thread(f"reserved-{index}") for index in range(3)]
    with store.acquire_account_lock(account_id) as owner:
        first = owner.start_run(preset, now=_START)
        owner.reserve_target(preset, fingerprints[0], first.receipt.id, _DECISION, now=_START)
        ambiguous_operation = uuid4()
        owner.complete_target_action(
            preset,
            fingerprints[0],
            first.receipt.id,
            "AMBIGUOUS",
            ambiguous_operation,
            now=_START + timedelta(seconds=1),
        )
        first.finish("SUCCESS", now=_START + timedelta(minutes=1))

        second = owner.start_run(preset, now=_START + timedelta(minutes=2))
        owner.reserve_target(preset, fingerprints[1], second.receipt.id, _DECISION, now=_START)
        with pytest.raises(NurtureStateError) as caught:
            owner.reserve_target(preset, fingerprints[2], second.receipt.id, _DECISION, now=_START)
        _assert_code(caught, "NURTURE_STATE_CAP_REACHED")
        targets = {target.fingerprint: target for target in owner.get_targets(preset)}
        assert targets[fingerprints[0]].action_state == "AMBIGUOUS"
        assert targets[fingerprints[1]].action_state == "PENDING"
        assert targets[fingerprints[0]].last_operation_id == ambiguous_operation


def test_pending_reservation_stays_unresolved_on_later_observation(tmp_path: Path) -> None:
    store, account_id, preset = _setup(tmp_path)
    fingerprint = fingerprint_remote_thread("pending-unresolved")
    with store.acquire_account_lock(account_id) as owner:
        first = owner.start_run(preset, now=_START)
        pending = owner.reserve_target(preset, fingerprint, first.receipt.id, _DECISION, now=_START)
        assert pending.action_state == "PENDING"
        assert pending.last_operation_id is None
        state = store.load_target_state(owner, preset)
        cleared = replace(
            state,
            targets=(replace(pending, action_state="NONE", last_action_at=None),),
        )
        with pytest.raises(NurtureStateError):
            store.save_target_state(owner, cleared)
        first.finish("SUCCESS", now=_START + timedelta(minutes=1))
        second = owner.start_run(preset, now=_START + timedelta(minutes=2))
        observed = owner.observe_target(
            preset,
            fingerprint,
            second.receipt.id,
            "OBSERVED_AGAIN",
            now=_START + timedelta(minutes=3),
        )
        assert observed.action_state == "PENDING"
        assert observed.last_run_id == first.receipt.id
        with pytest.raises(NurtureStateError) as caught:
            owner.reserve_target(preset, fingerprint, second.receipt.id, _DECISION)
        _assert_code(caught, "NURTURE_TARGET_PENDING")


def test_same_account_lock_is_busy_and_different_accounts_are_independent(
    tmp_path: Path,
) -> None:
    store, account_id, _ = _setup(tmp_path)
    first = store.acquire_account_lock(account_id).acquire()
    try:
        with pytest.raises(NurtureStateError) as caught:
            store.acquire_account_lock(account_id).acquire()
        _assert_code(caught, "NURTURE_BUSY")
        with store.acquire_account_lock(uuid4()):
            assert first.held
    finally:
        first.release()


def test_nurture_lock_is_distinct_from_browser_and_mutation_account_lock(tmp_path: Path) -> None:
    store, account_id, _ = _setup(tmp_path)
    nurture_lock = store.acquire_account_lock(account_id).acquire()
    capability_lock = FilesystemProcessLock(tmp_path / "locks" / f"{account_id}.lock")
    try:
        capability_lock.acquire()
        assert nurture_lock.held
    finally:
        capability_lock.release()
        nurture_lock.release()

    capability_lock.acquire()
    try:
        with store.acquire_account_lock(account_id) as second_nurture_lock:
            assert second_nurture_lock.held
    finally:
        capability_lock.release()


def test_stale_running_receipt_repairs_only_during_lock_acquisition(tmp_path: Path) -> None:
    store, account_id, preset = _setup(tmp_path)
    with store.acquire_account_lock(account_id) as owner:
        run = owner.start_run(preset, now=_START).receipt
    assert (
        json.loads(_run_path(tmp_path, account_id, run.id).read_text(encoding="utf-8"))["outcome"]
        == "RUNNING"
    )

    with store.acquire_account_lock(account_id) as owner:
        repaired = owner.get_run(run.id)
        assert repaired.outcome == "INTERRUPTED"
        assert repaired.finished_at == _START
        assert repaired.error_code == "INTERRUPTED"
        assert repaired.failed_stage == "recovery"
        assert not _run_path(tmp_path, account_id, run.id).exists()
        assert _completed_run_path(tmp_path, account_id, run.id).exists()


def test_terminal_move_failure_leaves_valid_receipt_for_next_lock_repair(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, account_id, preset = _setup(tmp_path)
    with store.acquire_account_lock(account_id) as owner:
        scope = owner.start_run(preset, now=_START)
        real_replace = nurture_store_module.os.replace
        replace_calls = 0

        def fail_move(source: Path, destination: Path) -> None:
            nonlocal replace_calls
            replace_calls += 1
            if replace_calls == 2:
                raise OSError("private failure detail")
            real_replace(source, destination)

        monkeypatch.setattr(nurture_store_module.os, "replace", fail_move)
        with pytest.raises(NurtureStateError) as caught:
            scope.finish("SUCCESS", now=_START + timedelta(seconds=1))
        _assert_code(caught, "NURTURE_STATE_INVALID")
        assert (
            json.loads(
                _run_path(tmp_path, account_id, scope.receipt.id).read_text(encoding="utf-8")
            )["outcome"]
            == "SUCCESS"
        )
        assert not _completed_run_path(tmp_path, account_id, scope.receipt.id).exists()

    monkeypatch.undo()
    with store.acquire_account_lock(account_id) as owner:
        assert owner.get_run(scope.receipt.id).outcome == "SUCCESS"
        assert not _run_path(tmp_path, account_id, scope.receipt.id).exists()
        assert _completed_run_path(tmp_path, account_id, scope.receipt.id).exists()


def test_keyboard_interrupt_persists_interrupted_and_propagates(tmp_path: Path) -> None:
    store, account_id, preset = _setup(tmp_path)
    with store.acquire_account_lock(account_id) as owner:
        scope = owner.start_run(preset, now=_START)
        with pytest.raises(KeyboardInterrupt):
            with scope:
                raise KeyboardInterrupt
        assert scope.receipt.outcome == "INTERRUPTED"
        assert owner.get_run(scope.receipt.id).outcome == "INTERRUPTED"


def test_async_cancellation_persists_interrupted_and_propagates(tmp_path: Path) -> None:
    store, account_id, preset = _setup(tmp_path)

    async def execute() -> None:
        async with store.acquire_account_lock(account_id) as owner:
            scope = owner.start_run(preset, now=_START)
            with pytest.raises(asyncio.CancelledError):
                async with scope:
                    raise asyncio.CancelledError
            assert scope.receipt.outcome == "INTERRUPTED"
            assert owner.get_run(scope.receipt.id).outcome == "INTERRUPTED"

    asyncio.run(execute())


def test_operation_references_are_bounded_local_uuids(tmp_path: Path) -> None:
    store, account_id, preset = _setup(tmp_path)
    with store.acquire_account_lock(account_id) as owner:
        scope = owner.start_run(preset, now=_START)
        operation_ids = tuple(uuid4() for _ in range(6))
        for operation_id in operation_ids:
            scope.link_operation(operation_id)
        assert scope.receipt.operation_ids == operation_ids
        with pytest.raises(NurtureStateError) as caught:
            scope.link_operation(uuid4())
        _assert_code(caught, "NURTURE_STATE_CAP_REACHED")
        with pytest.raises(NurtureStateError):
            scope.update(operation_ids=("remote-operation-id",))


def test_target_operation_link_is_local_and_confirmation_requires_it(tmp_path: Path) -> None:
    store, account_id, preset = _setup(tmp_path)
    fingerprint = fingerprint_remote_thread("operation-link-target")
    operation_id = uuid4()
    with store.acquire_account_lock(account_id) as owner:
        run = owner.start_run(preset, now=_START)
        owner.reserve_target(preset, fingerprint, run.receipt.id, _DECISION, now=_START)
        with pytest.raises(NurtureStateError):
            owner.complete_target_action(
                preset, fingerprint, run.receipt.id, "CONFIRMED", None, now=_START
            )
        confirmed = owner.complete_target_action(
            preset,
            fingerprint,
            run.receipt.id,
            "CONFIRMED",
            operation_id,
            now=_START + timedelta(seconds=1),
        )
        assert confirmed.last_operation_id == operation_id
        assert owner.get_targets(preset)[0].last_operation_id == operation_id


def test_preset_and_account_path_identity_rejects_traversal(tmp_path: Path) -> None:
    store, account_id, preset = _setup(tmp_path)
    with pytest.raises(NurtureStateError):
        store.acquire_account_lock("../account")  # type: ignore[arg-type]
    with store.acquire_account_lock(account_id) as owner:
        run = owner.start_run(preset, now=_START)
        invalid_preset = object.__new__(NurturePresetV1)
        object.__setattr__(invalid_preset, "version", 1)
        object.__setattr__(invalid_preset, "id", "../outside")
        with pytest.raises(NurtureStateError):
            owner.get_targets(invalid_preset)
        assert run.receipt.account_id == account_id
