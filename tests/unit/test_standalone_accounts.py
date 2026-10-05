from __future__ import annotations

import json
from pathlib import Path
from uuid import UUID

import pytest

import threads_platform.standalone.accounts as account_module
from threads_platform.standalone.accounts import (
    LOCAL_ACCOUNT_SCHEMA_VERSION,
    THREADS_LOCAL_DATA_ROOT_ENV,
    LocalAccountStore,
    StandaloneAccountError,
    resolve_standalone_data_root,
)


def test_explicit_data_root_is_resolved_without_creating_it(tmp_path: Path) -> None:
    root = tmp_path / "local-data"

    result = resolve_standalone_data_root({THREADS_LOCAL_DATA_ROOT_ENV: str(root)})

    assert result == root.resolve(strict=False)
    assert not root.exists()


def test_windows_default_data_root_uses_local_app_data(tmp_path: Path) -> None:
    result = resolve_standalone_data_root({"LOCALAPPDATA": str(tmp_path)}, os_name="nt")

    assert result == (tmp_path / "ThreadsOperations" / "standalone").resolve(strict=False)


def test_non_windows_without_override_requires_explicit_root() -> None:
    with pytest.raises(StandaloneAccountError, match="^LOCAL_DATA_ROOT_REQUIRED$") as error:
        resolve_standalone_data_root({}, os_name="posix")

    assert error.value.code == "LOCAL_DATA_ROOT_REQUIRED"


def test_windows_without_local_app_data_reports_unavailable() -> None:
    with pytest.raises(StandaloneAccountError, match="^LOCAL_DATA_ROOT_UNAVAILABLE$"):
        resolve_standalone_data_root({}, os_name="nt")


@pytest.mark.parametrize("override", ["", "   "])
def test_empty_explicit_data_root_is_rejected(override: str) -> None:
    with pytest.raises(StandaloneAccountError, match="^LOCAL_DATA_ROOT_INVALID$"):
        resolve_standalone_data_root({THREADS_LOCAL_DATA_ROOT_ENV: override})


@pytest.mark.parametrize("alias", ["a", "x" * 64])
def test_boundary_aliases_are_accepted(tmp_path: Path, alias: str) -> None:
    account = LocalAccountStore(tmp_path).add(alias)

    assert account.alias == alias
    assert account.version == LOCAL_ACCOUNT_SCHEMA_VERSION


@pytest.mark.parametrize(
    "alias",
    ["", "_starts", "-starts", "a.b", "a b", "a/b", "a\\b", "é", "x" * 65],
)
def test_invalid_aliases_are_rejected(tmp_path: Path, alias: str) -> None:
    with pytest.raises(StandaloneAccountError, match="^INVALID_ACCOUNT_ALIAS$"):
        LocalAccountStore(tmp_path).add(alias)


def test_add_writes_exact_account_v1_json(tmp_path: Path) -> None:
    store = LocalAccountStore(tmp_path)

    account = store.add("Alice")
    path = tmp_path / "accounts" / "alice.json"
    document = path.read_text(encoding="utf-8")
    data = json.loads(document)

    assert set(data) == {"version", "id", "alias"}
    assert data == {"version": 1, "id": str(account.id), "alias": "Alice"}
    assert document == json.dumps(data, indent=2, sort_keys=True) + "\n"
    assert UUID(data["id"]) == account.id


def test_uuid_is_stable_across_store_instances(tmp_path: Path) -> None:
    first = LocalAccountStore(tmp_path).add("alice")

    second = LocalAccountStore(tmp_path).get("ALICE")

    assert second == first


def test_alias_uniqueness_is_case_insensitive(tmp_path: Path) -> None:
    store = LocalAccountStore(tmp_path)
    store.add("Alice")

    with pytest.raises(StandaloneAccountError, match="^ACCOUNT_ALREADY_EXISTS$"):
        store.add("alice")


def test_get_missing_account_reports_not_found(tmp_path: Path) -> None:
    with pytest.raises(StandaloneAccountError, match="^ACCOUNT_NOT_FOUND$"):
        LocalAccountStore(tmp_path).get("alice")


@pytest.mark.parametrize(
    ("filename", "document"),
    [
        ("alice.json", "{"),
        (
            "alice.json",
            '{"version":1,"id":"00000000-0000-0000-0000-000000000001",'
            '"alias":"alice","secret":"unexpected"}',
        ),
        (
            "alice.json",
            '{"version":2,"id":"00000000-0000-0000-0000-000000000001","alias":"alice"}',
        ),
        (
            "alice.json",
            '{"version":true,"id":"00000000-0000-0000-0000-000000000001","alias":"alice"}',
        ),
        (
            "alice.json",
            '{"version":1,"id":"not-a-uuid","alias":"alice"}',
        ),
        (
            "alice.json",
            '{"version":1,"id":"00000000-0000-0000-0000-000000000001","alias":"bob"}',
        ),
    ],
)
def test_get_rejects_malformed_or_unsupported_state(
    tmp_path: Path, filename: str, document: str
) -> None:
    accounts = tmp_path / "accounts"
    accounts.mkdir()
    (accounts / filename).write_text(document, encoding="utf-8")

    with pytest.raises(StandaloneAccountError, match="^ACCOUNT_STATE_INVALID$"):
        LocalAccountStore(tmp_path).get("alice")


def test_list_is_sorted_by_casefolded_alias(tmp_path: Path) -> None:
    store = LocalAccountStore(tmp_path)
    store.add("charlie")
    store.add("Bravo")
    store.add("alice")

    accounts = store.list()

    assert isinstance(accounts, tuple)
    assert [account.alias for account in accounts] == ["alice", "Bravo", "charlie"]


def test_list_fails_when_any_account_file_is_corrupt(tmp_path: Path) -> None:
    store = LocalAccountStore(tmp_path)
    store.add("alice")
    (tmp_path / "accounts" / "broken.json").write_text("{", encoding="utf-8")

    with pytest.raises(StandaloneAccountError, match="^ACCOUNT_STATE_INVALID$"):
        store.list()


def test_atomic_write_failure_leaves_no_target_or_temporary_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = LocalAccountStore(tmp_path)

    def fail_replace(source: Path, target: Path) -> None:
        raise OSError("synthetic failure")

    monkeypatch.setattr(account_module.os, "replace", fail_replace)

    with pytest.raises(StandaloneAccountError, match="^ACCOUNT_STATE_INVALID$"):
        store.add("alice")

    accounts = tmp_path / "accounts"
    assert not (accounts / "alice.json").exists()
    assert list(accounts.iterdir()) == []
