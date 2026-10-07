from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
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
from threads_platform.standalone.mutations import (
    LocalThreadsMutationRuntime,
    PublishedTextResult,
    StandaloneMutationError,
)
from threads_platform.standalone.nurture import NurturePresetV1, get_nurture_preset
from threads_platform.standalone.nurture_content import ContentCandidateV1, ContentCategory
from threads_platform.standalone.nurture_runner import (
    NurtureRunner,
    NurtureRunnerError,
    NurtureRunResult,
)
from threads_platform.standalone.nurture_store import (
    NurtureAccountLock,
    NurtureStateError,
    NurtureStore,
)

_NOW = datetime(2026, 10, 7, 4, 0, tzinfo=UTC)
_TEXT_SENTINEL = "OWN_CONTENT_TEXT_SENTINEL"
_SOURCE_SENTINEL = "OWN_CONTENT_SOURCE_SENTINEL"
_TOKEN_SENTINEL = "OWN_CONTENT_TOKEN_SENTINEL"
_MEDIA_SENTINEL = "OWN_CONTENT_REMOTE_MEDIA_SENTINEL"
_REMOTE_THREAD = "REMOTE_THREAD_ID_SENTINEL"


def _preset(**changes: object) -> NurturePresetV1:
    values: dict[str, object] = {
        "keyword_queries": ("career query",),
        "tag_queries": (),
        "include_terms": ("career",),
        "exclude_terms": (),
        "watched_public_usernames": (),
        "max_total_discovery_candidates": 8,
        "max_selected_candidates": 3,
        "max_browser_enrichments": 3,
        "owner_public_username": None,
        "max_owned_threads_inspected_per_explicit_run": 0,
    }
    values.update(changes)
    return replace(get_nurture_preset("recruitment"), **cast(dict[str, Any], values))


def _candidate(
    *,
    source_id: str = _SOURCE_SENTINEL,
    text: str = _TEXT_SENTINEL,
    category: str = "CAREER_TIP",
    candidate_id: UUID | None = None,
) -> ContentCandidateV1:
    return ContentCandidateV1(
        candidate_id=candidate_id or uuid4(),
        account_alias="alice",
        preset_id="recruitment",
        source_id=source_id,
        category=cast(ContentCategory, category),
        text=text,
    )


def _thread(remote_id: str = _REMOTE_THREAD) -> RemoteDiscoveryThread:
    return RemoteDiscoveryThread(
        remote_id,
        "public_username_sentinel",
        None,
        "Career question?",
        None,
        None,
        _NOW,
        None,
        False,
    )


class _Api:
    def __init__(
        self,
        *,
        mentions: tuple[RemoteDiscoveryThread, ...] = (),
        quota: PublishingQuota | None = None,
        fail_mentions: bool = False,
        owned_threads: tuple[RemoteDiscoveryThread, ...] = (),
        conversations: dict[str, ReplyPage] | None = None,
    ) -> None:
        self.mention_threads = mentions
        self.quota_value = quota or PublishingQuota(usage=1, total=100)
        self.fail_mentions = fail_mentions
        self.owned_threads = owned_threads
        self.conversations = conversations or {}
        self.calls: list[tuple[object, ...]] = []

    async def profile_posts(
        self,
        alias: str,
        username: str,
        *,
        after: str | None,
        limit: int,
    ) -> DiscoveryPage:
        self.calls.append(("profile_posts", alias, username, after, limit))
        return DiscoveryPage(self.owned_threads, "ignored-profile-cursor", True)

    async def conversation(self, alias: str, thread_id: str, *, after: str | None) -> ReplyPage:
        self.calls.append(("conversation", alias, thread_id, after))
        return self.conversations.get(thread_id, ReplyPage((), "ignored-reply-cursor", True))

    async def mentions(self, alias: str, *, after: str | None, limit: int) -> DiscoveryPage:
        self.calls.append(("mentions", alias, after, limit))
        if self.fail_mentions:
            raise RuntimeError(_TOKEN_SENTINEL)
        return DiscoveryPage(self.mention_threads, "ignored-cursor", True)

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
        return DiscoveryPage((), "ignored-cursor", True)

    async def quota(self, alias: str) -> PublishingQuota:
        self.calls.append(("quota", alias))
        return self.quota_value


class _Mutations:
    def __init__(
        self,
        *,
        operation_id: UUID | None = None,
        failure: BaseException | None = None,
        on_publish: Callable[[], None] | None = None,
    ) -> None:
        self.operation_id = operation_id or uuid4()
        self.failure = failure
        self.on_publish = on_publish
        self.calls: list[tuple[str, str]] = []

    async def publish_text(self, alias: str, text: str) -> PublishedTextResult:
        self.calls.append((alias, text))
        if self.on_publish is not None:
            self.on_publish()
        if self.failure is not None:
            raise self.failure
        return PublishedTextResult(self.operation_id, _MEDIA_SENTINEL)


def _setup(
    tmp_path: Path, preset: NurturePresetV1 | None = None
) -> tuple[Path, NurtureStore, LocalAccount, NurturePresetV1]:
    root = tmp_path / "standalone"
    account = LocalAccountStore(root).add("alice")
    return root, NurtureStore(root), account, preset or _preset()


async def _run(
    api: _Api,
    store: NurtureStore,
    account: LocalAccount,
    preset: NurturePresetV1,
    candidate: ContentCandidateV1,
    *,
    apply: bool = False,
    mutations: _Mutations | None = None,
    now: datetime = _NOW,
) -> NurtureRunResult:
    return await NurtureRunner(cast(LocalThreadsApiRuntime, api), store).run_with_selection(
        account,
        preset,
        now=now,
        apply_requested=apply,
        content_candidate=candidate,
        mutations=cast(LocalThreadsMutationRuntime, mutations) if apply else None,
    )


def _content_path(root: Path, account: LocalAccount, preset: NurturePresetV1) -> Path:
    return root / "nurture" / "content" / str(account.id) / f"{preset.id}.json"


def _nurture_documents(root: Path) -> str:
    return "\n".join(
        path.read_text(encoding="utf-8") for path in root.joinpath("nurture").rglob("*.json")
    )


def _seed_published(
    store: NurtureStore,
    account: LocalAccount,
    preset: NurturePresetV1,
    candidate: ContentCandidateV1,
    *,
    action_at: datetime,
) -> None:
    with store.acquire_account_lock(account.id) as owner:
        scope = owner.start_run(preset, now=action_at)
        owner.register_content_candidate(
            preset,
            candidate.candidate_id,
            candidate.source_fingerprint,
            candidate.draft_fingerprint,
            candidate.category,
            scope.receipt.id,
            now=action_at,
        )
        owner.reserve_content_candidate(
            preset,
            candidate.source_fingerprint,
            candidate.draft_fingerprint,
            scope.receipt.id,
        )
        owner.complete_content_candidate(
            preset,
            candidate.source_fingerprint,
            candidate.draft_fingerprint,
            scope.receipt.id,
            "PUBLISHED",
            uuid4(),
            now=action_at,
        )
        scope.finish("SUCCESS", now=action_at)


@pytest.mark.parametrize("field", ["account_alias", "preset_id"])
async def test_content_packet_binding_is_rechecked_before_run_creation(
    tmp_path: Path,
    field: str,
) -> None:
    root, store, account, preset = _setup(tmp_path)
    api = _Api()
    candidate = replace(_candidate(), **{field: "different"})

    with pytest.raises(NurtureRunnerError) as error:
        await _run(api, store, account, preset, candidate)

    assert error.value.code == "NURTURE_CONTENT_ACCOUNT_MISMATCH"
    assert api.calls == []
    assert not (root / "nurture").exists()


@pytest.mark.asyncio
async def test_observe_mode_recommends_content_without_constructed_mutation_runtime(
    tmp_path: Path,
) -> None:
    root, store, account, preset = _setup(tmp_path)
    candidate = _candidate()
    api = _Api()

    result = await _run(api, store, account, preset, candidate)

    assert result.receipt.outcome == "SUCCESS"
    assert result.receipt.decision_codes == ("CONTENT_RECOMMENDED_NO_APPLY",)
    assert result.receipt.selected_count == 1
    assert result.receipt.published_count == 0
    assert result.receipt.replied_count == 0
    assert result.receipt.skipped_count == 0
    assert result.content_candidate_id == candidate.candidate_id
    state = json.loads(_content_path(root, account, preset).read_text(encoding="utf-8"))
    assert state["candidates"][0]["publication_state"] == "NEW"
    assert not any(call[0] == "quota" for call in api.calls)


@pytest.mark.parametrize(
    ("cooldown", "interval", "elapsed", "due"),
    [(120, 30, 100, False), (30, 120, 100, False), (120, 30, 120, True)],
)
async def test_due_policy_uses_the_stricter_preset_interval(
    tmp_path: Path,
    cooldown: int,
    interval: int,
    elapsed: int,
    due: bool,
) -> None:
    preset = _preset(
        own_content_cooldown_seconds=cooldown,
        own_content_due_interval_seconds=interval,
    )
    _, store, account, preset = _setup(tmp_path, preset)
    previous = _candidate(source_id="previous-source", text="previous draft")
    _seed_published(store, account, preset, previous, action_at=_NOW - timedelta(seconds=elapsed))

    result = await _run(_Api(), store, account, preset, _candidate())

    assert result.receipt.outcome == "SUCCESS"
    if due:
        assert result.receipt.decision_codes == ("CONTENT_RECOMMENDED_NO_APPLY",)
        assert result.receipt.selected_count == 1
    else:
        assert result.receipt.decision_codes == ("CONTENT_NOT_DUE",)
        assert result.receipt.selected_count == 0
    assert (
        result.receipt.skipped_count == result.receipt.deduped_count - result.receipt.selected_count
    )


@pytest.mark.asyncio
async def test_conversation_candidate_wins_without_registering_or_publishing_content(
    tmp_path: Path,
) -> None:
    root, store, account, preset = _setup(tmp_path)
    api = _Api(mentions=(_thread(),))
    mutations = _Mutations()

    result = await _run(api, store, account, preset, _candidate(), apply=True, mutations=mutations)

    assert result.receipt.outcome == "SUCCESS"
    assert result.receipt.decision_codes == ("OBSERVE_ONLY",)
    assert result.receipt.selected_count == 1
    assert result.target_fingerprint is not None
    assert mutations.calls == []
    assert not _content_path(root, account, preset).exists()
    target_path = root / "nurture" / "state" / str(account.id) / f"{preset.id}.json"
    target = json.loads(target_path.read_text(encoding="utf-8"))["targets"][0]
    assert target["action_state"] == "NONE"


@pytest.mark.asyncio
async def test_proven_inbound_reply_outranks_content_and_short_circuits_discovery(
    tmp_path: Path,
) -> None:
    preset = _preset(
        owner_public_username="configured_owner",
        max_owned_threads_inspected_per_explicit_run=1,
    )
    root, store, account, preset = _setup(tmp_path, preset)
    root_id = "OWNED_ROOT_THREAD_SENTINEL"
    reply_id = "PROVEN_INBOUND_REPLY_SENTINEL"
    api = _Api(
        owned_threads=(_thread(root_id),),
        conversations={
            root_id: ReplyPage(
                (
                    RemoteReply(
                        reply_id, "inbound text sentinel", _NOW.isoformat(), root_id, root_id, False
                    ),
                ),
                "ignored-cursor",
                True,
            )
        },
    )
    mutations = _Mutations()

    result = await _run(api, store, account, preset, _candidate(), apply=True, mutations=mutations)

    assert result.receipt.outcome == "SUCCESS"
    assert result.receipt.decision_codes == ("INBOUND_CANDIDATE",)
    assert api.calls == [
        ("profile_posts", "alice", "configured_owner", None, 1),
        ("conversation", "alice", root_id, None),
    ]
    assert result.target_fingerprint is not None
    assert mutations.calls == []
    assert not _content_path(root, account, preset).exists()


@pytest.mark.asyncio
async def test_discovery_failure_fails_run_without_content_fallback(tmp_path: Path) -> None:
    root, store, account, preset = _setup(tmp_path)
    mutations = _Mutations()

    with pytest.raises(NurtureRunnerError) as error:
        await _run(
            _Api(fail_mentions=True),
            store,
            account,
            preset,
            _candidate(),
            apply=True,
            mutations=mutations,
        )

    assert error.value.code == "DISCOVERY_SOURCE_FAILED"
    assert error.value.run_id is not None
    assert mutations.calls == []
    assert not (root / "nurture" / "content").exists()
    with store.acquire_account_lock(account.id) as owner:
        assert owner.get_run(error.value.run_id).outcome == "FAILED"


@pytest.mark.asyncio
async def test_not_due_content_is_successful_and_not_published(tmp_path: Path) -> None:
    preset = _preset(own_content_cooldown_seconds=100, own_content_due_interval_seconds=200)
    _, store, account, preset = _setup(tmp_path, preset)
    _seed_published(
        store,
        account,
        preset,
        _candidate(source_id="prior", text="prior content"),
        action_at=_NOW - timedelta(seconds=150),
    )
    mutations = _Mutations()

    result = await _run(
        _Api(), store, account, preset, _candidate(), apply=True, mutations=mutations
    )

    assert result.receipt.outcome == "SUCCESS"
    assert result.receipt.decision_codes == ("CONTENT_NOT_DUE",)
    assert result.receipt.selected_count == 0
    assert result.receipt.published_count == 0
    assert mutations.calls == []


@pytest.mark.asyncio
async def test_known_publish_quota_failure_precedes_reserved_state(tmp_path: Path) -> None:
    root, store, account, preset = _setup(tmp_path)
    candidate = _candidate()
    api = _Api(quota=PublishingQuota(usage=100, total=100))
    mutations = _Mutations()

    with pytest.raises(NurtureRunnerError) as error:
        await _run(api, store, account, preset, candidate, apply=True, mutations=mutations)

    assert error.value.code == "THREADS_PUBLISHING_QUOTA_REACHED"
    assert mutations.calls == []
    record = json.loads(_content_path(root, account, preset).read_text(encoding="utf-8"))[
        "candidates"
    ][0]
    assert record["publication_state"] == "NEW"


@pytest.mark.parametrize(
    "preset_changes",
    [{"engagement_enabled": False}, {"max_own_post_mutations_per_explicit_run": 0}],
)
async def test_disabled_content_apply_fails_before_quota_and_reservation(
    tmp_path: Path,
    preset_changes: dict[str, object],
) -> None:
    preset = _preset(**preset_changes)
    root, store, account, preset = _setup(tmp_path, preset)
    api = _Api()
    mutations = _Mutations()

    with pytest.raises(NurtureRunnerError) as error:
        await _run(api, store, account, preset, _candidate(), apply=True, mutations=mutations)

    assert error.value.code == "NURTURE_CONTENT_APPLY_DISABLED"
    assert not any(call[0] == "quota" for call in api.calls)
    assert mutations.calls == []
    record = json.loads(_content_path(root, account, preset).read_text(encoding="utf-8"))[
        "candidates"
    ][0]
    assert record["publication_state"] == "NEW"


@pytest.mark.asyncio
async def test_reserved_is_durable_before_single_publish_and_success_links_operation(
    tmp_path: Path,
) -> None:
    root, store, account, preset = _setup(tmp_path)
    candidate = _candidate()
    api = _Api(quota=PublishingQuota(usage=None, total=100))
    mutations = _Mutations()

    def assert_reserved_before_publish() -> None:
        state = json.loads(_content_path(root, account, preset).read_text(encoding="utf-8"))
        record = state["candidates"][0]
        assert record["publication_state"] == "RESERVED"
        active = list((root / "nurture" / "runs" / str(account.id) / "active").glob("*.json"))
        assert len(active) == 1
        assert json.loads(active[0].read_text(encoding="utf-8"))["outcome"] == "RUNNING"

    mutations.on_publish = assert_reserved_before_publish
    result = await _run(api, store, account, preset, candidate, apply=True, mutations=mutations)

    assert len(mutations.calls) == 1
    assert mutations.calls[0] == ("alice", _TEXT_SENTINEL)
    assert result.receipt.outcome == "SUCCESS"
    assert result.receipt.decision_codes == ("CONTENT_PUBLISHED",)
    assert result.receipt.selected_count == 1
    assert result.receipt.published_count == 1
    assert result.receipt.replied_count == 0
    assert result.receipt.selected_count in {0, 1}
    assert result.receipt.replied_count + result.receipt.published_count <= 1
    assert (
        result.receipt.skipped_count == result.receipt.deduped_count - result.receipt.selected_count
    )
    assert result.receipt.operation_ids == (mutations.operation_id,)
    record = json.loads(_content_path(root, account, preset).read_text(encoding="utf-8"))[
        "candidates"
    ][0]
    assert record["publication_state"] == "PUBLISHED"
    assert record["operation_id"] == str(mutations.operation_id)
    assert record["last_action_at"] == "2026-10-07T04:00:00.000000Z"
    persisted = _nurture_documents(root)
    for secret in (_SOURCE_SENTINEL, _TEXT_SENTINEL, _TOKEN_SENTINEL, _MEDIA_SENTINEL):
        assert secret not in persisted
        assert secret not in repr(result)


@pytest.mark.asyncio
async def test_published_state_write_failure_never_repeats_publish(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, store, account, preset = _setup(tmp_path)
    candidate = _candidate()
    mutations = _Mutations()
    original = NurtureAccountLock.complete_content_candidate

    def fail_confirmation(
        self: NurtureAccountLock,
        preset_value: NurturePresetV1,
        source: str,
        draft: str,
        run_id: UUID,
        publication_state: object,
        operation_id: UUID,
        *,
        now: datetime,
    ) -> object:
        if publication_state == "PUBLISHED":
            raise NurtureStateError("NURTURE_STATE_INVALID")
        return original(
            self,
            preset_value,
            source,
            draft,
            run_id,
            cast(Literal["PUBLISHED", "AMBIGUOUS"], publication_state),
            operation_id,
            now=now,
        )

    monkeypatch.setattr(NurtureAccountLock, "complete_content_candidate", fail_confirmation)
    with pytest.raises(NurtureRunnerError) as error:
        await _run(_Api(), store, account, preset, candidate, apply=True, mutations=mutations)

    assert error.value.code == "NURTURE_STATE_INVALID"
    assert len(mutations.calls) == 1
    record = json.loads(_content_path(root, account, preset).read_text(encoding="utf-8"))[
        "candidates"
    ][0]
    assert record["publication_state"] == "RESERVED"
    assert record["operation_id"] is None


@pytest.mark.asyncio
async def test_exact_publish_ambiguity_links_operation_and_never_retries(tmp_path: Path) -> None:
    root, store, account, preset = _setup(tmp_path)
    candidate = _candidate()
    operation_id = uuid4()
    mutations = _Mutations(
        operation_id=operation_id,
        failure=StandaloneMutationError("PUBLISH_OUTCOME_AMBIGUOUS", operation_id),
    )
    with pytest.raises(NurtureRunnerError) as error:
        await _run(_Api(), store, account, preset, candidate, apply=True, mutations=mutations)

    assert error.value.code == "PUBLISH_OUTCOME_AMBIGUOUS"
    assert error.value.operation_id == operation_id
    assert error.value.run_id is not None
    assert len(mutations.calls) == 1
    record = json.loads(_content_path(root, account, preset).read_text(encoding="utf-8"))[
        "candidates"
    ][0]
    assert record["publication_state"] == "AMBIGUOUS"
    assert record["operation_id"] == str(operation_id)
    with store.acquire_account_lock(account.id) as owner:
        run = owner.get_run(error.value.run_id)
    assert run.outcome == "AMBIGUOUS"
    assert run.published_count == 0
    assert run.operation_ids == (operation_id,)

    with pytest.raises(NurtureRunnerError) as retry_error:
        await _run(_Api(), store, account, preset, candidate, apply=True, mutations=mutations)
    assert retry_error.value.code == "NURTURE_CONTENT_UNRESOLVED"
    assert len(mutations.calls) == 1


@pytest.mark.asyncio
async def test_cancelled_publish_keeps_reserved_and_interrupted_receipt(tmp_path: Path) -> None:
    root, store, account, preset = _setup(tmp_path)
    mutations = _Mutations(failure=asyncio.CancelledError())

    with pytest.raises(asyncio.CancelledError):
        await _run(_Api(), store, account, preset, _candidate(), apply=True, mutations=mutations)

    assert len(mutations.calls) == 1
    record = json.loads(_content_path(root, account, preset).read_text(encoding="utf-8"))[
        "candidates"
    ][0]
    assert record["publication_state"] == "RESERVED"
    completed = list((root / "nurture" / "runs" / str(account.id) / "completed").glob("*.json"))
    assert len(completed) == 1
    assert json.loads(completed[0].read_text(encoding="utf-8"))["outcome"] == "INTERRUPTED"


def test_canonical_cli_parse_mutual_exclusion_and_invalid_packet_preflight(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    parsed = (
        cast(Any, cli_module)
        ._build_parser()
        .parse_args(
            [
                "nurture",
                "run",
                "alice",
                "--preset",
                "recruitment",
                "--apply",
                "--post-file",
                "packet.json",
            ]
        )
    )
    assert parsed.post_file == "packet.json"

    def forbidden_root() -> Path:
        pytest.fail("mutually exclusive content and reply inputs must fail before setup")

    monkeypatch.setattr(cli_module, "resolve_standalone_data_root", forbidden_root)
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
                "reply.json",
                "--post-file",
                "post.json",
            ]
        )
        == 1
    )
    assert capsys.readouterr().err == "ERROR NURTURE_INPUT_CONFLICT\n"

    root = tmp_path / "standalone"
    LocalAccountStore(root).add("alice")
    invalid_packet = tmp_path / "invalid-content.json"
    invalid_packet.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(cli_module, "resolve_standalone_data_root", lambda: root)
    monkeypatch.setenv("THREADS_LOCAL_DATA_ROOT", str(root))
    monkeypatch.setattr(cli_module, "build_standalone_app", forbidden_root)
    assert (
        cli_module.main(
            [
                "nurture",
                "run",
                "alice",
                "--preset",
                "recruitment",
                "--post-file",
                str(invalid_packet),
            ]
        )
        == 1
    )
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err == "ERROR INVALID_NURTURE_CONTENT\n"
    assert not (root / "nurture").exists()


@pytest.mark.parametrize(
    ("apply", "expected_mutations", "expected_decision"),
    [
        (False, False, "CONTENT_RECOMMENDED_NO_APPLY"),
        (True, True, "CONTENT_PUBLISHED"),
    ],
)
def test_cli_composes_only_requested_runtime_for_content(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    apply: bool,
    expected_mutations: bool,
    expected_decision: str,
) -> None:
    root = tmp_path / "standalone"
    LocalAccountStore(root).add("alice")
    preset = _preset()
    candidate = _candidate()
    packet_path = tmp_path / "content.json"
    packet_path.write_text(
        json.dumps(
            {
                "version": 1,
                "action": "POST_TEXT",
                "candidate_id": str(candidate.candidate_id),
                "account": "alice",
                "preset": preset.id,
                "source_kind": "OPERATOR_SOURCE_ID",
                "source_id": candidate.source_id,
                "category": candidate.category,
                "text": candidate.text,
            }
        ),
        encoding="utf-8",
    )
    composition: dict[str, object] = {}

    class _App:
        def __init__(self, with_mutations: bool) -> None:
            self.api = cast(LocalThreadsApiRuntime, _Api())
            self.mutations = (
                cast(LocalThreadsMutationRuntime, _Mutations()) if with_mutations else None
            )
            self.browser = None

        async def __aenter__(self) -> _App:
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

    def builder(
        app_root: Path,
        account_store: LocalAccountStore,
        *,
        include_api: bool,
        include_mutations: bool,
        include_browser: bool,
    ) -> _App:
        composition.update(
            root=app_root,
            accounts=account_store,
            include_api=include_api,
            include_mutations=include_mutations,
            include_browser=include_browser,
        )
        return _App(include_mutations)

    monkeypatch.setattr(cli_module, "build_standalone_app", builder)
    monkeypatch.setattr(cli_module, "resolve_standalone_data_root", lambda: root)
    arguments = [
        "nurture",
        "run",
        "alice",
        "--preset",
        "recruitment",
        "--post-file",
        str(packet_path),
    ]
    if apply:
        arguments.append("--apply")
    assert cli_module.main(arguments) == 0
    output = capsys.readouterr()

    assert output.err == ""
    assert f"decision={expected_decision}" in output.out
    assert f"candidate={candidate.candidate_id}" in output.out
    for secret in (_SOURCE_SENTINEL, _TEXT_SENTINEL, _TOKEN_SENTINEL, _MEDIA_SENTINEL):
        assert secret not in output.out
        assert secret not in output.err

    assert composition["include_api"] is True
    assert composition["include_mutations"] is expected_mutations
    assert composition["include_browser"] is False
    completed = list((root / "nurture" / "runs").rglob("*.json"))
    assert len(completed) == 1
    receipt = json.loads(completed[0].read_text(encoding="utf-8"))
    assert receipt["decision_codes"] == [expected_decision]
    assert receipt["selected_count"] == 1
    assert receipt["published_count"] == int(apply)
