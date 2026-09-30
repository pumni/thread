import json
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Any, cast
from urllib.parse import urlsplit

HOST_CONFIG_SCHEMA = "threads-worker-host-v1"
_MAX_CONFIG_BYTES = 16_384
_MAX_CONFIG_PATH_LENGTH = 2_048
_MAX_DATA_ROOT_LENGTH = 1_024
_ALLOWED_FIELDS = {
    "schema",
    "control_plane_url",
    "data_root",
    "display_name",
    "agent_version",
    "max_concurrent_jobs",
    "max_browser_sessions",
    "feed_browse_enabled",
    "thread_open_enabled",
    "profile_open_enabled",
    "media_local_upload_enabled",
}


class WorkerHostConfigError(ValueError):
    pass


@dataclass(frozen=True, slots=True, repr=False)
class WorkerHostConfig:
    control_plane_url: str | None = None
    data_root: str | None = None
    display_name: str | None = None
    agent_version: str | None = None
    max_concurrent_jobs: int | None = None
    max_browser_sessions: int | None = None
    feed_browse_enabled: bool | None = None
    thread_open_enabled: bool | None = None
    profile_open_enabled: bool | None = None
    media_local_upload_enabled: bool | None = None

    def __repr__(self) -> str:
        return "WorkerHostConfig(<validated deployment values omitted>)"


def read_worker_host_config(path: Path) -> WorkerHostConfig:
    path_text = str(path)
    if (
        len(path_text) > _MAX_CONFIG_PATH_LENGTH
        or not _is_absolute_path(path_text)
        or _has_control_characters(path_text)
    ):
        raise WorkerHostConfigError("host config is invalid")
    try:
        with path.open("rb") as stream:
            raw = stream.read(_MAX_CONFIG_BYTES + 1)
        if len(raw) > _MAX_CONFIG_BYTES:
            raise WorkerHostConfigError("host config is invalid")
        text = raw.decode("utf-8")
        document = json.loads(text, object_pairs_hook=_unique_object)
    except WorkerHostConfigError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise WorkerHostConfigError("host config is invalid") from error
    return parse_worker_host_config(document)


def parse_worker_host_config(document: Any) -> WorkerHostConfig:
    if not isinstance(document, dict):
        raise WorkerHostConfigError("host config is invalid")
    typed_document = cast(dict[str, Any], document)
    if typed_document.get("schema") != HOST_CONFIG_SCHEMA:
        raise WorkerHostConfigError("host config is invalid")
    if set(typed_document) - _ALLOWED_FIELDS:
        raise WorkerHostConfigError("host config is invalid")

    control_plane_url = _optional_string(typed_document, "control_plane_url", 2_048)
    if control_plane_url is not None:
        _validate_control_plane_url(control_plane_url)

    data_root = _optional_string(typed_document, "data_root", _MAX_DATA_ROOT_LENGTH)
    if data_root is not None and not _is_absolute_path(data_root):
        raise WorkerHostConfigError("host config is invalid")

    display_name = _optional_string(typed_document, "display_name", 255)
    agent_version = _optional_string(typed_document, "agent_version", 80)
    max_concurrent_jobs = _optional_capacity(typed_document, "max_concurrent_jobs")
    max_browser_sessions = _optional_capacity(typed_document, "max_browser_sessions")
    capabilities = {
        name: _optional_boolean(typed_document, name)
        for name in (
            "feed_browse_enabled",
            "thread_open_enabled",
            "profile_open_enabled",
            "media_local_upload_enabled",
        )
    }
    return WorkerHostConfig(
        control_plane_url=control_plane_url,
        data_root=data_root,
        display_name=display_name,
        agent_version=agent_version,
        max_concurrent_jobs=max_concurrent_jobs,
        max_browser_sessions=max_browser_sessions,
        **capabilities,
    )


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise WorkerHostConfigError("host config is invalid")
        result[key] = value
    return result


def _optional_string(document: dict[str, Any], name: str, maximum: int) -> str | None:
    if name not in document:
        return None
    value = document[name]
    if (
        not isinstance(value, str)
        or not value
        or len(value) > maximum
        or value != value.strip()
        or _has_control_characters(value)
    ):
        raise WorkerHostConfigError("host config is invalid")
    return value


def _optional_capacity(document: dict[str, Any], name: str) -> int | None:
    if name not in document:
        return None
    value = document[name]
    if type(value) is not int or not 1 <= value <= 1_000:
        raise WorkerHostConfigError("host config is invalid")
    return value


def _optional_boolean(document: dict[str, Any], name: str) -> bool | None:
    if name not in document:
        return None
    value = document[name]
    if type(value) is not bool:
        raise WorkerHostConfigError("host config is invalid")
    return value


def _validate_control_plane_url(value: str) -> None:
    try:
        parsed = urlsplit(value)
        valid = (
            parsed.scheme.casefold() == "https"
            and bool(parsed.hostname)
            and parsed.username is None
            and parsed.password is None
            and not parsed.query
            and not parsed.fragment
        )
    except ValueError:
        valid = False
    if not valid:
        raise WorkerHostConfigError("host config is invalid")


def _is_absolute_path(value: str) -> bool:
    return Path(value).is_absolute() or PureWindowsPath(value).is_absolute()


def _has_control_characters(value: str) -> bool:
    return any(ord(character) < 32 or ord(character) == 127 for character in value)
