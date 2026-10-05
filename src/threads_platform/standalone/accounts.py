"""Local, non-secret account metadata for standalone execution."""

from __future__ import annotations

import json
import os
import re
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import cast
from uuid import UUID, uuid4

LOCAL_ACCOUNT_SCHEMA_VERSION = 1
THREADS_LOCAL_DATA_ROOT_ENV = "THREADS_LOCAL_DATA_ROOT"

_ERROR_CODES = frozenset(
    {
        "LOCAL_DATA_ROOT_INVALID",
        "LOCAL_DATA_ROOT_UNAVAILABLE",
        "LOCAL_DATA_ROOT_REQUIRED",
        "INVALID_ACCOUNT_ALIAS",
        "ACCOUNT_ALREADY_EXISTS",
        "ACCOUNT_NOT_FOUND",
        "ACCOUNT_STATE_INVALID",
    }
)
_ALIAS_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
_ACCOUNT_KEYS = {"version", "id", "alias"}


@dataclass(frozen=True, slots=True)
class LocalAccount:
    version: int
    id: UUID
    alias: str


class StandaloneAccountError(Exception):
    code: str

    def __init__(self, code: str) -> None:
        if code not in _ERROR_CODES:
            raise ValueError("unsupported standalone account error code")
        self.code = code
        super().__init__(code)


def resolve_standalone_data_root(
    environment: Mapping[str, str] | None = None,
    *,
    os_name: str | None = None,
) -> Path:
    values = os.environ if environment is None else environment
    override = values.get(THREADS_LOCAL_DATA_ROOT_ENV)
    if override is not None:
        if not override.strip():
            raise StandaloneAccountError("LOCAL_DATA_ROOT_INVALID")
        try:
            return Path(override).expanduser().resolve(strict=False)
        except OSError, RuntimeError:
            raise StandaloneAccountError("LOCAL_DATA_ROOT_INVALID") from None

    if (os_name or os.name) == "nt":
        local_app_data = values.get("LOCALAPPDATA")
        if local_app_data is None or not local_app_data.strip():
            raise StandaloneAccountError("LOCAL_DATA_ROOT_UNAVAILABLE")
        try:
            return (Path(local_app_data) / "ThreadsOperations" / "standalone").resolve(strict=False)
        except OSError, RuntimeError:
            raise StandaloneAccountError("LOCAL_DATA_ROOT_INVALID") from None

    raise StandaloneAccountError("LOCAL_DATA_ROOT_REQUIRED")


def _validate_alias(alias: str) -> None:
    if _ALIAS_PATTERN.fullmatch(alias) is None:
        raise StandaloneAccountError("INVALID_ACCOUNT_ALIAS")


class LocalAccountStore:
    def __init__(self, data_root: Path) -> None:
        self._data_root = data_root
        self._accounts_directory = data_root / "accounts"

    def add(self, alias: str) -> LocalAccount:
        _validate_alias(alias)
        self._ensure_accounts_directory()
        target = self._account_path(alias)
        if self._path_exists(target):
            raise StandaloneAccountError("ACCOUNT_ALREADY_EXISTS")

        account = LocalAccount(
            version=LOCAL_ACCOUNT_SCHEMA_VERSION,
            id=uuid4(),
            alias=alias,
        )
        document = (
            json.dumps(
                {"version": account.version, "id": str(account.id), "alias": account.alias},
                indent=2,
                sort_keys=True,
            )
            + "\n"
        )
        self._atomic_write(target, document)
        return account

    def get(self, alias: str) -> LocalAccount:
        _validate_alias(alias)
        if not self._accounts_directory_exists():
            raise StandaloneAccountError("ACCOUNT_NOT_FOUND")
        target = self._account_path(alias)
        if not self._path_exists(target):
            raise StandaloneAccountError("ACCOUNT_NOT_FOUND")
        return self._read_account(target)

    def list(self) -> tuple[LocalAccount, ...]:
        if not self._accounts_directory_exists():
            return ()
        try:
            paths = tuple(self._accounts_directory.glob("*.json"))
        except OSError:
            raise StandaloneAccountError("ACCOUNT_STATE_INVALID") from None
        accounts = tuple(self._read_account(path) for path in paths)
        return tuple(
            sorted(accounts, key=lambda account: (account.alias.casefold(), str(account.id)))
        )

    def _account_path(self, alias: str) -> Path:
        return self._accounts_directory / f"{alias.casefold()}.json"

    def _ensure_accounts_directory(self) -> None:
        try:
            self._accounts_directory.mkdir(parents=True, exist_ok=True)
        except OSError:
            raise StandaloneAccountError("ACCOUNT_STATE_INVALID") from None
        self._validate_accounts_directory()

    def _accounts_directory_exists(self) -> bool:
        try:
            self._accounts_directory.lstat()
        except FileNotFoundError:
            return False
        except OSError:
            raise StandaloneAccountError("ACCOUNT_STATE_INVALID") from None
        self._validate_accounts_directory()
        try:
            is_directory = self._accounts_directory.is_dir()
        except OSError:
            raise StandaloneAccountError("ACCOUNT_STATE_INVALID") from None
        if not is_directory:
            raise StandaloneAccountError("ACCOUNT_STATE_INVALID")
        return True

    def _validate_accounts_directory(self) -> None:
        try:
            root = self._data_root.resolve(strict=False)
            accounts_directory = self._accounts_directory.resolve(strict=False)
        except OSError, RuntimeError:
            raise StandaloneAccountError("ACCOUNT_STATE_INVALID") from None
        if accounts_directory != root / "accounts":
            raise StandaloneAccountError("ACCOUNT_STATE_INVALID")

    def _validate_account_path(self, path: Path) -> None:
        try:
            root = self._data_root.resolve(strict=False)
            resolved_path = path.resolve(strict=True)
            resolved_path.relative_to(root)
        except OSError, RuntimeError, ValueError:
            raise StandaloneAccountError("ACCOUNT_STATE_INVALID") from None

    @staticmethod
    def _path_exists(path: Path) -> bool:
        try:
            path.lstat()
        except FileNotFoundError:
            return False
        except OSError:
            raise StandaloneAccountError("ACCOUNT_STATE_INVALID") from None
        return True

    def _read_account(self, path: Path) -> LocalAccount:
        self._validate_account_path(path)
        try:
            document = path.read_text(encoding="utf-8")
            data: object = json.loads(document)
        except OSError, UnicodeError, json.JSONDecodeError, RecursionError:
            raise StandaloneAccountError("ACCOUNT_STATE_INVALID") from None

        if not isinstance(data, dict):
            raise StandaloneAccountError("ACCOUNT_STATE_INVALID")
        account_data = cast(dict[str, object], data)
        if set(account_data) != _ACCOUNT_KEYS:
            raise StandaloneAccountError("ACCOUNT_STATE_INVALID")
        version = account_data["version"]
        raw_id = account_data["id"]
        alias = account_data["alias"]
        if type(version) is not int or version != LOCAL_ACCOUNT_SCHEMA_VERSION:
            raise StandaloneAccountError("ACCOUNT_STATE_INVALID")
        if not isinstance(raw_id, str) or not isinstance(alias, str):
            raise StandaloneAccountError("ACCOUNT_STATE_INVALID")
        try:
            account_id = UUID(raw_id)
        except ValueError, AttributeError:
            raise StandaloneAccountError("ACCOUNT_STATE_INVALID") from None
        try:
            _validate_alias(alias)
        except StandaloneAccountError:
            raise StandaloneAccountError("ACCOUNT_STATE_INVALID") from None
        if path.name != f"{alias.casefold()}.json":
            raise StandaloneAccountError("ACCOUNT_STATE_INVALID")
        return LocalAccount(version=version, id=account_id, alias=alias)

    def _atomic_write(self, target: Path, document: str) -> None:
        temporary_path: Path | None = None
        descriptor: int | None = None
        try:
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{target.name}.",
                suffix=".tmp",
                dir=self._accounts_directory,
            )
            temporary_path = Path(temporary_name)
            stream = os.fdopen(descriptor, "w", encoding="utf-8", newline="\n")
            descriptor = None
            with stream:
                stream.write(document)
                stream.flush()
                os.fsync(stream.fileno())

            if self._path_exists(target):
                raise StandaloneAccountError("ACCOUNT_ALREADY_EXISTS")
            try:
                os.replace(temporary_path, target)
            except FileExistsError:
                if self._path_exists(target):
                    raise StandaloneAccountError("ACCOUNT_ALREADY_EXISTS") from None
                raise
        except StandaloneAccountError:
            raise
        except OSError:
            raise StandaloneAccountError("ACCOUNT_STATE_INVALID") from None
        finally:
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            if temporary_path is not None:
                try:
                    temporary_path.unlink(missing_ok=True)
                except OSError:
                    pass
