from __future__ import annotations

import re
from pathlib import Path
from typing import cast
from uuid import UUID

import httpx2
import pytest
from pydantic import SecretStr

import threads_platform.standalone.__main__ as cli_module
import threads_platform.standalone.accounts as account_module
from threads_platform.application.ports.threads import (
    PublishingQuota,
    RemoteMedia,
    RemoteReply,
    ReplyPage,
    ThreadsAPIError,
    ThreadsCredentialError,
    ThreadsCredentialErrorCode,
)
from threads_platform.config.settings import Settings
from threads_platform.infrastructure.threads_api.client import HttpThreadsAPI
from threads_platform.standalone.__main__ import main
from threads_platform.standalone.api import LocalThreadsApiRuntime, StandaloneApiError
from threads_platform.standalone.runtime import LocalRuntime, StandaloneRuntimeError


def _set_data_root(monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
    monkeypatch.setenv("THREADS_LOCAL_DATA_ROOT", str(root))


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
