from __future__ import annotations

import re
from pathlib import Path
from uuid import UUID

import pytest

import threads_platform.standalone.accounts as account_module
from threads_platform.standalone.__main__ import main


def _set_data_root(monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
    monkeypatch.setenv("THREADS_LOCAL_DATA_ROOT", str(root))


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
