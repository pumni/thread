from __future__ import annotations

import json
import os
import stat
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast
from uuid import UUID, uuid4

import pytest

import threads_platform.standalone.__main__ as cli_module
from threads_platform.application.ports.threads import (
    DiscoveryPage,
    PublishingQuota,
    RemoteDiscoveryThread,
    RemoteReply,
    ReplyPage,
)
from threads_platform.standalone.accounts import LocalAccount, LocalAccountStore
from threads_platform.standalone.api import LocalThreadsApiRuntime
from threads_platform.standalone.app import StandaloneAppContext
from threads_platform.standalone.mutations import (
    CreatedReplyResult,
    LocalThreadsMutationRuntime,
    PublishedTextResult,
    StandaloneMutationError,
)
from threads_platform.standalone.nurture import (
    NurturePresetV1,
    get_nurture_preset,
    nurture_quote_allowed,
)
from threads_platform.standalone.nurture_draft import (
    NurtureDraft,
    NurtureDraftError,
    load_nurture_draft,
)
from threads_platform.standalone.nurture_quote_draft import (
    NurtureQuoteDraft,
    NurtureQuoteDraftError,
    load_nurture_quote_draft,
)
from threads_platform.standalone.nurture_runner import (
    NurtureRunner,
    NurtureRunnerError,
    NurtureRunResult,
)
from threads_platform.standalone.nurture_store import (
    NurtureAccountLock,
    NurtureStateError,
    NurtureStore,
    fingerprint_remote_reply,
    fingerprint_remote_thread,
)

_NOW = datetime(2026, 10, 7, 4, 0, tzinfo=UTC)
_DRAFT_TEXT = "OPERATOR_DRAFT_TEXT_SENTINEL"
_REMOTE_TEXT = "REMOTE_POST_TEXT_SENTINEL career?"
_THREAD_ID = "REMOTE_THREAD_ID_SENTINEL"
_ROOT_ID = "OWN_ROOT_THREAD_ID_SENTINEL"
_REPLY_ID = "REMOTE_REPLY_ID_SENTINEL"
_TOKEN = "THREADS_TOKEN_SENTINEL"
_CREDENTIAL_REF = "env://THREADS_CREDENTIAL_SENTINEL"
_QUOTE_TEXT = "OPERATOR_QUOTE_TEXT_SENTINEL"
_OWN_THREAD_ID = "OWN_THREAD_ID_SENTINEL"
_INBOUND_REPLY_ID = "INBOUND_REPLY_ID_SENTINEL"
_QUOTE_MEDIA_ID = "REMOTE_QUOTE_MEDIA_ID_SENTINEL"


def _preset(*, owner: str | None = None, cooldown: int = 604_800) -> NurturePresetV1:
    return replace(
        get_nurture_preset("recruitment"),
        keyword_queries=("career query sentinel",),
        tag_queries=(),
        include_terms=("career",),
        exclude_terms=(),
        watched_public_usernames=(),
        max_total_discovery_candidates=8,
        max_selected_candidates=3,
        max_browser_enrichments=3,
        seen_cooldown_seconds=cooldown,
        owner_public_username=owner,
        max_owned_threads_inspected_per_explicit_run=1 if owner else 0,
    )


def _thread(remote_id: str, text: str = _REMOTE_TEXT) -> RemoteDiscoveryThread:
    return RemoteDiscoveryThread(remote_id, None, None, text, None, None, _NOW, None, False)


class _Api:
    def __init__(
        self,
        threads: tuple[RemoteDiscoveryThread, ...] = (_thread(_THREAD_ID),),
        *,
        quota: PublishingQuota | None = None,
        owned_threads: tuple[RemoteDiscoveryThread, ...] = (),
        conversations: dict[str, ReplyPage] | None = None,
    ) -> None:
        self.threads = threads
        self.quota_value = quota or PublishingQuota()
        self.owned_threads = owned_threads
        self.conversations = conversations or {}
        self.calls: list[tuple[object, ...]] = []

    async def mentions(self, alias: str, *, after: str | None, limit: int) -> DiscoveryPage:
        self.calls.append(("mentions", alias, after, limit))
        return DiscoveryPage(self.threads, "cursor sentinel", True)

    async def search(
        self,
        alias: str,
        query: str,
        *,
        search_mode: object,
        search_type: object,
        after: str | None,
        limit: int,
    ) -> DiscoveryPage:
        self.calls.append(("search", alias, query, after, limit))
        return DiscoveryPage((), "cursor sentinel", True)

    async def profile_posts(
        self,
        alias: str,
        username: str,
        *,
        after: str | None,
        limit: int,
    ) -> DiscoveryPage:
        self.calls.append(("profile_posts", alias, username, after, limit))
        return DiscoveryPage(self.owned_threads, "profile cursor sentinel", True)

    async def conversation(self, alias: str, thread_id: str, *, after: str | None) -> ReplyPage:
        self.calls.append(("conversation", alias, thread_id, after))
        return self.conversations.get(thread_id, ReplyPage((), "reply cursor sentinel", True))

    async def quota(self, alias: str) -> PublishingQuota:
        self.calls.append(("quota", alias))
        return self.quota_value


class _Mutations:
    def __init__(
        self,
        *,
        operation_id: UUID | None = None,
        failure: StandaloneMutationError | None = None,
        on_create: Callable[[], None] | None = None,
    ) -> None:
        self.operation_id = operation_id or uuid4()
        self.failure = failure
        self.on_create = on_create
        self.calls: list[tuple[str, str, str, str | None]] = []
        self.quote_calls: list[tuple[str, str, str]] = []

    async def create_reply(
        self,
        alias: str,
        thread_id: str,
        text: str,
        *,
        parent_reply_id: str | None = None,
    ) -> CreatedReplyResult:
        self.calls.append((alias, thread_id, text, parent_reply_id))
        if self.on_create is not None:
            self.on_create()
        if self.failure is not None:
            raise self.failure
        return CreatedReplyResult(self.operation_id, "CREATED_REMOTE_REPLY_ID_SENTINEL")

    async def publish_quote(
        self,
        alias: str,
        quote_post_id: str,
        text: str,
    ) -> PublishedTextResult:
        self.quote_calls.append((alias, quote_post_id, text))
        if self.on_create is not None:
            self.on_create()
        if self.failure is not None:
            raise self.failure
        return PublishedTextResult(self.operation_id, _QUOTE_MEDIA_ID)


def _setup(
    tmp_path: Path,
    *,
    preset: NurturePresetV1 | None = None,
) -> tuple[Path, NurtureStore, LocalAccount, NurturePresetV1]:
    root = tmp_path / "standalone"
    account = LocalAccountStore(root).add("alice")
    return root, NurtureStore(root), account, preset or _preset()


def _draft(fingerprint: str, text: str = _DRAFT_TEXT) -> NurtureDraft:
    return NurtureDraft(target_fingerprint=fingerprint, text=text)


async def _run_apply(
    api: _Api,
    store: NurtureStore,
    account: LocalAccount,
    preset: NurturePresetV1,
    draft: NurtureDraft,
    mutations: _Mutations,
) -> NurtureRunResult:
    return await NurtureRunner(cast(LocalThreadsApiRuntime, api), store).run_with_selection(
        account,
        preset,
        now=_NOW,
        apply_requested=True,
        draft=draft,
        mutations=cast(LocalThreadsMutationRuntime, mutations),
    )


async def _run_quote_apply(
    api: _Api,
    store: NurtureStore,
    account: LocalAccount,
    preset: NurturePresetV1,
    quote_draft: NurtureQuoteDraft,
    mutations: _Mutations,
) -> NurtureRunResult:
    return await NurtureRunner(cast(LocalThreadsApiRuntime, api), store).run_with_selection(
        account,
        preset,
        now=_NOW,
        apply_requested=True,
        quote_draft=quote_draft,
        mutations=cast(LocalThreadsMutationRuntime, mutations),
    )


def _target_state_json(
    root: Path, account: LocalAccount, preset: NurturePresetV1
) -> dict[str, Any]:
    path = root / "nurture" / "state" / str(account.id) / f"{preset.id}.json"
    return cast(dict[str, Any], json.loads(path.read_text(encoding="utf-8")))


def _all_nurture_json(root: Path) -> str:
    return "\n".join(
        path.read_text(encoding="utf-8") for path in (root / "nurture").rglob("*.json")
    )


def _seed_seen(
    store: NurtureStore,
    account: LocalAccount,
    preset: NurturePresetV1,
    fingerprint: str,
    *,
    last_seen_at: datetime = _NOW,
) -> None:
    with store.acquire_account_lock(account.id) as owner:
        scope = owner.start_run(preset, now=_NOW)
        owner.observe_target(
            preset,
            fingerprint,
            scope.receipt.id,
            "OBSERVE_ONLY",
            now=last_seen_at,
        )
        scope.finish("SUCCESS", now=_NOW)


def test_draft_file_is_exact_bounded_and_repr_hides_text(tmp_path: Path) -> None:
    fingerprint = fingerprint_remote_thread(_THREAD_ID)
    path = tmp_path / "draft.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "action": "REPLY",
                "target_fingerprint": fingerprint,
                "text": _DRAFT_TEXT,
            }
        ),
        encoding="utf-8",
    )

    draft = load_nurture_draft(path)

    assert draft.target_fingerprint == fingerprint
    assert draft.action == "REPLY"
    assert draft.source == "OPERATOR_FILE"
    assert _DRAFT_TEXT not in repr(draft)


@pytest.mark.parametrize(
    "document",
    [
        '{"version":1,"action":"REPLY","target_fingerprint":"'
        + "a" * 64
        + '","text":"x","extra":1}',
        '{"version":1,"version":1,"action":"REPLY","target_fingerprint":"'
        + "a" * 64
        + '","text":"x"}',
        '{"version":true,"action":"REPLY","target_fingerprint":"' + "a" * 64 + '","text":"x"}',
        '{"version":1,"action":"REPLY","target_fingerprint":"' + "A" * 64 + '","text":"x"}',
        '{"version":1,"action":"REPLY","target_fingerprint":"' + "a" * 64 + '","text":"   "}',
    ],
)
def test_draft_file_rejects_noncanonical_or_invalid_documents(
    tmp_path: Path,
    document: str,
) -> None:
    path = tmp_path / "draft.json"
    path.write_text(document, encoding="utf-8")
    with pytest.raises(NurtureDraftError) as error:
        load_nurture_draft(path)
    assert str(error.value) == "INVALID_NURTURE_DRAFT"
    assert str(path) not in repr(error.value)
    assert document not in repr(error.value)


def test_draft_file_rejects_oversize_nonfile_and_symlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    oversized = tmp_path / "oversized.json"
    oversized.write_bytes(b" " * 8193)
    with pytest.raises(NurtureDraftError):
        load_nurture_draft(oversized)

    with pytest.raises(NurtureDraftError):
        load_nurture_draft(tmp_path)

    link = tmp_path / "link.json"
    original_lstat = Path.lstat
    symlink_metadata = os.stat_result((stat.S_IFLNK | 0o777, 0, 0, 1, 0, 0, 0, 0, 0, 0))

    def lstat_with_link_metadata(path: Path) -> os.stat_result:
        return symlink_metadata if path == link else original_lstat(path)

    monkeypatch.setattr(Path, "lstat", lstat_with_link_metadata)
    with pytest.raises(NurtureDraftError):
        load_nurture_draft(link)


def test_reply_file_without_apply_fails_before_composition(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def forbidden_builder(*_args: object, **_kwargs: object) -> object:
        pytest.fail("invalid CLI preflight must not compose an app")

    monkeypatch.setattr(cli_module, "build_standalone_app", forbidden_builder)
    monkeypatch.setenv("THREADS_LOCAL_DATA_ROOT", str(tmp_path / "standalone"))
    assert (
        cli_module.main(
            ["nurture", "run", "alice", "--preset", "recruitment", "--reply-file", "draft.json"]
        )
        == 1
    )
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err == "ERROR INVALID_NURTURE_DRAFT\n"

    root = tmp_path / "standalone"
    LocalAccountStore(root).add("alice")
    invalid_file = tmp_path / "invalid-draft.json"
    invalid_file.write_text("{}", encoding="utf-8")
    assert (
        cli_module.main(
            [
                "nurture",
                "run",
                "alice",
                "--preset",
                "recruitment",
                "--apply",
                "--reply-file",
                str(invalid_file),
            ]
        )
        == 1
    )
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err == "ERROR INVALID_NURTURE_DRAFT\n"
    assert not (root / "nurture").exists()


@pytest.mark.asyncio
async def test_apply_reserves_before_exactly_one_generic_root_reply_and_confirms(
    tmp_path: Path,
) -> None:
    root, store, account, preset = _setup(tmp_path)
    fingerprint = fingerprint_remote_thread(_THREAD_ID)
    api = _Api()
    mutations = _Mutations()

    def assert_pending_is_durable() -> None:
        state = _target_state_json(root, account, preset)
        target = state["targets"][0]
        assert target["fingerprint"] == fingerprint
        assert target["action_state"] == "PENDING"
        assert target["last_operation_id"] is None

    mutations.on_create = assert_pending_is_durable
    result = await _run_apply(api, store, account, preset, _draft(fingerprint), mutations)

    assert len(mutations.calls) == 1
    assert mutations.calls[0] == ("alice", _THREAD_ID, _DRAFT_TEXT, None)
    assert api.calls[-1] == ("quota", "alice")
    receipt = result.receipt
    assert receipt.outcome == "SUCCESS"
    assert receipt.selected_count == 1
    assert receipt.replied_count == 1
    assert receipt.operation_ids == (mutations.operation_id,)
    assert result.target_fingerprint == fingerprint
    target = _target_state_json(root, account, preset)["targets"][0]
    assert target["action_state"] == "CONFIRMED"
    assert target["last_operation_id"] == str(mutations.operation_id)
    persisted = _all_nurture_json(root)
    for secret in (
        _DRAFT_TEXT,
        _THREAD_ID,
        "CREATED_REMOTE_REPLY_ID_SENTINEL",
        _TOKEN,
        _CREDENTIAL_REF,
    ):
        assert secret not in persisted


@pytest.mark.asyncio
async def test_inbound_apply_uses_root_thread_and_selected_reply_as_parent(tmp_path: Path) -> None:
    preset = _preset(owner="configured_owner")
    root, store, account, preset = _setup(tmp_path, preset=preset)
    api = _Api(
        threads=(),
        owned_threads=(_thread(_ROOT_ID),),
        conversations={
            _ROOT_ID: ReplyPage(
                (
                    RemoteReply(
                        _REPLY_ID,
                        "reply text sentinel",
                        _NOW.isoformat(),
                        _ROOT_ID,
                        "parent-other",
                        False,
                    ),
                ),
                "cursor sentinel",
                True,
            )
        },
    )
    fingerprint = fingerprint_remote_reply(_REPLY_ID)
    mutations = _Mutations()

    await _run_apply(api, store, account, preset, _draft(fingerprint), mutations)

    assert mutations.calls == [("alice", _ROOT_ID, _DRAFT_TEXT, _REPLY_ID)]
    assert not any(call[0] == "mentions" for call in api.calls)
    persisted = _all_nurture_json(root)
    for secret in (_ROOT_ID, _REPLY_ID, "parent-other", "reply text sentinel", _DRAFT_TEXT):
        assert secret not in persisted


@pytest.mark.asyncio
async def test_recently_observed_exact_target_can_apply_but_stale_or_other_target_cannot(
    tmp_path: Path,
) -> None:
    root, store, account, preset = _setup(tmp_path)
    fingerprint = fingerprint_remote_thread(_THREAD_ID)
    _seed_seen(store, account, preset, fingerprint)
    mutations = _Mutations()

    result = await _run_apply(_Api(), store, account, preset, _draft(fingerprint), mutations)

    assert result.receipt.outcome == "SUCCESS"
    assert len(mutations.calls) == 1
    assert _target_state_json(root, account, preset)["targets"][0]["action_state"] == "CONFIRMED"

    other_root, other_store, other_account, other_preset = _setup(tmp_path / "other")
    other_api = _Api()
    other_mutations = _Mutations()
    with pytest.raises(NurtureRunnerError) as error:
        await _run_apply(
            other_api,
            other_store,
            other_account,
            other_preset,
            _draft(fingerprint_remote_thread("stale-target")),
            other_mutations,
        )
    assert error.value.code == "NURTURE_DRAFT_TARGET_MISMATCH"
    assert other_mutations.calls == []
    assert not (
        other_root / "nurture" / "state" / str(other_account.id) / f"{other_preset.id}.json"
    ).exists()


@pytest.mark.asyncio
async def test_future_seen_timestamp_does_not_use_apply_cooldown_bypass(tmp_path: Path) -> None:
    _, store, account, preset = _setup(tmp_path)
    fingerprint = fingerprint_remote_thread(_THREAD_ID)
    _seed_seen(store, account, preset, fingerprint, last_seen_at=_NOW.replace(day=15))
    mutations = _Mutations()

    with pytest.raises(NurtureRunnerError) as error:
        await _run_apply(_Api(), store, account, preset, _draft(fingerprint), mutations)

    assert error.value.code == "NURTURE_DRAFT_TARGET_MISMATCH"
    assert mutations.calls == []


@pytest.mark.asyncio
async def test_higher_priority_current_candidate_makes_bound_draft_mismatch(tmp_path: Path) -> None:
    root, store, account, preset = _setup(tmp_path)
    later = replace(_thread("bound-lower-priority"), timestamp=_NOW.replace(day=1))
    api = _Api(threads=(_thread("higher-priority"), later))
    mutations = _Mutations()

    with pytest.raises(NurtureRunnerError) as error:
        await _run_apply(
            api,
            store,
            account,
            preset,
            _draft(fingerprint_remote_thread("bound-lower-priority")),
            mutations,
        )

    assert error.value.code == "NURTURE_DRAFT_TARGET_MISMATCH"
    assert mutations.calls == []
    assert not (root / "nurture" / "state" / str(account.id) / f"{preset.id}.json").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("action_state", ["PENDING", "AMBIGUOUS", "CONFIRMED"])
async def test_reserved_ambiguous_and_confirmed_targets_are_never_apply_eligible(
    tmp_path: Path,
    action_state: str,
) -> None:
    root, store, account, preset = _setup(tmp_path)
    fingerprint = fingerprint_remote_thread(_THREAD_ID)
    with store.acquire_account_lock(account.id) as owner:
        scope = owner.start_run(preset, now=_NOW)
        owner.reserve_target(preset, fingerprint, scope.receipt.id, "REPLY_APPLY", now=_NOW)
        if action_state != "PENDING":
            terminal_state = "AMBIGUOUS" if action_state == "AMBIGUOUS" else "CONFIRMED"
            owner.complete_target_action(
                preset,
                fingerprint,
                scope.receipt.id,
                terminal_state,
                uuid4(),
                now=_NOW,
            )
        scope.finish("SUCCESS", now=_NOW)
    api = _Api()
    mutations = _Mutations()

    with pytest.raises(NurtureRunnerError) as error:
        await _run_apply(api, store, account, preset, _draft(fingerprint), mutations)

    assert error.value.code == "NURTURE_DRAFT_TARGET_MISMATCH"
    assert api.calls == []
    assert mutations.calls == []
    assert _target_state_json(root, account, preset)["targets"][0]["action_state"] == action_state


@pytest.mark.asyncio
async def test_known_exhausted_reply_quota_fails_before_pending_or_mutation(tmp_path: Path) -> None:
    root, store, account, preset = _setup(tmp_path)
    api = _Api(quota=PublishingQuota(reply_usage=100, reply_total=100))
    mutations = _Mutations()

    with pytest.raises(NurtureRunnerError) as error:
        await _run_apply(
            api,
            store,
            account,
            preset,
            _draft(fingerprint_remote_thread(_THREAD_ID)),
            mutations,
        )

    assert error.value.code == "THREADS_REPLY_QUOTA_REACHED"
    assert mutations.calls == []
    state_path = root / "nurture" / "state" / str(account.id) / f"{preset.id}.json"
    assert not state_path.exists()


@pytest.mark.asyncio
async def test_exact_ambiguous_runtime_signal_persists_ambiguous_and_never_retries(
    tmp_path: Path,
) -> None:
    root, store, account, preset = _setup(tmp_path)
    operation_id = uuid4()
    mutations = _Mutations(
        failure=StandaloneMutationError("PUBLISH_OUTCOME_AMBIGUOUS", operation_id)
    )

    with pytest.raises(NurtureRunnerError) as error:
        await _run_apply(
            _Api(),
            store,
            account,
            preset,
            _draft(fingerprint_remote_thread(_THREAD_ID)),
            mutations,
        )

    assert error.value.code == "PUBLISH_OUTCOME_AMBIGUOUS"
    assert error.value.operation_id == operation_id
    assert len(mutations.calls) == 1
    state = _target_state_json(root, account, preset)
    assert state["targets"][0]["action_state"] == "AMBIGUOUS"
    assert state["targets"][0]["last_operation_id"] == str(operation_id)
    with store.acquire_account_lock(account.id) as owner:
        assert error.value.run_id is not None
        run = owner.get_run(error.value.run_id)
    assert run.outcome == "AMBIGUOUS"
    assert run.operation_ids == (operation_id,)


@pytest.mark.parametrize("failure_code", ["THREADS_INVALID_REQUEST", "THREADS_TRANSPORT_FAILURE"])
@pytest.mark.asyncio
async def test_nonambiguous_failure_keeps_pending_and_state_write_failure_never_retries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_code: str,
) -> None:
    root, store, account, preset = _setup(tmp_path)
    mutations = _Mutations(failure=StandaloneMutationError(failure_code))
    with pytest.raises(NurtureRunnerError) as error:
        await _run_apply(
            _Api(),
            store,
            account,
            preset,
            _draft(fingerprint_remote_thread(_THREAD_ID)),
            mutations,
        )
    assert error.value.code == failure_code
    assert len(mutations.calls) == 1
    assert _target_state_json(root, account, preset)["targets"][0]["action_state"] == "PENDING"
    assert error.value.run_id is not None
    with store.acquire_account_lock(account.id) as owner:
        run = owner.get_run(error.value.run_id)
    assert run.outcome == "FAILED"
    assert run.error_code == failure_code

    second_root, second_store, second_account, second_preset = _setup(tmp_path / "second")
    second_mutations = _Mutations()

    def fail_confirm(*_args: object, **_kwargs: object) -> None:
        raise NurtureStateError("NURTURE_STATE_INVALID")

    monkeypatch.setattr(NurtureAccountLock, "complete_target_action", fail_confirm)
    with pytest.raises(NurtureRunnerError):
        await _run_apply(
            _Api(),
            second_store,
            second_account,
            second_preset,
            _draft(fingerprint_remote_thread(_THREAD_ID)),
            second_mutations,
        )
    assert len(second_mutations.calls) == 1
    state = _target_state_json(second_root, second_account, second_preset)
    assert state["targets"][0]["action_state"] == "PENDING"


def test_cli_invalid_draft_does_not_compose_mutations_and_success_exposes_only_fingerprint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "standalone"
    monkeypatch.setenv("THREADS_LOCAL_DATA_ROOT", str(root))
    accounts = LocalAccountStore(root)
    accounts.add("alice")
    fingerprint = fingerprint_remote_thread(_THREAD_ID)
    draft_file = tmp_path / "draft.json"
    draft_file.write_text(
        json.dumps(
            {
                "version": 1,
                "action": "REPLY",
                "target_fingerprint": fingerprint,
                "text": _DRAFT_TEXT,
            }
        ),
        encoding="utf-8",
    )
    api = _Api()
    mutations = _Mutations()
    flags: list[tuple[bool, bool, bool]] = []

    @asynccontextmanager
    async def fake_builder(
        _root: Path,
        local_accounts: LocalAccountStore,
        *,
        include_api: bool = True,
        include_mutations: bool = True,
        include_browser: bool = True,
    ) -> AsyncGenerator[StandaloneAppContext]:
        flags.append((include_api, include_mutations, include_browser))
        yield StandaloneAppContext(
            local_accounts,
            cast(LocalThreadsApiRuntime, api),
            cast(LocalThreadsMutationRuntime, mutations) if include_mutations else None,
            None,
        )

    monkeypatch.setattr(cli_module, "build_standalone_app", fake_builder)

    assert (
        cli_module.main(
            [
                "nurture",
                "run",
                "alice",
                "--preset",
                "recruitment",
                "--apply",
                "--reply-file",
                str(draft_file),
            ]
        )
        == 0
    )
    output = capsys.readouterr()
    assert flags == [(True, True, False)]
    assert f"target={fingerprint}" in output.out
    for secret in (_THREAD_ID, _REMOTE_TEXT, _DRAFT_TEXT, _TOKEN, _CREDENTIAL_REF):
        assert secret not in output.out + output.err


def test_apply_without_draft_is_api_only_recommendation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "standalone"
    monkeypatch.setenv("THREADS_LOCAL_DATA_ROOT", str(root))
    accounts = LocalAccountStore(root)
    accounts.add("alice")
    api = _Api()
    flags: list[tuple[bool, bool, bool]] = []

    @asynccontextmanager
    async def fake_builder(
        _root: Path,
        local_accounts: LocalAccountStore,
        *,
        include_api: bool = True,
        include_mutations: bool = True,
        include_browser: bool = True,
    ) -> AsyncGenerator[StandaloneAppContext]:
        flags.append((include_api, include_mutations, include_browser))
        yield StandaloneAppContext(local_accounts, cast(LocalThreadsApiRuntime, api), None, None)

    monkeypatch.setattr(cli_module, "build_standalone_app", fake_builder)

    assert cli_module.main(["nurture", "run", "alice", "--preset", "recruitment", "--apply"]) == 0
    output = capsys.readouterr()
    assert flags == [(True, False, False)]
    assert "decision=REPLY_RECOMMENDED_NO_DRAFT" in output.out
    assert f"target={fingerprint_remote_thread(_THREAD_ID)}" in output.out
    assert output.err == ""


def test_quote_draft_is_exact_bounded_and_redacts_commentary(tmp_path: Path) -> None:
    fingerprint = fingerprint_remote_thread(_THREAD_ID)
    path = tmp_path / "quote.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "action": "QUOTE",
                "target_fingerprint": fingerprint,
                "text": _QUOTE_TEXT,
            }
        ),
        encoding="utf-8",
    )

    draft = load_nurture_quote_draft(path)

    assert draft.target_fingerprint == fingerprint
    assert draft.action == "QUOTE"
    assert _QUOTE_TEXT not in repr(draft)


@pytest.mark.parametrize(
    "document",
    [
        '{"version":1,"action":"QUOTE","target_fingerprint":"'
        + "a" * 64
        + '","text":"x","extra":1}',
        '{"version":1,"version":1,"action":"QUOTE","target_fingerprint":"'
        + "a" * 64
        + '","text":"x"}',
        '{"version":true,"action":"QUOTE","target_fingerprint":"' + "a" * 64 + '","text":"x"}',
        '{"version":1,"action":"REPLY","target_fingerprint":"' + "a" * 64 + '","text":"x"}',
        '{"version":1,"action":"QUOTE","target_fingerprint":"' + "A" * 64 + '","text":"x"}',
        '{"version":1,"action":"QUOTE","target_fingerprint":"' + "a" * 64 + '","text":1}',
        '{"version":1,"action":"QUOTE","target_fingerprint":"' + "a" * 64 + '","text":"   "}',
        '{"version":1,"action":"QUOTE","target_fingerprint":"'
        + "a" * 64
        + '","text":"'
        + "x" * 501
        + '"}',
    ],
)
def test_quote_draft_rejects_extra_duplicate_coerced_and_invalid_fields(
    tmp_path: Path,
    document: str,
) -> None:
    path = tmp_path / "invalid-quote.json"
    path.write_text(document, encoding="utf-8")

    with pytest.raises(NurtureQuoteDraftError) as caught:
        load_nurture_quote_draft(path)

    assert str(caught.value) == "INVALID_NURTURE_QUOTE_DRAFT"
    assert str(path) not in repr(caught.value)
    assert document not in repr(caught.value)


def test_quote_draft_rejects_oversize_nonfile_symlink_and_reparse_point(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    oversized = tmp_path / "oversized-quote.json"
    oversized.write_bytes(b" " * 8_193)
    with pytest.raises(NurtureQuoteDraftError):
        load_nurture_quote_draft(oversized)
    with pytest.raises(NurtureQuoteDraftError):
        load_nurture_quote_draft(tmp_path)

    link = tmp_path / "quote-link.json"
    original_lstat = Path.lstat
    symlink_metadata = os.stat_result((stat.S_IFLNK | 0o777, 0, 0, 1, 0, 0, 0, 0, 0, 0))

    def lstat_with_link_metadata(path: Path) -> os.stat_result:
        return symlink_metadata if path == link else original_lstat(path)

    monkeypatch.setattr(Path, "lstat", lstat_with_link_metadata)
    with pytest.raises(NurtureQuoteDraftError):
        load_nurture_quote_draft(link)

    reparse = tmp_path / "quote-reparse.json"
    regular_reparse_metadata = type(
        "ReparseMetadata",
        (),
        {
            "st_mode": stat.S_IFREG | 0o600,
            "st_file_attributes": 0x400,
            "st_dev": 1,
            "st_ino": 2,
        },
    )()

    def lstat_with_reparse_metadata(path: Path) -> os.stat_result:
        if path == reparse:
            return cast(os.stat_result, regular_reparse_metadata)
        return original_lstat(path)

    monkeypatch.setattr(Path, "lstat", lstat_with_reparse_metadata)
    with pytest.raises(NurtureQuoteDraftError):
        load_nurture_quote_draft(reparse)


def _seed_quote_target_state(
    store: NurtureStore,
    account: LocalAccount,
    preset: NurturePresetV1,
    fingerprint: str,
    action_state: Literal["NONE", "PENDING", "AMBIGUOUS", "CONFIRMED"],
) -> None:
    with store.acquire_account_lock(account.id) as owner:
        scope = owner.start_run(preset, now=_NOW)
        if action_state == "NONE":
            owner.observe_target(preset, fingerprint, scope.receipt.id, "OBSERVE_ONLY", now=_NOW)
        else:
            owner.reserve_target(preset, fingerprint, scope.receipt.id, "PREVIOUS_ACTION", now=_NOW)
            if action_state != "PENDING":
                owner.complete_target_action(
                    preset,
                    fingerprint,
                    scope.receipt.id,
                    action_state,
                    uuid4(),
                    now=_NOW,
                )
        scope.finish("SUCCESS", now=_NOW)


@pytest.mark.asyncio
async def test_quote_apply_reserves_exact_target_before_one_mutation_and_confirms(
    tmp_path: Path,
) -> None:
    root, store, account, preset = _setup(tmp_path)
    fingerprint = fingerprint_remote_thread(_THREAD_ID)
    _seed_quote_target_state(store, account, preset, fingerprint, "NONE")
    api = _Api()
    mutations = _Mutations()

    def assert_pending() -> None:
        target = _target_state_json(root, account, preset)["targets"][0]
        assert target["fingerprint"] == fingerprint
        assert target["action_state"] == "PENDING"
        assert target["last_operation_id"] is None

    mutations.on_create = assert_pending
    result = await _run_quote_apply(
        api,
        store,
        account,
        preset,
        NurtureQuoteDraft(fingerprint, _QUOTE_TEXT),
        mutations,
    )

    assert mutations.quote_calls == [("alice", _THREAD_ID, _QUOTE_TEXT)]
    assert mutations.calls == []
    assert result.receipt.outcome == "SUCCESS"
    assert result.receipt.selected_count == 1
    assert result.receipt.published_count == 1
    assert result.receipt.replied_count == 0
    assert result.receipt.skipped_count == result.receipt.deduped_count - 1
    assert result.receipt.operation_ids == (mutations.operation_id,)
    assert result.receipt.decision_codes == ("QUOTE_PUBLISHED",)
    assert result.target_fingerprint == fingerprint
    target = _target_state_json(root, account, preset)["targets"][0]
    assert target["action_state"] == "CONFIRMED"
    assert target["last_operation_id"] == str(mutations.operation_id)
    persisted = _all_nurture_json(root)
    for secret in (_THREAD_ID, _QUOTE_TEXT, _QUOTE_MEDIA_ID, _TOKEN, _CREDENTIAL_REF):
        assert secret not in persisted


@pytest.mark.asyncio
async def test_quote_binding_fails_closed_without_silent_retarget(tmp_path: Path) -> None:
    root, store, account, preset = _setup(tmp_path)
    api = _Api(threads=(_thread("CURRENT_THREAD_ID_SENTINEL"),))
    mutations = _Mutations()
    old_fingerprint = fingerprint_remote_thread("OLD_THREAD_ID_SENTINEL")

    with pytest.raises(NurtureRunnerError) as caught:
        await _run_quote_apply(
            api,
            store,
            account,
            preset,
            NurtureQuoteDraft(old_fingerprint, _QUOTE_TEXT),
            mutations,
        )

    assert caught.value.code == "NURTURE_QUOTE_TARGET_MISMATCH"
    assert mutations.quote_calls == []
    state_path = root / "nurture" / "state" / str(account.id) / f"{preset.id}.json"
    assert not state_path.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("bind_to_inbound", [False, True])
async def test_actionable_inbound_outranks_quote_and_is_never_quote_eligible(
    tmp_path: Path,
    bind_to_inbound: bool,
) -> None:
    preset = _preset(owner="configured_owner")
    _, store, account, preset = _setup(tmp_path, preset=preset)
    api = _Api(
        owned_threads=(_thread(_ROOT_ID),),
        conversations={
            _ROOT_ID: ReplyPage(
                (
                    RemoteReply(
                        reply_id=_INBOUND_REPLY_ID,
                        text="inbound body sentinel",
                        timestamp=_NOW.isoformat(),
                        root_post_id=_ROOT_ID,
                        replied_to_id=_ROOT_ID,
                        is_reply_owned_by_me=False,
                    ),
                ),
                "ignored cursor",
                True,
            )
        },
    )
    mutations = _Mutations()
    fingerprint = (
        fingerprint_remote_reply(_INBOUND_REPLY_ID)
        if bind_to_inbound
        else fingerprint_remote_thread(_THREAD_ID)
    )

    with pytest.raises(NurtureRunnerError) as caught:
        await _run_quote_apply(
            api,
            store,
            account,
            preset,
            NurtureQuoteDraft(fingerprint, _QUOTE_TEXT),
            mutations,
        )

    assert caught.value.code == "NURTURE_QUOTE_TARGET_MISMATCH"
    assert mutations.quote_calls == []
    assert not any(call[0] == "mentions" for call in api.calls)
    assert not any(call[0] == "search" for call in api.calls)


@pytest.mark.asyncio
@pytest.mark.parametrize("action_state", ["PENDING", "AMBIGUOUS", "CONFIRMED"])
async def test_quote_rejects_targets_with_existing_action_state(
    tmp_path: Path,
    action_state: Literal["PENDING", "AMBIGUOUS", "CONFIRMED"],
) -> None:
    root, store, account, preset = _setup(tmp_path)
    fingerprint = fingerprint_remote_thread(_THREAD_ID)
    _seed_quote_target_state(store, account, preset, fingerprint, action_state)
    api = _Api()
    mutations = _Mutations()

    with pytest.raises(NurtureRunnerError) as caught:
        await _run_quote_apply(
            api,
            store,
            account,
            preset,
            NurtureQuoteDraft(fingerprint, _QUOTE_TEXT),
            mutations,
        )

    assert caught.value.code == "NURTURE_QUOTE_TARGET_MISMATCH"
    assert api.calls == []
    assert mutations.quote_calls == []
    assert _target_state_json(root, account, preset)["targets"][0]["action_state"] == action_state


@pytest.mark.asyncio
async def test_quote_quota_exhaustion_precedes_target_reservation(tmp_path: Path) -> None:
    root, store, account, preset = _setup(tmp_path)
    fingerprint = fingerprint_remote_thread(_THREAD_ID)
    _seed_quote_target_state(store, account, preset, fingerprint, "NONE")
    api = _Api(quota=PublishingQuota(usage=100, total=100))
    mutations = _Mutations()

    with pytest.raises(NurtureRunnerError) as caught:
        await _run_quote_apply(
            api,
            store,
            account,
            preset,
            NurtureQuoteDraft(fingerprint, _QUOTE_TEXT),
            mutations,
        )

    assert caught.value.code == "THREADS_PUBLISHING_QUOTA_REACHED"
    assert mutations.quote_calls == []
    assert _target_state_json(root, account, preset)["targets"][0]["action_state"] == "NONE"


@pytest.mark.asyncio
async def test_quote_ambiguity_is_terminal_and_never_uses_a_second_target(
    tmp_path: Path,
) -> None:
    root, store, account, preset = _setup(tmp_path)
    operation_id = uuid4()
    mutations = _Mutations(
        failure=StandaloneMutationError("PUBLISH_OUTCOME_AMBIGUOUS", operation_id)
    )
    fingerprint = fingerprint_remote_thread(_THREAD_ID)

    with pytest.raises(NurtureRunnerError) as caught:
        await _run_quote_apply(
            _Api(
                threads=(
                    _thread(_THREAD_ID),
                    RemoteDiscoveryThread(
                        "SECOND_THREAD_ID_SENTINEL",
                        None,
                        None,
                        "career",
                        None,
                        None,
                        _NOW,
                        None,
                        None,
                    ),
                )
            ),
            store,
            account,
            preset,
            NurtureQuoteDraft(fingerprint, _QUOTE_TEXT),
            mutations,
        )

    assert caught.value.code == "PUBLISH_OUTCOME_AMBIGUOUS"
    assert caught.value.operation_id == operation_id
    assert len(mutations.quote_calls) == 1
    assert mutations.calls == []
    assert _target_state_json(root, account, preset)["targets"][0]["action_state"] == "AMBIGUOUS"
    assert _target_state_json(root, account, preset)["targets"][0]["last_operation_id"] == str(
        operation_id
    )
    assert caught.value.run_id is not None
    with store.acquire_account_lock(account.id) as owner:
        receipt = owner.get_run(caught.value.run_id)
    assert receipt.outcome == "AMBIGUOUS"
    assert receipt.operation_ids == (operation_id,)


@pytest.mark.asyncio
async def test_quote_deterministic_failure_keeps_pending_and_finalization_never_retries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, store, account, preset = _setup(tmp_path)
    fingerprint = fingerprint_remote_thread(_THREAD_ID)
    mutations = _Mutations(failure=StandaloneMutationError("THREADS_API_REJECTED_REQUEST"))

    with pytest.raises(NurtureRunnerError) as caught:
        await _run_quote_apply(
            _Api(),
            store,
            account,
            preset,
            NurtureQuoteDraft(fingerprint, _QUOTE_TEXT),
            mutations,
        )

    assert caught.value.code == "THREADS_API_REJECTED_REQUEST"
    assert len(mutations.quote_calls) == 1
    assert _target_state_json(root, account, preset)["targets"][0]["action_state"] == "PENDING"

    second_root, second_store, second_account, second_preset = _setup(tmp_path / "second")
    second_mutations = _Mutations()

    def fail_confirm(*_args: object, **_kwargs: object) -> None:
        raise NurtureStateError("NURTURE_STATE_INVALID")

    monkeypatch.setattr(NurtureAccountLock, "complete_target_action", fail_confirm)
    with pytest.raises(NurtureRunnerError):
        await _run_quote_apply(
            _Api(),
            second_store,
            second_account,
            second_preset,
            NurtureQuoteDraft(fingerprint_remote_thread(_THREAD_ID), _QUOTE_TEXT),
            second_mutations,
        )
    assert len(second_mutations.quote_calls) == 1
    assert (
        _target_state_json(second_root, second_account, second_preset)["targets"][0]["action_state"]
        == "PENDING"
    )


@pytest.mark.asyncio
async def test_confirmed_quote_target_is_suppressed_by_normal_dedupe(tmp_path: Path) -> None:
    root, store, account, preset = _setup(tmp_path)
    fingerprint = fingerprint_remote_thread(_THREAD_ID)
    _seed_quote_target_state(store, account, preset, fingerprint, "CONFIRMED")
    result = await NurtureRunner(cast(LocalThreadsApiRuntime, _Api()), store).run_with_selection(
        account,
        preset,
        now=_NOW,
    )

    assert result.receipt.outcome == "SUCCESS"
    assert result.receipt.selected_count == 0
    assert result.receipt.decision_codes == ("NO_ACTION",)
    assert _target_state_json(root, account, preset)["targets"][0]["action_state"] == "CONFIRMED"


def test_quote_cli_rejects_missing_apply_and_conflicting_files_before_root_or_app(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def forbidden(*_args: object, **_kwargs: object) -> object:
        pytest.fail("quote input preflight must happen before data root or app composition")

    monkeypatch.setattr(cli_module, "resolve_standalone_data_root", forbidden)
    monkeypatch.setattr(cli_module, "build_standalone_app", forbidden)

    assert (
        cli_module.main(
            ["nurture", "run", "alice", "--preset", "recruitment", "--quote-file", "q.json"]
        )
        == 1
    )
    assert capsys.readouterr().err == "ERROR INVALID_NURTURE_QUOTE_DRAFT\n"

    conflicts = (
        ("--reply-file", "r.json", "--post-file", "p.json"),
        ("--reply-file", "r.json", "--quote-file", "q.json"),
        ("--post-file", "p.json", "--quote-file", "q.json"),
        ("--reply-file", "r.json", "--post-file", "p.json", "--quote-file", "q.json"),
    )
    for conflict in conflicts:
        args = ["nurture", "run", "alice", "--preset", "recruitment", "--apply", *conflict]
        assert cli_module.main(args) == 1
        assert capsys.readouterr().err == "ERROR NURTURE_INPUT_CONFLICT\n"
    assert not (tmp_path / "standalone" / "nurture").exists()


def test_invalid_quote_file_fails_before_app_composition_or_run_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "standalone"
    monkeypatch.setenv("THREADS_LOCAL_DATA_ROOT", str(root))
    LocalAccountStore(root).add("alice")
    invalid_file = tmp_path / "invalid-quote.json"
    invalid_file.write_text("{}", encoding="utf-8")

    def forbidden_builder(*_args: object, **_kwargs: object) -> object:
        pytest.fail("invalid quote packet must fail before app composition")

    monkeypatch.setattr(cli_module, "build_standalone_app", forbidden_builder)
    assert (
        cli_module.main(
            [
                "nurture",
                "run",
                "alice",
                "--preset",
                "recruitment",
                "--apply",
                "--quote-file",
                str(invalid_file),
            ]
        )
        == 1
    )
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err == "ERROR INVALID_NURTURE_QUOTE_DRAFT\n"
    assert not (root / "nurture").exists()


def test_quote_policy_is_only_enabled_for_exact_engaged_recruitment_v1() -> None:
    preset = get_nurture_preset("recruitment")
    assert nurture_quote_allowed(preset)
    assert not nurture_quote_allowed(replace(preset, engagement_enabled=False))
    assert not nurture_quote_allowed(
        type(
            "LookalikePreset", (), {"id": "recruitment", "version": 1, "engagement_enabled": True}
        )()
    )


def test_quote_cli_composes_api_and_mutations_without_browser_and_outputs_only_fingerprint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "standalone"
    monkeypatch.setenv("THREADS_LOCAL_DATA_ROOT", str(root))
    accounts = LocalAccountStore(root)
    accounts.add("alice")
    fingerprint = fingerprint_remote_thread(_THREAD_ID)
    draft_path = tmp_path / "quote-file.json"
    draft_path.write_text(
        json.dumps(
            {
                "version": 1,
                "action": "QUOTE",
                "target_fingerprint": fingerprint,
                "text": _QUOTE_TEXT,
            }
        ),
        encoding="utf-8",
    )
    api = _Api()
    mutations = _Mutations()
    flags: list[tuple[bool, bool, bool]] = []

    @asynccontextmanager
    async def fake_builder(
        _root: Path,
        local_accounts: LocalAccountStore,
        *,
        include_api: bool = True,
        include_mutations: bool = True,
        include_browser: bool = True,
    ) -> AsyncGenerator[StandaloneAppContext]:
        flags.append((include_api, include_mutations, include_browser))
        yield StandaloneAppContext(
            local_accounts,
            cast(LocalThreadsApiRuntime, api),
            cast(LocalThreadsMutationRuntime, mutations) if include_mutations else None,
            None,
        )

    monkeypatch.setattr(cli_module, "build_standalone_app", fake_builder)

    assert (
        cli_module.main(
            [
                "nurture",
                "run",
                "alice",
                "--preset",
                "recruitment",
                "--apply",
                "--quote-file",
                str(draft_path),
            ]
        )
        == 0
    )

    output = capsys.readouterr()
    assert flags == [(True, True, False)]
    assert mutations.quote_calls == [("alice", _THREAD_ID, _QUOTE_TEXT)]
    assert f"target={fingerprint}" in output.out
    assert "decision=QUOTE_PUBLISHED" in output.out
    for secret in (
        str(draft_path),
        _THREAD_ID,
        _REMOTE_TEXT,
        _QUOTE_TEXT,
        _QUOTE_MEDIA_ID,
        _TOKEN,
        _CREDENTIAL_REF,
    ):
        assert secret not in output.out + output.err
    persisted = _all_nurture_json(root)
    for secret in (_THREAD_ID, _QUOTE_TEXT, _QUOTE_MEDIA_ID, _TOKEN, _CREDENTIAL_REF):
        assert secret not in persisted
