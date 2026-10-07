from __future__ import annotations

import json
import os
import stat
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from uuid import UUID, uuid4

import pytest

from threads_platform.standalone.nurture import get_nurture_preset
from threads_platform.standalone.nurture_content import (
    ContentCandidateV1,
    NurtureContentError,
    draft_fingerprint,
    load_content_candidate,
    source_fingerprint,
)
from threads_platform.standalone.nurture_store import NurtureStateError, NurtureStore

_NOW = datetime(2026, 10, 7, 4, 0, tzinfo=UTC)
_SOURCE_SENTINEL = "OPERATOR_SOURCE_ID_SENTINEL"
_TEXT_SENTINEL = "THREADS_NATIVE_DRAFT_SENTINEL"
_TOKEN_SENTINEL = "THREADS_TOKEN_SENTINEL"
_MEDIA_SENTINEL = "REMOTE_MEDIA_ID_SENTINEL"


def _document(
    *,
    candidate_id: UUID | None = None,
    source_id: str = _SOURCE_SENTINEL,
    text: str = _TEXT_SENTINEL,
    account: str = "alice",
    preset: str = "recruitment",
    category: str = "CAREER_TIP",
) -> dict[str, object]:
    return {
        "version": 1,
        "action": "POST_TEXT",
        "candidate_id": str(candidate_id or uuid4()),
        "account": account,
        "preset": preset,
        "source_kind": "OPERATOR_SOURCE_ID",
        "source_id": source_id,
        "category": category,
        "text": text,
    }


def _load_packet(tmp_path: Path, **changes: object) -> ContentCandidateV1:
    path = tmp_path / "content.json"
    document = _document()
    document.update(changes)
    if isinstance(document["candidate_id"], UUID):
        document["candidate_id"] = str(document["candidate_id"])
    path.write_text(json.dumps(document), encoding="utf-8")
    return load_content_candidate(path, account_alias="alice", preset_id="recruitment")


def _content_path(root: Path, account_id: UUID) -> Path:
    return root / "nurture" / "content" / str(account_id) / "recruitment.json"


def test_content_packet_is_exact_bound_and_redacts_source_and_text(tmp_path: Path) -> None:
    candidate = _load_packet(tmp_path)

    assert candidate.category == "CAREER_TIP"
    assert candidate.source_fingerprint == source_fingerprint(
        "OPERATOR_SOURCE_ID", _SOURCE_SENTINEL
    )
    assert candidate.draft_fingerprint == draft_fingerprint(_TEXT_SENTINEL)
    assert _SOURCE_SENTINEL not in repr(candidate)
    assert _TEXT_SENTINEL not in repr(candidate)


@pytest.mark.parametrize(
    "document",
    [
        {**_document(), "extra": "field"},
        {**_document(), "version": True},
        {**_document(), "version": 2},
        {**_document(), "action": "REPLY"},
        {**_document(), "candidate_id": str(UUID(int=1))},
        {**_document(), "category": "OTHER"},
        {**_document(), "source_kind": "URL"},
        {**_document(), "source_id": " padded "},
        {**_document(), "source_id": "two\nlines"},
        {**_document(), "source_id": "\x00"},
        {**_document(), "text": " "},
        {**_document(), "text": "x" * 501},
        {**_document(), "account": "other"},
        {**_document(), "preset": "other"},
    ],
)
def test_content_packet_rejects_noncanonical_documents(
    tmp_path: Path,
    document: dict[str, object],
) -> None:
    path = tmp_path / "invalid.json"
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(NurtureContentError) as error:
        load_content_candidate(path, account_alias="alice", preset_id="recruitment")

    assert str(error.value) in {"INVALID_NURTURE_CONTENT", "NURTURE_CONTENT_ACCOUNT_MISMATCH"}
    assert str(path) not in repr(error.value)
    assert _SOURCE_SENTINEL not in repr(error.value)
    assert _TEXT_SENTINEL not in repr(error.value)


def test_content_packet_rejects_duplicate_keys_oversize_and_nonfiles(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text(
        json.dumps(_document()).replace('"version": 1', '"version": 1, "version": 1'),
        encoding="utf-8",
    )
    with pytest.raises(NurtureContentError):
        load_content_candidate(duplicate, account_alias="alice", preset_id="recruitment")

    oversized = tmp_path / "oversized.json"
    oversized.write_bytes(b" " * 16_385)
    with pytest.raises(NurtureContentError):
        load_content_candidate(oversized, account_alias="alice", preset_id="recruitment")
    with pytest.raises(NurtureContentError):
        load_content_candidate(tmp_path, account_alias="alice", preset_id="recruitment")

    link = tmp_path / "link.json"
    original_lstat = Path.lstat
    symlink_metadata = os.stat_result((stat.S_IFLNK | 0o777, 0, 0, 1, 0, 0, 0, 0, 0, 0))

    def lstat_with_link_metadata(path: Path) -> os.stat_result:
        return symlink_metadata if path == link else original_lstat(path)

    monkeypatch.setattr(Path, "lstat", lstat_with_link_metadata)
    with pytest.raises(NurtureContentError):
        load_content_candidate(link, account_alias="alice", preset_id="recruitment")

    class ReparseMetadata:
        st_mode = stat.S_IFREG
        st_file_attributes = 0x400
        st_dev = 0
        st_ino = 0

    reparse = tmp_path / "reparse.json"

    def lstat_with_reparse_metadata(path: Path) -> os.stat_result:
        if path == reparse:
            return cast(os.stat_result, ReparseMetadata())
        return original_lstat(path)

    monkeypatch.setattr(Path, "lstat", lstat_with_reparse_metadata)
    with pytest.raises(NurtureContentError):
        load_content_candidate(reparse, account_alias="alice", preset_id="recruitment")


def test_source_and_draft_fingerprints_are_deterministic_and_namespaced() -> None:
    assert source_fingerprint("OPERATOR_SOURCE_ID", "e\u0301") == source_fingerprint(
        "OPERATOR_SOURCE_ID", "é"
    )
    assert source_fingerprint("OPERATOR_SOURCE_ID", "source-a") != source_fingerprint(
        "OPERATOR_SOURCE_ID", "source-b"
    )
    assert draft_fingerprint("cafe\u0301\r\nTip") == draft_fingerprint("café\nTip")
    assert draft_fingerprint("café\nTip") != draft_fingerprint("café Tip")


def test_content_state_is_local_strict_and_allows_exact_new_reload(tmp_path: Path) -> None:
    root = tmp_path / "standalone"
    root.mkdir()
    account_id = uuid4()
    preset = get_nurture_preset("recruitment")
    store = NurtureStore(root)
    candidate = _load_packet(tmp_path)
    replacement_id = uuid4()
    same_pair = ContentCandidateV1(
        candidate_id=replacement_id,
        account_alias="alice",
        preset_id=preset.id,
        source_id=candidate.source_id,
        category=candidate.category,
        text=candidate.text,
    )

    with store.acquire_account_lock(account_id) as owner:
        run = owner.start_run(preset, now=_NOW)
        record = owner.register_content_candidate(
            preset,
            candidate.candidate_id,
            candidate.source_fingerprint,
            candidate.draft_fingerprint,
            candidate.category,
            run.receipt.id,
            now=_NOW,
        )
        run.finish("SUCCESS", now=_NOW)

        next_run = owner.start_run(preset, now=_NOW)
        same_record = owner.register_content_candidate(
            preset,
            same_pair.candidate_id,
            same_pair.source_fingerprint,
            same_pair.draft_fingerprint,
            same_pair.category,
            next_run.receipt.id,
            now=_NOW,
        )
        next_run.finish("SUCCESS", now=_NOW)

    path = _content_path(root, account_id)
    persisted = path.read_text(encoding="utf-8")
    document = json.loads(persisted)
    assert set(document) == {"version", "account_id", "preset_id", "candidates"}
    assert len(document["candidates"]) == 1
    assert set(document["candidates"][0]) == {
        "candidate_id",
        "source_fingerprint",
        "draft_fingerprint",
        "category",
        "first_seen_at",
        "last_action_at",
        "publication_state",
        "last_run_id",
        "operation_id",
    }
    assert same_record.candidate_id == record.candidate_id
    for secret in (_SOURCE_SENTINEL, _TEXT_SENTINEL, _TOKEN_SENTINEL, _MEDIA_SENTINEL):
        assert secret not in persisted


@pytest.mark.parametrize(
    ("second_source", "second_text", "expected_code"),
    [
        (_SOURCE_SENTINEL, "changed draft", "NURTURE_CONTENT_PROVENANCE_CONFLICT"),
        ("changed source", _TEXT_SENTINEL, "NURTURE_CONTENT_DRAFT_REUSE_CONFLICT"),
    ],
)
def test_new_content_source_or_draft_collision_fails_closed(
    tmp_path: Path,
    second_source: str,
    second_text: str,
    expected_code: str,
) -> None:
    root = tmp_path / "standalone"
    root.mkdir()
    account_id = uuid4()
    preset = get_nurture_preset("recruitment")
    store = NurtureStore(root)
    first = _load_packet(tmp_path)
    second = _load_packet(
        tmp_path,
        candidate_id=uuid4(),
        source_id=second_source,
        text=second_text,
    )

    with store.acquire_account_lock(account_id) as owner:
        first_run = owner.start_run(preset, now=_NOW)
        owner.register_content_candidate(
            preset,
            first.candidate_id,
            first.source_fingerprint,
            first.draft_fingerprint,
            first.category,
            first_run.receipt.id,
            now=_NOW,
        )
        first_run.finish("SUCCESS", now=_NOW)

        second_run = owner.start_run(preset, now=_NOW)
        with pytest.raises(NurtureStateError) as error:
            owner.register_content_candidate(
                preset,
                second.candidate_id,
                second.source_fingerprint,
                second.draft_fingerprint,
                second.category,
                second_run.receipt.id,
                now=_NOW,
            )
        assert error.value.code == expected_code
        second_run.finish("FAILED", error_code=expected_code, failed_stage="content", now=_NOW)


def test_reserved_and_ambiguous_content_are_never_retryable(tmp_path: Path) -> None:
    root = tmp_path / "standalone"
    root.mkdir()
    account_id = uuid4()
    preset = get_nurture_preset("recruitment")
    store = NurtureStore(root)
    candidate = _load_packet(tmp_path)
    operation_id = uuid4()

    with store.acquire_account_lock(account_id) as owner:
        run = owner.start_run(preset, now=_NOW)
        owner.register_content_candidate(
            preset,
            candidate.candidate_id,
            candidate.source_fingerprint,
            candidate.draft_fingerprint,
            candidate.category,
            run.receipt.id,
            now=_NOW,
        )
        owner.reserve_content_candidate(
            preset,
            candidate.source_fingerprint,
            candidate.draft_fingerprint,
            run.receipt.id,
        )
        owner.complete_content_candidate(
            preset,
            candidate.source_fingerprint,
            candidate.draft_fingerprint,
            run.receipt.id,
            "AMBIGUOUS",
            operation_id,
            now=_NOW,
        )
        run.finish("AMBIGUOUS", error_code="PUBLISH_OUTCOME_AMBIGUOUS", failed_stage="publish")

        retry = owner.start_run(preset, now=_NOW)
        with pytest.raises(NurtureStateError) as error:
            owner.register_content_candidate(
                preset,
                candidate.candidate_id,
                candidate.source_fingerprint,
                candidate.draft_fingerprint,
                candidate.category,
                retry.receipt.id,
                now=_NOW,
            )
        assert error.value.code == "NURTURE_CONTENT_UNRESOLVED"
        retry.finish("FAILED", error_code=error.value.code, failed_stage="content")

    persisted = _content_path(root, account_id).read_text(encoding="utf-8")
    record = json.loads(persisted)["candidates"][0]
    assert record["publication_state"] == "AMBIGUOUS"
    assert record["operation_id"] == str(operation_id)
    assert _SOURCE_SENTINEL not in persisted
    assert _TEXT_SENTINEL not in persisted


@pytest.mark.parametrize(
    ("publication_state", "expected_code"),
    [
        ("RESERVED", "NURTURE_CONTENT_UNRESOLVED"),
        ("PUBLISHED", "NURTURE_CONTENT_ALREADY_USED"),
    ],
)
def test_reserved_and_published_content_are_non_retryable(
    tmp_path: Path,
    publication_state: str,
    expected_code: str,
) -> None:
    root = tmp_path / "standalone"
    root.mkdir()
    account_id = uuid4()
    preset = get_nurture_preset("recruitment")
    store = NurtureStore(root)
    candidate = _load_packet(tmp_path)
    with store.acquire_account_lock(account_id) as owner:
        first = owner.start_run(preset, now=_NOW)
        owner.register_content_candidate(
            preset,
            candidate.candidate_id,
            candidate.source_fingerprint,
            candidate.draft_fingerprint,
            candidate.category,
            first.receipt.id,
            now=_NOW,
        )
        owner.reserve_content_candidate(
            preset,
            candidate.source_fingerprint,
            candidate.draft_fingerprint,
            first.receipt.id,
        )
        if publication_state == "PUBLISHED":
            owner.complete_content_candidate(
                preset,
                candidate.source_fingerprint,
                candidate.draft_fingerprint,
                first.receipt.id,
                "PUBLISHED",
                uuid4(),
                now=_NOW,
            )
            first.finish("SUCCESS", now=_NOW)
        else:
            first.finish("FAILED", error_code="RUN_FAILED", failed_stage="test", now=_NOW)

        second = owner.start_run(preset, now=_NOW)
        with pytest.raises(NurtureStateError) as error:
            owner.register_content_candidate(
                preset,
                candidate.candidate_id,
                candidate.source_fingerprint,
                candidate.draft_fingerprint,
                candidate.category,
                second.receipt.id,
                now=_NOW,
            )
        assert error.value.code == expected_code
        second.finish("FAILED", error_code=expected_code, failed_stage="content", now=_NOW)


def test_content_state_failed_atomic_update_keeps_previous_document(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "standalone"
    root.mkdir()
    account_id = uuid4()
    preset = get_nurture_preset("recruitment")
    store = NurtureStore(root)
    candidate = _load_packet(tmp_path)
    with store.acquire_account_lock(account_id) as owner:
        first = owner.start_run(preset, now=_NOW)
        owner.register_content_candidate(
            preset,
            candidate.candidate_id,
            candidate.source_fingerprint,
            candidate.draft_fingerprint,
            candidate.category,
            first.receipt.id,
            now=_NOW,
        )
        first.finish("SUCCESS", now=_NOW)
        path = _content_path(root, account_id)
        previous = path.read_bytes()

        second = owner.start_run(preset, now=_NOW)

        def fail_replace(*_: object) -> None:
            raise NurtureStateError("NURTURE_STATE_INVALID")

        with monkeypatch.context() as patch:
            patch.setattr(store, "_write_replace", fail_replace)
            with pytest.raises(NurtureStateError):
                owner.register_content_candidate(
                    preset,
                    candidate.candidate_id,
                    candidate.source_fingerprint,
                    candidate.draft_fingerprint,
                    candidate.category,
                    second.receipt.id,
                    now=_NOW,
                )
        assert path.read_bytes() == previous
        second.finish("FAILED", error_code="NURTURE_STATE_INVALID", failed_stage="content")


@pytest.mark.parametrize(
    "document",
    [
        '{"version":true,"account_id":"x","preset_id":"recruitment","candidates":[]}',
        '{"version":1,"account_id":"x","preset_id":"recruitment","candidates":[],"extra":1}',
        '{"version":1,"version":1,"account_id":"x","preset_id":"recruitment","candidates":[]}',
    ],
)
def test_content_state_rejects_invalid_version_duplicate_and_extra_fields(
    tmp_path: Path,
    document: str,
) -> None:
    root = tmp_path / "standalone"
    account_id = uuid4()
    preset = get_nurture_preset("recruitment")
    store = NurtureStore(root)
    path = _content_path(root, account_id)
    path.parent.mkdir(parents=True)
    path.write_text(document, encoding="utf-8")

    with store.acquire_account_lock(account_id) as owner:
        with pytest.raises(NurtureStateError):
            owner.get_content_state(preset)


def test_content_path_symlink_and_oversize_state_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "standalone"
    account_id = uuid4()
    preset = get_nurture_preset("recruitment")
    store = NurtureStore(root)
    path = _content_path(root, account_id)
    path.parent.mkdir(parents=True)
    path.write_text(
        '{"version":1,"account_id":"x","preset_id":"recruitment","candidates":[]}', encoding="utf-8"
    )

    with store.acquire_account_lock(account_id) as owner:
        original_lstat = Path.lstat
        symlink_metadata = os.stat_result((stat.S_IFLNK | 0o777, 0, 0, 1, 0, 0, 0, 0, 0, 0))

        def lstat_with_link_metadata(target: Path) -> os.stat_result:
            return symlink_metadata if target == path.parent else original_lstat(target)

        monkeypatch.setattr(Path, "lstat", lstat_with_link_metadata)
        with pytest.raises(NurtureStateError):
            owner.get_content_state(preset)


def test_content_state_oversize_document_fails_closed(tmp_path: Path) -> None:
    root = tmp_path / "standalone"
    account_id = uuid4()
    preset = get_nurture_preset("recruitment")
    store = NurtureStore(root)
    path = _content_path(root, account_id)
    path.parent.mkdir(parents=True)
    path.write_bytes(b" " * 2_097_153)

    with store.acquire_account_lock(account_id) as owner:
        with pytest.raises(NurtureStateError) as error:
            owner.get_content_state(preset)
    assert error.value.code == "NURTURE_STATE_INVALID"
