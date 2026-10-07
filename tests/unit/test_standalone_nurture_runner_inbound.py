from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast
from uuid import uuid4

import pytest

import threads_platform.standalone.__main__ as cli_module
import threads_platform.standalone.nurture_runner as runner_module
from threads_platform.application.ports.threads import (
    DiscoveryPage,
    RemoteDiscoveryThread,
    RemoteReply,
    ReplyPage,
)
from threads_platform.standalone.accounts import LocalAccount, LocalAccountStore
from threads_platform.standalone.api import LocalThreadsApiRuntime
from threads_platform.standalone.app import StandaloneAppContext
from threads_platform.standalone.nurture import NurturePresetV1, get_nurture_preset
from threads_platform.standalone.nurture_discovery import (
    NurtureCandidate,
    NurtureDiscoveryResult,
    NurtureDiscoverySource,
    NurtureSourceCallCounts,
)
from threads_platform.standalone.nurture_runner import NurtureRunner, NurtureRunnerError
from threads_platform.standalone.nurture_store import (
    NurtureStore,
    NurtureTargetV1,
    fingerprint_remote_reply,
    fingerprint_remote_thread,
)

_NOW = datetime(2026, 10, 7, 1, 30, tzinfo=UTC)
_REPLY_TEXT = "RAW_REPLY_TEXT_SENTINEL"
_THREAD_ID = "OWN_THREAD_ID_SENTINEL"
_REPLY_ID = "REMOTE_REPLY_ID_SENTINEL"


def _preset() -> NurturePresetV1:
    return replace(
        get_nurture_preset("recruitment"),
        keyword_queries=("query_sentinel",),
        tag_queries=(),
        watched_public_usernames=(),
        owner_public_username="configured_owner",
        max_owned_threads_inspected_per_explicit_run=2,
        seen_cooldown_seconds=60,
    )


def _thread(remote_id: str) -> RemoteDiscoveryThread:
    return RemoteDiscoveryThread(remote_id, None, None, None, None, None, None, None, None)


def _candidate(
    remote_id: str,
    source: NurtureDiscoverySource,
    *,
    score: int,
) -> NurtureCandidate:
    return NurtureCandidate(
        remote_thread_id=remote_id,
        source_class=source,
        source_index=0,
        source_order=0,
        username=None,
        text="candidate text sentinel",
        permalink=None,
        timestamp=None,
        has_replies=None,
        is_quote_post=None,
        fingerprint=fingerprint_remote_thread(remote_id),
        include_term_match=False,
        question_signal=False,
        recency_bucket=None,
        score=score,
        reason_codes=(),
    )


class _Api:
    def __init__(
        self,
        *,
        owned_threads: tuple[RemoteDiscoveryThread, ...] = (),
        reply_pages: dict[str, ReplyPage] | None = None,
        discovery_threads: tuple[RemoteDiscoveryThread, ...] = (),
        profile_failure: Exception | None = None,
        conversation_failure: Exception | None = None,
    ) -> None:
        self.owned_threads = owned_threads
        self.reply_pages = reply_pages or {}
        self.discovery_threads = discovery_threads
        self.profile_failure = profile_failure
        self.conversation_failure = conversation_failure
        self.profile_calls: list[tuple[str, str, str | None, int]] = []
        self.conversation_calls: list[tuple[str, str, str | None]] = []
        self.discovery_calls: list[tuple[str, str | None, int, str | None]] = []

    async def profile_posts(
        self, alias: str, username: str, *, after: str | None, limit: int
    ) -> DiscoveryPage:
        self.profile_calls.append((alias, username, after, limit))
        if self.profile_failure is not None:
            raise self.profile_failure
        return DiscoveryPage(self.owned_threads, "ignored-profile-cursor", True)

    async def conversation(self, alias: str, thread_id: str, *, after: str | None) -> ReplyPage:
        self.conversation_calls.append((alias, thread_id, after))
        if self.conversation_failure is not None:
            raise self.conversation_failure
        return self.reply_pages.get(thread_id, ReplyPage((), "ignored-conversation-cursor", True))

    async def mentions(self, alias: str, *, after: str | None, limit: int) -> DiscoveryPage:
        self.discovery_calls.append(("mentions", None, limit, after))
        return DiscoveryPage(self.discovery_threads, "ignored-discovery-cursor", True)

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
        self.discovery_calls.append(("search", query, limit, after))
        return DiscoveryPage((), "ignored-search-cursor", True)


def _setup(tmp_path: Path) -> tuple[NurtureStore, LocalAccount, NurturePresetV1]:
    root = tmp_path / "standalone"
    account = LocalAccountStore(root).add("alice")
    return NurtureStore(root), account, _preset()


def _json_state(root: Path) -> str:
    return "\n".join(
        path.read_text(encoding="utf-8") for path in (root / "nurture").rglob("*.json")
    )


def _seed_seen(
    store: NurtureStore,
    account: LocalAccount,
    preset: NurturePresetV1,
    fingerprint: str,
    now: datetime,
) -> NurtureTargetV1:
    with store.acquire_account_lock(account.id) as owner:
        scope = owner.start_run(preset, now=now)
        target = owner.observe_target(
            preset, fingerprint, scope.receipt.id, "OBSERVE_ONLY", now=now
        )
        scope.finish("SUCCESS")
        return target


@pytest.mark.asyncio
async def test_proven_inbound_short_circuits_discovery_and_persists_only_fingerprint(
    tmp_path: Path,
) -> None:
    store, account, preset = _setup(tmp_path)
    root = tmp_path / "standalone"
    api = _Api(
        owned_threads=(_thread(_THREAD_ID),),
        reply_pages={
            _THREAD_ID: ReplyPage(
                (
                    RemoteReply(
                        _REPLY_ID,
                        _REPLY_TEXT,
                        _NOW.isoformat(),
                        _THREAD_ID,
                        "parent",
                        False,
                    ),
                ),
                "must-not-follow",
                True,
            )
        },
    )

    receipt = await NurtureRunner(cast(LocalThreadsApiRuntime, api), store).run(
        account, preset, now=_NOW
    )

    assert api.profile_calls == [("alice", "configured_owner", None, 2)]
    assert api.conversation_calls == [("alice", _THREAD_ID, None)]
    assert api.discovery_calls == []
    assert receipt.outcome == "SUCCESS"
    assert receipt.discovered_count == 1
    assert receipt.deduped_count == 1
    assert receipt.selected_count == 1
    assert receipt.skipped_count == 0
    assert receipt.decision_codes == ("INBOUND_CANDIDATE",)
    assert receipt.operation_ids == ()
    with store.acquire_account_lock(account.id) as owner:
        targets = owner.get_targets(preset)
    assert len(targets) == 1
    assert targets[0].fingerprint == fingerprint_remote_reply(_REPLY_ID)
    assert targets[0].action_state == "NONE"
    assert targets[0].last_decision_code == "INBOUND_CANDIDATE"
    persisted = _json_state(root)
    for sentinel in (_REPLY_TEXT, _REPLY_ID, _THREAD_ID, "configured_owner", "query_sentinel"):
        assert sentinel not in persisted


def test_inbound_cli_output_omits_remote_reply_data(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "standalone"
    monkeypatch.setenv("THREADS_LOCAL_DATA_ROOT", str(root))
    accounts = LocalAccountStore(root)
    accounts.add("alice")
    preset = _preset()
    api = _Api(
        owned_threads=(_thread(_THREAD_ID),),
        reply_pages={
            _THREAD_ID: ReplyPage(
                (RemoteReply(_REPLY_ID, _REPLY_TEXT, _NOW.isoformat(), _THREAD_ID, None, False),),
                None,
                False,
            )
        },
    )

    @asynccontextmanager
    async def fake_builder(
        _root: Path,
        local_accounts: LocalAccountStore,
        *,
        include_api: bool = True,
        include_mutations: bool = True,
        include_browser: bool = True,
    ) -> AsyncGenerator[StandaloneAppContext]:
        assert include_api is True
        assert include_mutations is False
        assert include_browser is False
        yield StandaloneAppContext(local_accounts, cast(LocalThreadsApiRuntime, api), None, None)

    monkeypatch.setattr(cli_module, "build_standalone_app", fake_builder)

    def fixed_preset(_preset_id: object) -> NurturePresetV1:
        return preset

    monkeypatch.setattr(cli_module, "get_nurture_preset", fixed_preset)

    assert cli_module.main(["nurture", "run", "alice", "--preset", "recruitment"]) == 0

    output = capsys.readouterr()
    assert output.err == ""
    assert "decision=INBOUND_CANDIDATE" in output.out
    for sentinel in (_REPLY_TEXT, _REPLY_ID, _THREAD_ID, "configured_owner", "query_sentinel"):
        assert sentinel not in output.out + output.err


@pytest.mark.asyncio
async def test_recently_observed_inbound_is_suppressed_then_discovery_runs_once(
    tmp_path: Path,
) -> None:
    store, account, preset = _setup(tmp_path)
    api = _Api(
        owned_threads=(_thread(_THREAD_ID),),
        reply_pages={
            _THREAD_ID: ReplyPage(
                (RemoteReply(_REPLY_ID, _REPLY_TEXT, _NOW.isoformat(), _THREAD_ID, None, False),),
                None,
                False,
            )
        },
    )
    runner = NurtureRunner(cast(LocalThreadsApiRuntime, api), store)

    first = await runner.run(account, preset, now=_NOW)
    second = await runner.run(account, preset, now=_NOW + timedelta(seconds=1))

    assert first.decision_codes == ("INBOUND_CANDIDATE",)
    assert second.outcome == "SUCCESS"
    assert second.selected_count == 0
    assert second.decision_codes == ("NO_ACTION",)
    assert second.discovered_count == 1
    assert second.deduped_count == 1
    assert [call[0] for call in api.discovery_calls] == ["mentions", "search"]
    assert len(api.profile_calls) == 2
    assert len(api.conversation_calls) == 2


@pytest.mark.asyncio
async def test_future_seen_reply_is_suppressed_conservatively(tmp_path: Path) -> None:
    store, account, preset = _setup(tmp_path)
    future_seen = _NOW + timedelta(hours=1)
    _seed_seen(
        store,
        account,
        preset,
        fingerprint_remote_reply(_REPLY_ID),
        future_seen,
    )
    api = _Api(
        owned_threads=(_thread(_THREAD_ID),),
        reply_pages={
            _THREAD_ID: ReplyPage(
                (RemoteReply(_REPLY_ID, _REPLY_TEXT, None, _THREAD_ID, None, False),), None, False
            )
        },
    )

    receipt = await NurtureRunner(cast(LocalThreadsApiRuntime, api), store).run(
        account, preset, now=_NOW
    )

    assert receipt.outcome == "SUCCESS"
    assert receipt.selected_count == 0
    assert receipt.decision_codes == ("NO_ACTION",)
    with store.acquire_account_lock(account.id) as owner:
        target = next(
            item
            for item in owner.get_targets(preset)
            if item.fingerprint == fingerprint_remote_reply(_REPLY_ID)
        )
    assert target.last_seen_at == future_seen
    assert target.last_decision_code == "OBSERVE_ONLY"


@pytest.mark.asyncio
async def test_zero_seen_cooldown_allows_same_time_inbound_surface(tmp_path: Path) -> None:
    store, account, preset = _setup(tmp_path)
    preset = replace(preset, seen_cooldown_seconds=0)
    _seed_seen(store, account, preset, fingerprint_remote_reply(_REPLY_ID), _NOW)
    api = _Api(
        owned_threads=(_thread(_THREAD_ID),),
        reply_pages={
            _THREAD_ID: ReplyPage(
                (RemoteReply(_REPLY_ID, _REPLY_TEXT, None, _THREAD_ID, None, False),), None, False
            )
        },
    )

    receipt = await NurtureRunner(cast(LocalThreadsApiRuntime, api), store).run(
        account, preset, now=_NOW
    )

    assert receipt.decision_codes == ("INBOUND_CANDIDATE",)
    assert api.discovery_calls == []


@pytest.mark.asyncio
async def test_inbound_observation_preserves_confirmed_action_history(tmp_path: Path) -> None:
    store, account, preset = _setup(tmp_path)
    action_at = _NOW - timedelta(seconds=120)
    operation_id = uuid4()
    fingerprint = fingerprint_remote_reply(_REPLY_ID)
    with store.acquire_account_lock(account.id) as owner:
        scope = owner.start_run(preset, now=action_at)
        owner.reserve_target(
            preset,
            fingerprint,
            scope.receipt.id,
            "OBSERVE_ONLY",
            now=action_at,
        )
        previous = owner.complete_target_action(
            preset,
            fingerprint,
            scope.receipt.id,
            "CONFIRMED",
            operation_id,
            now=action_at,
        )
        scope.finish("SUCCESS")
    api = _Api(
        owned_threads=(_thread(_THREAD_ID),),
        reply_pages={
            _THREAD_ID: ReplyPage(
                (RemoteReply(_REPLY_ID, _REPLY_TEXT, None, _THREAD_ID, None, False),), None, False
            )
        },
    )

    receipt = await NurtureRunner(cast(LocalThreadsApiRuntime, api), store).run(
        account, preset, now=_NOW
    )

    assert receipt.decision_codes == ("INBOUND_CANDIDATE",)
    with store.acquire_account_lock(account.id) as owner:
        observed = next(
            item for item in owner.get_targets(preset) if item.fingerprint == fingerprint
        )
    assert observed.action_state == "CONFIRMED"
    assert observed.last_action_at == previous.last_action_at == action_at
    assert observed.last_operation_id == previous.last_operation_id == operation_id
    assert observed.last_seen_at == _NOW
    assert observed.last_decision_code == "INBOUND_CANDIDATE"


@pytest.mark.asyncio
async def test_configured_inbound_source_failure_fails_without_discovery_fallback(
    tmp_path: Path,
) -> None:
    store, account, preset = _setup(tmp_path)
    api = _Api(
        owned_threads=(_thread(_THREAD_ID),),
        conversation_failure=RuntimeError("reply text/token sentinel"),
    )

    with pytest.raises(NurtureRunnerError) as captured:
        await NurtureRunner(cast(LocalThreadsApiRuntime, api), store).run(account, preset, now=_NOW)

    assert captured.value.code == "DISCOVERY_SOURCE_FAILED"
    assert captured.value.run_id is not None
    assert api.discovery_calls == []
    assert api.conversation_calls == [("alice", _THREAD_ID, None)]
    with store.acquire_account_lock(account.id) as owner:
        assert captured.value.run_id is not None
        receipt = owner.get_run(captured.value.run_id)
    assert receipt.outcome == "FAILED"
    assert receipt.failed_stage == "conversation"
    assert "reply text" not in str(captured.value)


def _shortlist() -> tuple[NurtureCandidate, ...]:
    return (
        _candidate("nonmention-high", NurtureDiscoverySource.KEYWORD, score=1000),
        _candidate("mention-first", NurtureDiscoverySource.MENTIONS, score=1),
        _candidate("nonmention-second", NurtureDiscoverySource.TAG, score=999),
        _candidate("mention-second", NurtureDiscoverySource.MENTIONS, score=0),
    )


@pytest.mark.parametrize(
    ("recently_seen", "expected_id"),
    [
        ((), "mention-first"),
        (("mention-first",), "mention-second"),
        (("mention-first", "mention-second"), "nonmention-high"),
        (("nonmention-high", "mention-first", "nonmention-second", "mention-second"), None),
    ],
)
@pytest.mark.asyncio
async def test_runner_partitions_bounded_shortlist_and_preserves_group_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    recently_seen: tuple[str, ...],
    expected_id: str | None,
) -> None:
    store, account, preset = _setup(tmp_path)
    for remote_id in recently_seen:
        _seed_seen(
            store,
            account,
            preset,
            fingerprint_remote_thread(remote_id),
            _NOW - timedelta(seconds=1),
        )
    calls = 0

    async def discovery(*_args: object, **_kwargs: object) -> NurtureDiscoveryResult:
        nonlocal calls
        calls += 1
        candidates = _shortlist()
        return NurtureDiscoveryResult(
            source_call_counts=NurtureSourceCallCounts(mentions=1),
            discovered_count=4,
            deduped_count=4,
            rejected_count=0,
            selected_candidates=candidates,
            reason_codes=(),
        )

    monkeypatch.setattr(runner_module, "discover_nurture_candidates", discovery)
    api = _Api(
        owned_threads=(_thread(_THREAD_ID),),
        reply_pages={
            _THREAD_ID: ReplyPage(
                (
                    RemoteReply("unknown-reply", _REPLY_TEXT, None, _THREAD_ID, None, None),
                    RemoteReply("own-reply", _REPLY_TEXT, None, _THREAD_ID, None, True),
                ),
                None,
                False,
            )
        },
    )

    receipt = await NurtureRunner(cast(LocalThreadsApiRuntime, api), store).run(
        account, preset, now=_NOW
    )

    assert calls == 1
    assert receipt.discovered_count == 6
    assert receipt.deduped_count == 6
    if expected_id is None:
        assert receipt.selected_count == 0
        assert receipt.decision_codes == ("NO_ACTION",)
        assert receipt.skipped_count == 6
    else:
        assert receipt.selected_count == 1
        assert receipt.decision_codes == ("OBSERVE_ONLY",)
        with store.acquire_account_lock(account.id) as owner:
            targets = owner.get_targets(preset)
        surfaced = next(
            target
            for target in targets
            if target.last_run_id == receipt.id and target.last_decision_code == "OBSERVE_ONLY"
        )
        assert surfaced.fingerprint == fingerprint_remote_thread(expected_id)
