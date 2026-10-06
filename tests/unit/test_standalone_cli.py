from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from uuid import UUID

import httpx2
import pytest
from pydantic import SecretStr

import threads_platform.standalone.__main__ as cli_module
import threads_platform.standalone.accounts as account_module
from threads_platform.application.ports.threads import (
    DiscoveryPage,
    MediaContainer,
    MediaContainerRequest,
    PublishingQuota,
    RemoteDiscoveryThread,
    RemoteMedia,
    RemotePublicProfile,
    RemoteReply,
    ReplyPage,
    ThreadsAPIError,
    ThreadsCredentialError,
    ThreadsCredentialErrorCode,
)
from threads_platform.config.settings import Settings
from threads_platform.domain.discovery import DiscoverySearchMode, DiscoverySearchType
from threads_platform.infrastructure.threads_api.client import HttpThreadsAPI
from threads_platform.standalone.__main__ import main
from threads_platform.standalone.api import LocalThreadsApiRuntime, StandaloneApiError
from threads_platform.standalone.mutations import (
    CreatedReplyResult,
    LocalOperationStore,
    LocalThreadsMutationRuntime,
    PublishedMediaResult,
    PublishedTextResult,
    StandaloneMutationError,
)
from threads_platform.standalone.runtime import LocalRuntime, StandaloneRuntimeError


def _set_data_root(monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
    monkeypatch.setenv("THREADS_LOCAL_DATA_ROOT", str(root))


def _write_workflow(path: Path, steps: list[object], *, account: str = "alice") -> Path:
    path.write_text(
        json.dumps({"version": 1, "account": account, "steps": steps}),
        encoding="utf-8",
    )
    return path


class _FakeAsyncClientContext:
    def __init__(self) -> None:
        self.enter_count = 0
        self.exit_count = 0

    async def __aenter__(self) -> _FakeAsyncClientContext:
        self.enter_count += 1
        return self

    async def __aexit__(self, *args: object) -> None:
        self.exit_count += 1


def _install_fake_api_client(
    monkeypatch: pytest.MonkeyPatch,
) -> _FakeAsyncClientContext:
    context = _FakeAsyncClientContext()

    def fake_builder(_settings: Settings) -> httpx2.AsyncClient:
        return cast(httpx2.AsyncClient, context)

    monkeypatch.setattr(
        cli_module,
        "build_threads_http_client",
        fake_builder,
    )
    return context


def test_cli_add_prints_alias_and_uuid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _set_data_root(monkeypatch, tmp_path / "local")

    result = main(["account", "add", "alice"])

    captured = capsys.readouterr()
    assert result == 0
    assert re.fullmatch(r"added alice [0-9a-f-]{36}\n", captured.out)
    assert UUID(captured.out.split()[2])
    assert captured.err == ""


def test_cli_list_in_a_later_call_reads_persisted_account(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _set_data_root(monkeypatch, tmp_path / "local")
    assert main(["account", "add", "alice"]) == 0
    added = capsys.readouterr().out.split()[2]

    result = main(["account", "list"])

    captured = capsys.readouterr()
    assert result == 0
    assert captured.out == f"alice {added}\n"
    assert captured.err == ""


def test_cli_duplicate_uses_fixed_error_and_exit_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _set_data_root(monkeypatch, tmp_path / "local")
    assert main(["account", "add", "alice"]) == 0
    capsys.readouterr()

    result = main(["account", "add", "alice"])

    captured = capsys.readouterr()
    assert result == 1
    assert captured.out == ""
    assert captured.err == "ERROR ACCOUNT_ALREADY_EXISTS\n"
    assert "Traceback" not in captured.err


def test_cli_invalid_alias_uses_fixed_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _set_data_root(monkeypatch, Path("unused"))

    result = main(["account", "add", "../alice"])

    captured = capsys.readouterr()
    assert result == 1
    assert captured.out == ""
    assert captured.err == "ERROR INVALID_ACCOUNT_ALIAS\n"
    assert "Traceback" not in captured.err


def test_cli_missing_non_windows_root_returns_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("THREADS_LOCAL_DATA_ROOT", raising=False)
    monkeypatch.setattr(account_module.os, "name", "posix")
    monkeypatch.delenv("LOCALAPPDATA", raising=False)

    result = main(["account", "list"])

    captured = capsys.readouterr()
    assert result == 1
    assert captured.out == ""
    assert captured.err == "ERROR LOCAL_DATA_ROOT_REQUIRED\n"
    assert "Traceback" not in captured.err


def test_cli_rejects_unknown_commands_with_argparse_code_two(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as error:
        main(["unknown"])

    assert error.value.code == 2
    assert "usage:" in capsys.readouterr().err


def test_cli_login_success_prints_exact_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _set_data_root(monkeypatch, tmp_path / "local")
    calls: list[str] = []

    async def fake_login(runtime: LocalRuntime, alias: str) -> None:
        calls.append(alias)

    monkeypatch.setattr(cli_module.LocalRuntime, "login", fake_login)

    result = main(["account", "login", "alice"])

    captured = capsys.readouterr()
    assert result == 0
    assert calls == ["alice"]
    assert captured.out == "login browser closed alice\n"
    assert captured.err == ""


def test_cli_login_missing_account_has_exact_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _set_data_root(monkeypatch, tmp_path / "local")

    result = main(["account", "login", "alice"])

    captured = capsys.readouterr()
    assert result == 1
    assert captured.out == ""
    assert captured.err == "ERROR ACCOUNT_NOT_FOUND\n"
    assert "Traceback" not in captured.err


def test_cli_login_busy_has_exact_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _set_data_root(monkeypatch, tmp_path / "local")

    async def busy_login(runtime: LocalRuntime, alias: str) -> None:
        raise StandaloneRuntimeError("ACCOUNT_BUSY")

    monkeypatch.setattr(cli_module.LocalRuntime, "login", busy_login)

    result = main(["account", "login", "alice"])

    captured = capsys.readouterr()
    assert result == 1
    assert captured.out == ""
    assert captured.err == "ERROR ACCOUNT_BUSY\n"
    assert "Traceback" not in captured.err


def test_cli_login_keyboard_interrupt_returns_130(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _set_data_root(monkeypatch, tmp_path / "local")

    async def interrupt_login(runtime: LocalRuntime, alias: str) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(cli_module.LocalRuntime, "login", interrupt_login)

    result = main(["account", "login", "alice"])

    captured = capsys.readouterr()
    assert result == 130
    assert captured.out == ""
    assert captured.err == "ERROR INTERRUPTED\n"
    assert "Traceback" not in captured.err


def test_cli_set_env_credential_prints_only_alias(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _set_data_root(monkeypatch, tmp_path / "local")
    assert main(["account", "add", "alice"]) == 0
    capsys.readouterr()

    result = main(
        ["account", "credential", "set-env", "alice", "THREADS_PLATFORM_THREADS_TOKEN_A_V1"]
    )

    captured = capsys.readouterr()
    assert result == 0
    assert captured.out == "credential configured alice\n"
    assert "env://" not in captured.out
    assert captured.err == ""
    account = account_module.LocalAccountStore(tmp_path / "local").get("alice")
    assert account.credential_ref == "env://THREADS_PLATFORM_THREADS_TOKEN_A_V1"


def test_cli_set_env_credential_invalid_value_is_not_echoed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _set_data_root(monkeypatch, tmp_path / "local")
    assert main(["account", "add", "alice"]) == 0
    capsys.readouterr()
    account_path = tmp_path / "local" / "accounts" / "alice.json"
    original_document = account_path.read_bytes()
    secret_sentinel = "raw-token-sentinel-do-not-echo"

    result = main(["account", "credential", "set-env", "alice", secret_sentinel])

    captured = capsys.readouterr()
    assert result == 1
    assert captured.out == ""
    assert captured.err == "ERROR THREADS_CREDENTIAL_INVALID\n"
    assert secret_sentinel not in captured.out + captured.err
    assert account_path.read_bytes() == original_document


def test_cli_api_quota_exact_output_and_client_close(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _set_data_root(monkeypatch, tmp_path / "local")
    context = _install_fake_api_client(monkeypatch)

    async def fake_quota(runtime: LocalThreadsApiRuntime, alias: str) -> PublishingQuota:
        assert alias == "alice"
        return PublishingQuota(usage=4, total=250, reply_usage=None, reply_total=100)

    monkeypatch.setattr(cli_module.LocalThreadsApiRuntime, "quota", fake_quota)

    result = main(["api", "quota", "alice"])

    captured = capsys.readouterr()
    assert result == 0
    assert captured.out == "quota usage=4 total=250 reply_usage=- reply_total=100\n"
    assert captured.err == ""
    assert context.enter_count == 1
    assert context.exit_count == 1


def test_cli_api_media_collapses_and_caps_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _set_data_root(monkeypatch, tmp_path / "local")
    _install_fake_api_client(monkeypatch)
    text = "  first\n\tsecond " + "x" * 510

    async def fake_media(runtime: LocalThreadsApiRuntime, alias: str, media_id: str) -> RemoteMedia:
        assert (alias, media_id) == ("alice", "media-1")
        return RemoteMedia("media-1", text, None, "2026-10-05T00:00:00Z")

    monkeypatch.setattr(cli_module.LocalThreadsApiRuntime, "media", fake_media)

    result = main(["api", "media", "alice", "media-1"])

    captured = capsys.readouterr()
    bounded_text = ("first second " + "x" * 510)[:500]
    assert result == 0
    assert captured.out == (
        f"media media-1\ntimestamp 2026-10-05T00:00:00Z\npermalink -\ntext {bounded_text}\n"
    )
    assert captured.err == ""


def test_cli_api_replies_formats_one_page_and_forwards_cursor_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _set_data_root(monkeypatch, tmp_path / "local")
    _install_fake_api_client(monkeypatch)
    calls: list[tuple[str, str, str | None]] = []

    async def fake_replies(
        runtime: LocalThreadsApiRuntime,
        alias: str,
        thread_id: str,
        *,
        after: str | None = None,
    ) -> ReplyPage:
        calls.append((alias, thread_id, after))
        return ReplyPage(
            (RemoteReply("reply-1", " first\nreply ", None, "root-1", "parent-1"),),
            "next-2",
            True,
        )

    monkeypatch.setattr(cli_module.LocalThreadsApiRuntime, "replies", fake_replies)

    result = main(["api", "replies", "alice", "thread-1", "--after", "cursor opaque"])

    captured = capsys.readouterr()
    assert result == 0
    assert calls == [("alice", "thread-1", "cursor opaque")]
    assert captured.out == (
        "replies count=1 has_more=true next_cursor=next-2\n"
        "reply reply-1 timestamp=- root=root-1 parent=parent-1 text=first reply\n"
    )
    assert captured.err == ""


def test_cli_api_conversation_formats_one_page(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _set_data_root(monkeypatch, tmp_path / "local")
    _install_fake_api_client(monkeypatch)

    async def fake_conversation(
        runtime: LocalThreadsApiRuntime,
        alias: str,
        thread_id: str,
        *,
        after: str | None = None,
    ) -> ReplyPage:
        return ReplyPage(
            (RemoteReply("reply-2", "answer", "then", None, None),),
            None,
            False,
        )

    monkeypatch.setattr(cli_module.LocalThreadsApiRuntime, "conversation", fake_conversation)

    result = main(["api", "conversation", "alice", "thread-1"])

    captured = capsys.readouterr()
    assert result == 0
    assert captured.out == (
        "conversation count=1 has_more=false next_cursor=-\n"
        "reply reply-2 timestamp=then root=- parent=- text=answer\n"
    )
    assert captured.err == ""


def test_cli_api_public_profile_normalizes_and_bounds_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _set_data_root(monkeypatch, tmp_path / "local")
    context = _install_fake_api_client(monkeypatch)
    biography = " biography\n" + "b" * 510

    async def fake_profile(
        runtime: LocalThreadsApiRuntime, alias: str, username: str
    ) -> RemotePublicProfile:
        assert (alias, username) == ("alice", "public_user")
        return RemotePublicProfile(
            "profile\nid",
            "public_user",
            " Example\n User\x1bEND ",
            biography,
            "http://example.test/profile.jpg",
        )

    monkeypatch.setattr(cli_module.LocalThreadsApiRuntime, "public_profile", fake_profile)

    result = main(["api", "public-profile", "alice", "public_user"])

    captured = capsys.readouterr()
    assert result == 0
    assert captured.out == (
        f"profile id=profile id username=public_user name=Example User END "
        f"bio={('biography ' + 'b' * 510)[:500]} picture=-\n"
    )
    assert captured.err == ""
    assert context.enter_count == context.exit_count == 1


def test_cli_api_profile_posts_forwards_pagination_and_formats_page(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _set_data_root(monkeypatch, tmp_path / "local")
    context = _install_fake_api_client(monkeypatch)
    calls: list[tuple[str, str, str | None, int]] = []

    async def fake_profile_posts(
        runtime: LocalThreadsApiRuntime,
        alias: str,
        username: str,
        *,
        after: str | None = None,
        limit: int = 25,
    ) -> DiscoveryPage:
        calls.append((alias, username, after, limit))
        thread = RemoteDiscoveryThread(
            "thread-1",
            "author-1",
            None,
            "post text",
            "https://example.test/thread/1",
            "TEXT",
            None,
            None,
            None,
        )
        return DiscoveryPage((thread,), "next-posts", True)

    monkeypatch.setattr(
        cli_module.LocalThreadsApiRuntime,
        "profile_posts",
        fake_profile_posts,
    )

    result = main(
        ["api", "profile-posts", "alice", "public_user", "--after", "cursor-1", "--limit", "7"]
    )

    captured = capsys.readouterr()
    assert result == 0
    assert calls == [("alice", "public_user", "cursor-1", 7)]
    assert captured.out == (
        "profile-posts count=1 has_more=true next_cursor=next-posts\n"
        "thread thread-1 username=- timestamp=- media_type=TEXT quote=- has_replies=- "
        "permalink=https://example.test/thread/1 text=post text\n"
    )
    assert captured.err == ""
    assert context.enter_count == context.exit_count == 1


def test_cli_api_search_maps_choices_and_bounds_page_output_without_query(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _set_data_root(monkeypatch, tmp_path / "local")
    context = _install_fake_api_client(monkeypatch)
    query_sentinel = "private-query-sentinel"
    thread_text = " first\n\tsecond " + "x" * 510
    calls: list[tuple[str, str, DiscoverySearchMode, DiscoverySearchType, str | None, int]] = []

    async def fake_search(
        runtime: LocalThreadsApiRuntime,
        alias: str,
        query: str,
        *,
        search_mode: DiscoverySearchMode,
        search_type: DiscoverySearchType,
        after: str | None = None,
        limit: int = 25,
    ) -> DiscoveryPage:
        calls.append((alias, query, search_mode, search_type, after, limit))
        first = RemoteDiscoveryThread(
            "thread-1",
            "author-1",
            "alice",
            thread_text,
            "https://example.test/thread/1",
            "TEXT",
            datetime(2026, 10, 5, tzinfo=UTC),
            True,
            False,
        )
        second = RemoteDiscoveryThread("thread-2", None, None, None, None, None, None, None, None)
        return DiscoveryPage((first, second), "next-search", True)

    monkeypatch.setattr(cli_module.LocalThreadsApiRuntime, "search", fake_search)

    result = main(
        [
            "api",
            "search",
            "alice",
            query_sentinel,
            "--mode",
            "tag",
            "--type",
            "recent",
            "--after",
            "search-cursor",
            "--limit",
            "1",
        ]
    )

    captured = capsys.readouterr()
    bounded_text = ("first second " + "x" * 510)[:500]
    assert result == 0
    assert calls == [
        (
            "alice",
            query_sentinel,
            DiscoverySearchMode.TAG,
            DiscoverySearchType.RECENT,
            "search-cursor",
            1,
        )
    ]
    assert captured.out == (
        "search count=1 has_more=true next_cursor=next-search\n"
        "thread thread-1 username=alice timestamp=2026-10-05T00:00:00+00:00 "
        f"media_type=TEXT quote=true has_replies=false permalink=https://example.test/thread/1 "
        f"text={bounded_text}\n"
    )
    assert query_sentinel not in captured.out + captured.err
    assert captured.err == ""
    assert context.enter_count == context.exit_count == 1


def test_cli_api_mentions_formats_cursor_and_accepts_canonical_options(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _set_data_root(monkeypatch, tmp_path / "local")
    context = _install_fake_api_client(monkeypatch)
    calls: list[tuple[str, str | None, int]] = []

    async def fake_mentions(
        runtime: LocalThreadsApiRuntime,
        alias: str,
        *,
        after: str | None = None,
        limit: int = 25,
    ) -> DiscoveryPage:
        calls.append((alias, after, limit))
        return DiscoveryPage((), "next-mentions", True)

    monkeypatch.setattr(cli_module.LocalThreadsApiRuntime, "mentions", fake_mentions)

    result = main(["api", "mentions", "alice", "--after", "mentions-cursor", "--limit", "4"])

    captured = capsys.readouterr()
    assert result == 0
    assert calls == [("alice", "mentions-cursor", 4)]
    assert captured.out == "mentions count=0 has_more=true next_cursor=next-mentions\n"
    assert captured.err == ""
    assert context.enter_count == context.exit_count == 1


@pytest.mark.parametrize(
    ("argv", "method_name", "failure", "expected_code"),
    [
        (
            ["api", "quota", "alice"],
            "quota",
            ThreadsCredentialError(ThreadsCredentialErrorCode.SECRET_UNAVAILABLE),
            "THREADS_CREDENTIAL_SECRET_UNAVAILABLE",
        ),
        (
            ["api", "media", "alice", "media-1"],
            "media",
            ThreadsAPIError("THREADS_TRANSPORT_FAILURE"),
            "THREADS_TRANSPORT_FAILURE",
        ),
        (
            ["api", "media", "alice", "media-1"],
            "media",
            StandaloneApiError("INVALID_MEDIA_ID"),
            "INVALID_MEDIA_ID",
        ),
        (
            ["api", "public-profile", "alice", "username"],
            "public_profile",
            ThreadsAPIError("THREADS_TRANSPORT_FAILURE"),
            "THREADS_TRANSPORT_FAILURE",
        ),
        (
            ["api", "profile-posts", "alice", "username"],
            "profile_posts",
            ThreadsCredentialError(ThreadsCredentialErrorCode.SECRET_UNAVAILABLE),
            "THREADS_CREDENTIAL_SECRET_UNAVAILABLE",
        ),
        (
            ["api", "search", "alice", "query", "--mode", "keyword", "--type", "top"],
            "search",
            ThreadsAPIError("THREADS_TRANSPORT_FAILURE"),
            "THREADS_TRANSPORT_FAILURE",
        ),
        (
            ["api", "mentions", "alice"],
            "mentions",
            ThreadsAPIError("THREADS_TRANSPORT_FAILURE"),
            "THREADS_TRANSPORT_FAILURE",
        ),
    ],
)
def test_cli_api_errors_are_safe_and_close_client(
    argv: list[str],
    method_name: str,
    failure: Exception,
    expected_code: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _set_data_root(monkeypatch, tmp_path / "local")
    context = _install_fake_api_client(monkeypatch)
    token_sentinel = "api-token-sentinel-not-for-output"

    async def fail_call(runtime: LocalThreadsApiRuntime, *args: object, **kwargs: object) -> object:
        raise failure

    monkeypatch.setattr(cli_module.LocalThreadsApiRuntime, method_name, fail_call)

    result = main(argv)

    captured = capsys.readouterr()
    assert result == 1
    assert captured.out == ""
    assert captured.err == f"ERROR {expected_code}\n"
    assert token_sentinel not in captured.out + captured.err
    assert context.enter_count == 1
    assert context.exit_count == 1


def test_cli_never_prints_resolved_token_on_api_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _set_data_root(monkeypatch, tmp_path / "local")
    context = _install_fake_api_client(monkeypatch)
    token_sentinel = "resolved-api-token-sentinel-do-not-print"
    monkeypatch.setenv("THREADS_PLATFORM_THREADS_TOKEN_ACCOUNT_A_V1", token_sentinel)
    store = account_module.LocalAccountStore(tmp_path / "local")
    store.add("alice")
    store.set_credential_ref("alice", "env://THREADS_PLATFORM_THREADS_TOKEN_ACCOUNT_A_V1")

    class _FailingApi:
        async def get_publishing_quota(self, token: SecretStr) -> PublishingQuota:
            assert token.get_secret_value() == token_sentinel
            raise ThreadsAPIError("THREADS_TRANSPORT_FAILURE")

    def fake_http_api(_client: httpx2.AsyncClient) -> HttpThreadsAPI:
        return cast(HttpThreadsAPI, _FailingApi())

    monkeypatch.setattr(cli_module, "HttpThreadsAPI", fake_http_api)

    result = main(["api", "quota", "alice"])

    captured = capsys.readouterr()
    assert result == 1
    assert captured.out == ""
    assert captured.err == "ERROR THREADS_TRANSPORT_FAILURE\n"
    assert token_sentinel not in captured.out + captured.err
    assert context.enter_count == 1
    assert context.exit_count == 1


def test_cli_post_success_has_exact_output_and_closes_http_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _set_data_root(monkeypatch, tmp_path / "local")
    context = _install_fake_api_client(monkeypatch)
    operation_id = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
    calls: list[tuple[str, str]] = []

    async def fake_publish(runtime: object, alias: str, text: str) -> PublishedTextResult:
        calls.append((alias, text))
        return PublishedTextResult(operation_id, "media-123")

    monkeypatch.setattr(cli_module.LocalThreadsMutationRuntime, "publish_text", fake_publish)

    result = main(["post", "alice", "chosen text"])

    captured = capsys.readouterr()
    assert result == 0
    assert calls == [("alice", "chosen text")]
    assert captured.out == f"published operation={operation_id} media=media-123\n"
    assert captured.err == ""
    assert context.enter_count == 1
    assert context.exit_count == 1


def test_cli_reply_success_has_exact_output_and_closes_http_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _set_data_root(monkeypatch, tmp_path / "local")
    context = _install_fake_api_client(monkeypatch)
    operation_id = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
    calls: list[tuple[str, str, str, str | None]] = []

    async def fake_reply(
        runtime: LocalThreadsMutationRuntime,
        alias: str,
        thread_id: str,
        text: str,
        *,
        parent_reply_id: str | None = None,
    ) -> CreatedReplyResult:
        calls.append((alias, thread_id, text, parent_reply_id))
        return CreatedReplyResult(operation_id, "reply-123")

    monkeypatch.setattr(cli_module.LocalThreadsMutationRuntime, "create_reply", fake_reply)

    result = main(
        [
            "reply",
            "alice",
            "thread-456",
            "chosen reply text",
            "--parent-reply-id",
            "reply-789",
        ]
    )

    captured = capsys.readouterr()
    assert result == 0
    assert calls == [("alice", "thread-456", "chosen reply text", "reply-789")]
    assert captured.out == f"replied operation={operation_id} reply=reply-123\n"
    assert "chosen reply text" not in captured.out + captured.err
    assert captured.err == ""
    assert context.enter_count == context.exit_count == 1


@pytest.mark.parametrize(
    ("media_type", "command", "expected_label"),
    [
        ("IMAGE", "post-image", "published-image"),
        ("VIDEO", "post-video", "published-video"),
    ],
)
def test_cli_media_post_success_has_exact_safe_output_and_closes_http_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    media_type: str,
    command: str,
    expected_label: str,
) -> None:
    _set_data_root(monkeypatch, tmp_path / "local")
    context = _install_fake_api_client(monkeypatch)
    operation_id = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
    media_url = f"https://{media_type.lower()}-url-secret.media.example/object"
    text = "media-post-text-secret"
    alt_text = "media-alt-text-secret"
    calls: list[tuple[str, str, str, str | None, str | None]] = []

    async def fake_image(
        _runtime: LocalThreadsMutationRuntime,
        alias: str,
        image_url: str,
        post_text: str | None = None,
        *,
        alt_text: str | None = None,
    ) -> PublishedMediaResult:
        calls.append(("IMAGE", alias, image_url, post_text, alt_text))
        return PublishedMediaResult(operation_id, "media-123")

    async def fake_video(
        _runtime: LocalThreadsMutationRuntime,
        alias: str,
        video_url: str,
        post_text: str | None = None,
        *,
        alt_text: str | None = None,
    ) -> PublishedMediaResult:
        calls.append(("VIDEO", alias, video_url, post_text, alt_text))
        return PublishedMediaResult(operation_id, "media-123")

    method = "publish_image" if media_type == "IMAGE" else "publish_video"
    replacement = fake_image if media_type == "IMAGE" else fake_video
    monkeypatch.setattr(cli_module.LocalThreadsMutationRuntime, method, replacement)

    result = main([command, "alice", media_url, text, "--alt-text", alt_text])

    captured = capsys.readouterr()
    assert result == 0
    assert calls == [(media_type, "alice", media_url, text, alt_text)]
    assert captured.out == f"{expected_label} operation={operation_id} media=media-123\n"
    assert captured.err == ""
    for forbidden in (media_url, text, alt_text):
        assert forbidden not in captured.out + captured.err
    assert context.enter_count == 1
    assert context.exit_count == 1


@pytest.mark.parametrize(
    ("media_type", "command"),
    [("IMAGE", "post-image"), ("VIDEO", "post-video")],
)
def test_cli_media_publish_ambiguous_error_redacts_all_mutation_inputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    media_type: str,
    command: str,
) -> None:
    root = tmp_path / "local"
    _set_data_root(monkeypatch, root)
    context = _install_fake_api_client(monkeypatch)
    token_sentinel = "media-token-secret-never-output"
    credential_name = "THREADS_PLATFORM_THREADS_TOKEN_MEDIA_TEST"
    credential_ref = f"env://{credential_name}"
    media_url = f"https://{media_type.lower()}-url-secret.media.example/object"
    text = "media-post-text-secret-never-output"
    alt_text = "media-alt-text-secret-never-output"
    monkeypatch.setenv(credential_name, token_sentinel)
    store = account_module.LocalAccountStore(root)
    store.add("alice")
    store.set_credential_ref("alice", credential_ref)
    api_calls: list[str] = []

    class _FailingMediaApi:
        async def get_publishing_quota(self, token: SecretStr) -> PublishingQuota:
            assert token.get_secret_value() == token_sentinel
            api_calls.append("quota")
            return PublishingQuota(usage=1, total=100)

        async def create_container(
            self, token: SecretStr, request: MediaContainerRequest
        ) -> MediaContainer:
            assert token.get_secret_value() == token_sentinel
            if media_type == "IMAGE":
                expected = MediaContainerRequest(
                    media_type="IMAGE",
                    image_url=media_url,
                    text=text,
                    alt_text=alt_text,
                )
            else:
                expected = MediaContainerRequest(
                    media_type="VIDEO",
                    video_url=media_url,
                    text=text,
                    alt_text=alt_text,
                )
            assert request == expected
            api_calls.append("create")
            return MediaContainer(container_id="container-123")

        async def get_container(self, token: SecretStr, container_id: str) -> MediaContainer:
            assert token.get_secret_value() == token_sentinel
            assert container_id == "container-123"
            api_calls.append("status")
            return MediaContainer(container_id, status="FINISHED")

        async def publish_container(self, token: SecretStr, container_id: str) -> str:
            assert token.get_secret_value() == token_sentinel
            assert container_id == "container-123"
            api_calls.append("publish")
            raise ThreadsAPIError("THREADS_RATE_LIMITED")

    def fake_http_api(_client: httpx2.AsyncClient) -> HttpThreadsAPI:
        return cast(HttpThreadsAPI, _FailingMediaApi())

    monkeypatch.setattr(cli_module, "HttpThreadsAPI", fake_http_api)

    result = main([command, "alice", media_url, text, "--alt-text", alt_text])

    captured = capsys.readouterr()
    assert result == 1
    assert captured.out == ""
    match = re.fullmatch(
        r"ERROR PUBLISH_OUTCOME_AMBIGUOUS operation=([0-9a-f-]{36})\n", captured.err
    )
    assert match is not None
    operation_id = UUID(match.group(1))
    assert api_calls == ["quota", "create", "status", "publish"]
    assert api_calls.count("publish") == 1
    operation = LocalOperationStore(root).get(operation_id)
    assert operation.kind == f"POST_{media_type}"
    assert operation.phase == "AMBIGUOUS"
    journal = (root / "operations" / f"{operation_id}.json").read_text(encoding="utf-8")
    for forbidden in (
        media_url,
        text,
        alt_text,
        token_sentinel,
        credential_ref,
        "Authorization",
    ):
        assert forbidden not in captured.out + captured.err + journal
    assert context.enter_count == 1
    assert context.exit_count == 1


@pytest.mark.parametrize(
    ("command", "url", "text", "alt_text", "expected_code"),
    [
        ("post-image", "file:///private/image.jpg", "private text", None, "INVALID_MEDIA_URL"),
        ("post-video", "https://media.example/video.mp4", " \t ", None, "INVALID_POST_TEXT"),
        ("post-image", "https://media.example/image.jpg", None, "x" * 1001, "INVALID_ALT_TEXT"),
    ],
)
def test_cli_media_inputs_fail_before_settings_or_http(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
    url: str,
    text: str | None,
    alt_text: str | None,
    expected_code: str,
) -> None:
    _set_data_root(monkeypatch, tmp_path / "local")
    calls: list[str] = []

    def fail_settings() -> Settings:
        calls.append("settings")
        raise AssertionError("invalid media input must be rejected before Settings")

    def fail_client(_settings: Settings) -> httpx2.AsyncClient:
        calls.append("http")
        raise AssertionError("invalid media input must be rejected before HTTP client")

    monkeypatch.setattr(cli_module, "Settings", fail_settings)
    monkeypatch.setattr(cli_module, "build_threads_http_client", fail_client)
    argv = [command, "alice", url]
    if text is not None:
        argv.append(text)
    if alt_text is not None:
        argv.extend(["--alt-text", alt_text])

    result = main(argv)

    captured = capsys.readouterr()
    assert result == 1
    assert captured.out == ""
    assert captured.err == f"ERROR {expected_code}\n"
    assert url not in captured.out + captured.err
    if text is not None:
        assert text not in captured.out + captured.err
    if alt_text is not None:
        assert alt_text not in captured.out + captured.err
    assert calls == []


@pytest.mark.parametrize("text", ["  \t", "x" * 501])
def test_cli_post_validation_is_bounded_and_does_not_echo_text(
    text: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _set_data_root(monkeypatch, tmp_path / "local")
    context = _install_fake_api_client(monkeypatch)

    result = main(["post", "alice", text])

    captured = capsys.readouterr()
    assert result == 1
    assert captured.out == ""
    assert captured.err == "ERROR INVALID_POST_TEXT\n"
    assert text not in captured.out + captured.err
    assert context.enter_count == 1
    assert context.exit_count == 1


def test_cli_post_ambiguous_error_includes_operation_and_redacts_token_and_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _set_data_root(monkeypatch, tmp_path / "local")
    context = _install_fake_api_client(monkeypatch)
    token_sentinel = "token-sentinel-never-output"
    text_sentinel = "post-text-sentinel-never-output"
    token_env_name = "THREADS_PLATFORM_THREADS_TOKEN_LOCAL07_TEST"
    monkeypatch.setenv(token_env_name, token_sentinel)
    store = account_module.LocalAccountStore(tmp_path / "local")
    store.add("alice")
    store.set_credential_ref("alice", f"env://{token_env_name}")
    api_calls: list[str] = []

    class _FailingPublishApi:
        async def get_publishing_quota(self, token: SecretStr) -> PublishingQuota:
            assert token.get_secret_value() == token_sentinel
            api_calls.append("quota")
            return PublishingQuota(usage=1, total=100)

        async def create_container(
            self, token: SecretStr, request: MediaContainerRequest
        ) -> MediaContainer:
            assert token.get_secret_value() == token_sentinel
            assert request == MediaContainerRequest(media_type="TEXT", text=text_sentinel)
            api_calls.append("create")
            return MediaContainer(container_id="container-123")

        async def publish_container(self, token: SecretStr, container_id: str) -> str:
            assert token.get_secret_value() == token_sentinel
            assert container_id == "container-123"
            api_calls.append("publish")
            raise ThreadsAPIError("THREADS_RATE_LIMITED")

    def fake_http_api(_client: httpx2.AsyncClient) -> HttpThreadsAPI:
        return cast(HttpThreadsAPI, _FailingPublishApi())

    monkeypatch.setattr(cli_module, "HttpThreadsAPI", fake_http_api)

    result = main(["post", "alice", text_sentinel])

    captured = capsys.readouterr()
    assert result == 1
    assert captured.out == ""
    match = re.fullmatch(
        r"ERROR PUBLISH_OUTCOME_AMBIGUOUS operation=([0-9a-f-]{36})\n", captured.err
    )
    assert match is not None
    operation_id = UUID(match.group(1))
    assert api_calls == ["quota", "create", "publish"]
    assert LocalOperationStore(tmp_path / "local").get(operation_id).phase == "AMBIGUOUS"
    assert token_sentinel not in captured.out + captured.err
    assert text_sentinel not in captured.out + captured.err
    assert context.enter_count == 1
    assert context.exit_count == 1


def test_cli_reply_ambiguous_error_redacts_token_text_and_target_ids(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _set_data_root(monkeypatch, tmp_path / "local")
    context = _install_fake_api_client(monkeypatch)
    token_sentinel = "reply-token-sentinel-never-output"
    text_sentinel = "reply-text-sentinel-never-output"
    credential_ref = "THREADS_PLATFORM_THREADS_TOKEN_REPLY_TEST"
    monkeypatch.setenv(credential_ref, token_sentinel)
    store = account_module.LocalAccountStore(tmp_path / "local")
    store.add("alice")
    store.set_credential_ref("alice", f"env://{credential_ref}")
    api_calls: list[str] = []

    class _FailingReplyApi:
        async def get_publishing_quota(self, token: SecretStr) -> PublishingQuota:
            assert token.get_secret_value() == token_sentinel
            api_calls.append("quota")
            return PublishingQuota(reply_usage=1, reply_total=100)

        async def create_container(
            self,
            token: SecretStr,
            request: MediaContainerRequest,
        ) -> MediaContainer:
            assert token.get_secret_value() == token_sentinel
            assert request == MediaContainerRequest(
                media_type="TEXT",
                text=text_sentinel,
                reply_to_id="reply-parent-2",
            )
            api_calls.append("create")
            return MediaContainer(container_id="container-123")

        async def publish_container(self, token: SecretStr, container_id: str) -> str:
            assert token.get_secret_value() == token_sentinel
            assert container_id == "container-123"
            api_calls.append("publish")
            raise ThreadsAPIError("THREADS_RATE_LIMITED")

    def fake_http_api(_client: httpx2.AsyncClient) -> HttpThreadsAPI:
        return cast(HttpThreadsAPI, _FailingReplyApi())

    monkeypatch.setattr(cli_module, "HttpThreadsAPI", fake_http_api)

    result = main(
        [
            "reply",
            "alice",
            "thread-root-1",
            text_sentinel,
            "--parent-reply-id",
            "reply-parent-2",
        ]
    )

    captured = capsys.readouterr()
    assert result == 1
    assert captured.out == ""
    match = re.fullmatch(
        r"ERROR PUBLISH_OUTCOME_AMBIGUOUS operation=([0-9a-f-]{36})\n", captured.err
    )
    assert match is not None
    operation_id = UUID(match.group(1))
    assert api_calls == ["quota", "create", "publish"]
    operation = LocalOperationStore(tmp_path / "local").get(operation_id)
    assert operation.kind == "CREATE_REPLY"
    assert operation.phase == "AMBIGUOUS"
    journal = (tmp_path / "local" / "operations" / f"{operation_id}.json").read_text()
    for forbidden in (
        text_sentinel,
        token_sentinel,
        credential_ref,
        "thread-root-1",
        "reply-parent-2",
        "Authorization",
    ):
        assert forbidden not in captured.out + captured.err + journal
    assert context.enter_count == context.exit_count == 1


def test_cli_operation_show_is_local_only_and_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "local"
    _set_data_root(monkeypatch, root)
    root.mkdir(parents=True)
    operation = LocalOperationStore(root).create_received(
        UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc")
    )

    def fail_if_http_is_built(_settings: Settings) -> httpx2.AsyncClient:
        raise AssertionError("operation show must not build an HTTP client")

    monkeypatch.setattr(cli_module, "build_threads_http_client", fail_if_http_is_built)

    result = main(["operation", "show", str(operation.id)])

    captured = capsys.readouterr()
    assert result == 0
    assert captured.out == (
        f"operation {operation.id} kind=POST_TEXT phase=RECEIVED container=- media=- outcome=-\n"
    )
    assert captured.err == ""


def test_cli_operation_show_renders_create_reply_journal_without_http(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "local"
    _set_data_root(monkeypatch, root)
    root.mkdir(parents=True)
    operation = LocalOperationStore(root).create_received(
        UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc"),
        kind="CREATE_REPLY",
    )

    def fail_if_http_is_built(_settings: Settings) -> httpx2.AsyncClient:
        raise AssertionError("operation show must not build an HTTP client")

    monkeypatch.setattr(cli_module, "build_threads_http_client", fail_if_http_is_built)

    result = main(["operation", "show", str(operation.id)])

    captured = capsys.readouterr()
    assert result == 0
    assert captured.out == (
        f"operation {operation.id} kind=CREATE_REPLY phase=RECEIVED container=- media=- outcome=-\n"
    )
    assert captured.err == ""


@pytest.mark.parametrize("kind", ["POST_IMAGE", "POST_VIDEO"])
def test_cli_operation_show_renders_media_journal_without_http(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    kind: str,
) -> None:
    root = tmp_path / "local"
    _set_data_root(monkeypatch, root)
    root.mkdir(parents=True)
    operation = LocalOperationStore(root).create_received(
        UUID("dddddddd-dddd-4ddd-8ddd-dddddddddddd"),
        kind=kind,
    )

    def fail_if_http_is_built(_settings: Settings) -> httpx2.AsyncClient:
        raise AssertionError("operation show must not build an HTTP client")

    monkeypatch.setattr(cli_module, "build_threads_http_client", fail_if_http_is_built)

    result = main(["operation", "show", str(operation.id)])

    captured = capsys.readouterr()
    assert result == 0
    assert captured.out == (
        f"operation {operation.id} kind={kind} phase=RECEIVED container=- media=- outcome=-\n"
    )
    assert captured.err == ""


def test_cli_operation_show_invalid_id_has_safe_error_without_http(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _set_data_root(monkeypatch, tmp_path / "local")

    def fail_if_http_is_built(_settings: Settings) -> httpx2.AsyncClient:
        raise AssertionError("operation show must not build an HTTP client")

    monkeypatch.setattr(cli_module, "build_threads_http_client", fail_if_http_is_built)

    result = main(["operation", "show", "not-an-operation-id"])

    captured = capsys.readouterr()
    assert result == 1
    assert captured.out == ""
    assert captured.err == "ERROR OPERATION_NOT_FOUND\n"


def test_cli_workflow_invalid_plan_is_rejected_before_settings_or_http_client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _set_data_root(monkeypatch, tmp_path / "local")
    workflow_path = _write_workflow(
        tmp_path / "private-workflow.json",
        [{"action": "quota"}, {"action": "unknown"}],
    )
    calls: list[str] = []

    def fail_settings() -> Settings:
        calls.append("settings")
        raise AssertionError("invalid workflow must be rejected before Settings")

    def fail_client(_settings: Settings) -> httpx2.AsyncClient:
        calls.append("http")
        raise AssertionError("invalid workflow must be rejected before HTTP client")

    monkeypatch.setattr(cli_module, "Settings", fail_settings)
    monkeypatch.setattr(cli_module, "build_threads_http_client", fail_client)

    result = main(["workflow", "run", str(workflow_path)])

    captured = capsys.readouterr()
    assert result == 1
    assert captured.out == ""
    assert captured.err == "ERROR INVALID_WORKFLOW\n"
    assert str(workflow_path) not in captured.out + captured.err
    assert calls == []


def test_cli_workflow_missing_account_is_rejected_before_http_client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _set_data_root(monkeypatch, tmp_path / "local")
    workflow_path = _write_workflow(tmp_path / "workflow.json", [{"action": "quota"}])
    calls: list[str] = []

    def fail_client(_settings: Settings) -> httpx2.AsyncClient:
        calls.append("http")
        raise AssertionError("missing local account must fail before HTTP client")

    monkeypatch.setattr(cli_module, "build_threads_http_client", fail_client)

    result = main(["workflow", "run", str(workflow_path)])

    captured = capsys.readouterr()
    assert result == 1
    assert captured.out == ""
    assert captured.err == "ERROR ACCOUNT_NOT_FOUND\n"
    assert calls == []


def test_cli_workflow_mixed_reads_buffer_exact_output_and_uses_one_http_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "local"
    _set_data_root(monkeypatch, root)
    account_module.LocalAccountStore(root).add("alice")
    workflow_path = _write_workflow(
        tmp_path / "workflow.json",
        [
            {"action": "quota"},
            {"action": "media", "media_id": "media-1"},
            {"action": "replies", "thread_id": "thread-1", "after": "cursor-in"},
            {"action": "conversation", "thread_id": "thread-2"},
        ],
    )
    context = _install_fake_api_client(monkeypatch)
    calls: list[tuple[object, ...]] = []

    async def fake_quota(runtime: LocalThreadsApiRuntime, alias: str) -> PublishingQuota:
        calls.append(("quota", alias))
        return PublishingQuota(3, 100, None, 20)

    async def fake_media(runtime: LocalThreadsApiRuntime, alias: str, media_id: str) -> RemoteMedia:
        calls.append(("media", alias, media_id))
        return RemoteMedia(
            "media-1", "  public\n media text  ", "https://www.threads.com/t/1", "then"
        )

    async def fake_replies(
        runtime: LocalThreadsApiRuntime,
        alias: str,
        thread_id: str,
        *,
        after: str | None = None,
    ) -> ReplyPage:
        calls.append(("replies", alias, thread_id, after))
        return ReplyPage(
            (RemoteReply("reply-1", "  reply\n text ", None, "root-1", None),), "cursor-out", True
        )

    async def fake_conversation(
        runtime: LocalThreadsApiRuntime,
        alias: str,
        thread_id: str,
        *,
        after: str | None = None,
    ) -> ReplyPage:
        calls.append(("conversation", alias, thread_id, after))
        return ReplyPage((), None, False)

    def fail_if_browser_login(*args: object, **kwargs: object) -> None:
        raise AssertionError("workflow commands must not invoke browser login")

    monkeypatch.setattr(cli_module.LocalThreadsApiRuntime, "quota", fake_quota)
    monkeypatch.setattr(cli_module.LocalThreadsApiRuntime, "media", fake_media)
    monkeypatch.setattr(cli_module.LocalThreadsApiRuntime, "replies", fake_replies)
    monkeypatch.setattr(cli_module.LocalThreadsApiRuntime, "conversation", fake_conversation)
    monkeypatch.setattr(cli_module.LocalRuntime, "login", fail_if_browser_login)

    result = main(["workflow", "run", str(workflow_path)])

    captured = capsys.readouterr()
    assert result == 0
    assert captured.out == (
        "workflow step=1 action=quota usage=3 total=100 reply_usage=- reply_total=20\n"
        "workflow step=2 action=media id=media-1 timestamp=then "
        "permalink=https://www.threads.com/t/1 text=public media text\n"
        "workflow step=3 action=replies count=1 has_more=true next_cursor=cursor-out\n"
        "workflow step=3 reply reply-1 timestamp=- root=root-1 parent=- text=reply text\n"
        "workflow step=4 action=conversation count=0 has_more=false next_cursor=-\n"
        "workflow completed steps=4\n"
    )
    assert captured.err == ""
    assert calls == [
        ("quota", "alice"),
        ("media", "alice", "media-1"),
        ("replies", "alice", "thread-1", "cursor-in"),
        ("conversation", "alice", "thread-2", None),
    ]
    assert context.enter_count == 1
    assert context.exit_count == 1


def test_cli_workflow_post_success_is_exact_and_never_echoes_text(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "local"
    _set_data_root(monkeypatch, root)
    account_module.LocalAccountStore(root).add("alice")
    text_sentinel = "chosen workflow post text do not echo"
    token_sentinel = "workflow-token-never-output"
    monkeypatch.setenv("THREADS_PLATFORM_THREADS_TOKEN_WORKFLOW_TEST", token_sentinel)
    workflow_path = _write_workflow(
        tmp_path / "workflow.json",
        [{"action": "post_text", "text": text_sentinel}],
    )
    context = _install_fake_api_client(monkeypatch)
    operation_id = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
    calls: list[tuple[str, str]] = []

    async def fake_publish(
        runtime: LocalThreadsMutationRuntime, alias: str, text: str
    ) -> PublishedTextResult:
        calls.append((alias, text))
        return PublishedTextResult(operation_id, "published-123")

    monkeypatch.setattr(cli_module.LocalThreadsMutationRuntime, "publish_text", fake_publish)

    result = main(["workflow", "run", str(workflow_path)])

    captured = capsys.readouterr()
    assert result == 0
    assert calls == [("alice", text_sentinel)]
    assert captured.out == (
        f"workflow step=1 action=post_text published operation={operation_id} "
        "media=published-123\nworkflow completed steps=1\n"
    )
    assert captured.err == ""
    assert text_sentinel not in captured.out + captured.err
    assert token_sentinel not in captured.out + captured.err
    assert context.enter_count == 1
    assert context.exit_count == 1


def test_cli_workflow_failure_discards_buffered_output_and_closes_http_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "local"
    _set_data_root(monkeypatch, root)
    account_module.LocalAccountStore(root).add("alice")
    workflow_path = _write_workflow(
        tmp_path / "workflow.json",
        [
            {"action": "quota"},
            {"action": "media", "media_id": "media-1"},
            {"action": "conversation", "thread_id": "thread-2"},
        ],
    )
    context = _install_fake_api_client(monkeypatch)
    calls: list[str] = []

    async def fake_quota(runtime: LocalThreadsApiRuntime, alias: str) -> PublishingQuota:
        calls.append("quota")
        return PublishingQuota(3, 100)

    async def fail_media(runtime: LocalThreadsApiRuntime, alias: str, media_id: str) -> RemoteMedia:
        calls.append("media")
        raise StandaloneApiError("INVALID_MEDIA_ID")

    async def fake_conversation(
        runtime: LocalThreadsApiRuntime,
        alias: str,
        thread_id: str,
        *,
        after: str | None = None,
    ) -> ReplyPage:
        calls.append("conversation")
        return ReplyPage((), None, False)

    monkeypatch.setattr(cli_module.LocalThreadsApiRuntime, "quota", fake_quota)
    monkeypatch.setattr(cli_module.LocalThreadsApiRuntime, "media", fail_media)
    monkeypatch.setattr(cli_module.LocalThreadsApiRuntime, "conversation", fake_conversation)

    result = main(["workflow", "run", str(workflow_path)])

    captured = capsys.readouterr()
    assert result == 1
    assert captured.out == ""
    assert captured.err == "ERROR INVALID_MEDIA_ID step=2\n"
    assert calls == ["quota", "media"]
    assert context.enter_count == 1
    assert context.exit_count == 1


def test_cli_workflow_ambiguous_post_error_includes_step_and_operation_without_secrets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "local"
    _set_data_root(monkeypatch, root)
    account_module.LocalAccountStore(root).add("alice")
    text_sentinel = "workflow-post-private-text"
    token_sentinel = "workflow-secret-token"
    workflow_path = _write_workflow(
        tmp_path / "private-workflow.json",
        [{"action": "quota"}, {"action": "post_text", "text": text_sentinel}],
    )
    context = _install_fake_api_client(monkeypatch)
    operation_id = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")

    async def fake_quota(runtime: LocalThreadsApiRuntime, alias: str) -> PublishingQuota:
        return PublishingQuota(1, 100)

    async def fail_publish(
        runtime: LocalThreadsMutationRuntime, alias: str, text: str
    ) -> PublishedTextResult:
        assert text == text_sentinel
        raise StandaloneMutationError("PUBLISH_OUTCOME_AMBIGUOUS", operation_id)

    monkeypatch.setenv("THREADS_PLATFORM_THREADS_TOKEN_WORKFLOW_TEST", token_sentinel)
    monkeypatch.setattr(cli_module.LocalThreadsApiRuntime, "quota", fake_quota)
    monkeypatch.setattr(cli_module.LocalThreadsMutationRuntime, "publish_text", fail_publish)

    result = main(["workflow", "run", str(workflow_path)])

    captured = capsys.readouterr()
    assert result == 1
    assert captured.out == ""
    assert captured.err == (f"ERROR PUBLISH_OUTCOME_AMBIGUOUS step=2 operation={operation_id}\n")
    assert str(workflow_path) not in captured.err
    assert text_sentinel not in captured.out + captured.err
    assert token_sentinel not in captured.out + captured.err
    assert context.enter_count == 1
    assert context.exit_count == 1
