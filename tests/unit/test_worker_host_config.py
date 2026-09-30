import contextlib
import io
import json
import sys
from pathlib import Path

import pytest

from threads_platform.workers import __main__ as worker_main
from threads_platform.workers.host_config import (
    HOST_CONFIG_SCHEMA,
    WorkerHostConfig,
    WorkerHostConfigError,
    parse_worker_host_config,
    read_worker_host_config,
)


def _document(**overrides: object) -> dict[str, object]:
    return {
        "schema": HOST_CONFIG_SCHEMA,
        "control_plane_url": "https://control.example.test",
        **overrides,
    }


@pytest.mark.parametrize(
    "field",
    [
        "enrollment_code",
        "access_token",
        "private_key",
        "password",
        "proxy_credential",
        "arbitrary_extension",
    ],
)
def test_host_config_rejects_secret_like_and_unknown_fields(field: str) -> None:
    with pytest.raises(WorkerHostConfigError, match="host config is invalid") as raised:
        parse_worker_host_config(_document(**{field: "SYNTHETIC_SECRET"}))
    assert "SYNTHETIC_SECRET" not in str(raised.value)
    assert field not in str(raised.value)


@pytest.mark.parametrize(
    "control_plane_url",
    [
        "http://control.example.test",
        "https://control.example.test?token=SYNTHETIC_SECRET",
        "https://control.example.test#fragment",
        "https://[invalid",
    ],
)
def test_host_config_preserves_control_plane_url_security_boundary(
    control_plane_url: str,
) -> None:
    with pytest.raises(WorkerHostConfigError, match="host config is invalid"):
        parse_worker_host_config(_document(control_plane_url=control_plane_url))


def test_host_config_rejects_embedded_url_credentials() -> None:
    control_plane_url = "https://user:" + "password" + "@control.example.test"
    with pytest.raises(WorkerHostConfigError, match="host config is invalid"):
        parse_worker_host_config(_document(control_plane_url=control_plane_url))


@pytest.mark.parametrize(
    "data_root",
    [
        "relative\\worker-data",
        "C:\\worker-data\nattacker",
        "x" * 1_025,
    ],
)
def test_host_config_rejects_relative_or_unbounded_data_root(data_root: str) -> None:
    with pytest.raises(WorkerHostConfigError, match="host config is invalid"):
        parse_worker_host_config(_document(data_root=data_root))


def test_host_config_accepts_absolute_roots_and_bounded_values(tmp_path: Path) -> None:
    config = parse_worker_host_config(
        _document(
            data_root=str(tmp_path / "worker-data"),
            display_name="Windows Worker A",
            agent_version="0.1.0",
            max_concurrent_jobs=2,
            max_browser_sessions=3,
            feed_browse_enabled=True,
            thread_open_enabled=False,
            profile_open_enabled=True,
            media_local_upload_enabled=False,
        )
    )
    assert config.control_plane_url == "https://control.example.test"
    assert config.data_root == str(tmp_path / "worker-data")
    assert config.display_name == "Windows Worker A"
    assert config.agent_version == "0.1.0"
    assert config.max_concurrent_jobs == 2
    assert config.max_browser_sessions == 3
    assert config.feed_browse_enabled is True
    assert config.thread_open_enabled is False


def test_host_config_accepts_absolute_windows_data_root() -> None:
    config = parse_worker_host_config(_document(data_root="C:\\ThreadsOperations"))
    assert config.data_root == "C:\\ThreadsOperations"


@pytest.mark.parametrize(
    "document",
    [
        [],
        {"schema": "wrong"},
        _document(max_concurrent_jobs=True),
        _document(max_browser_sessions=0),
        _document(feed_browse_enabled="true"),
        _document(display_name="bad\nname"),
        _document(capabilities={"arbitrary": True}),
    ],
)
def test_host_config_rejects_bad_schema_types_and_unknown_nested_fields(
    document: object,
) -> None:
    with pytest.raises(WorkerHostConfigError, match="host config is invalid"):
        parse_worker_host_config(document)


def test_host_config_reader_rejects_duplicate_keys_and_relative_file(tmp_path: Path) -> None:
    config_path = tmp_path / "host.json"
    config_path.write_text(
        '{"schema":"threads-worker-host-v1","schema":"threads-worker-host-v1"}',
        encoding="utf-8",
    )
    with pytest.raises(WorkerHostConfigError, match="host config is invalid"):
        read_worker_host_config(config_path)
    with pytest.raises(WorkerHostConfigError, match="host config is invalid"):
        read_worker_host_config(Path("relative.json"))


def test_host_config_reader_rejects_oversized_file(tmp_path: Path) -> None:
    config_path = tmp_path / "host.json"
    config_path.write_bytes(b" " * 16_385)
    with pytest.raises(WorkerHostConfigError, match="host config is invalid"):
        read_worker_host_config(config_path)


def test_config_repr_omits_values_and_filesystem_paths(tmp_path: Path) -> None:
    private_path = str(tmp_path / "private-worker-root")
    config = parse_worker_host_config(_document(data_root=private_path))
    rendered = repr(config)
    assert "private-worker-root" not in rendered
    assert "control.example.test" not in rendered


def test_cli_validation_prints_only_a_bounded_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "worker-host.json"
    config_path.write_text(json.dumps(_document()), encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["threads-worker", "--validate-host-config", str(config_path)])
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        assert worker_main.main() == 0
    assert output.getvalue().strip() == "THREADS_WORKER_HOST_CONFIG_VALID"
    assert str(config_path) not in output.getvalue()


def test_cli_version_ignores_environment_host_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "argv", ["threads-worker", "--version"])
    monkeypatch.setenv("THREADS_WORKER_HOST_CONFIG", "relative-and-invalid.json")

    def fail_if_run(host_config: WorkerHostConfig | None = None) -> None:
        pytest.fail("Worker started")

    monkeypatch.setattr(worker_main, "_run", fail_if_run)
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        assert worker_main.main() == 0
    assert output.getvalue().strip() == "0.1.0"


def test_package_check_ignores_environment_host_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "argv", ["threads-worker", "--package-check"])
    monkeypatch.setenv("THREADS_WORKER_HOST_CONFIG", "invalid.json")
    monkeypatch.setattr(worker_main, "run_package_check", lambda: 0)
    assert worker_main.main() == 0


def test_host_config_values_override_environment_deterministically(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = parse_worker_host_config(
        _document(
            control_plane_url="https://config.example.test",
            data_root="C:\\ConfiguredData",
            display_name="Configured worker",
            max_concurrent_jobs=2,
            feed_browse_enabled=False,
        )
    )
    monkeypatch.setenv("THREADS_WORKER_CONTROL_PLANE_URL", "https://environment.example.test")
    monkeypatch.setenv("THREADS_WORKER_DISPLAY_NAME", "Environment worker")
    monkeypatch.setenv("THREADS_WORKER_MAX_CONCURRENT_JOBS", "4")
    monkeypatch.setenv("THREADS_WORKER_DATA_ROOT", "C:\\EnvironmentData")
    monkeypatch.setenv("THREADS_WORKER_FEED_BROWSE_ENABLED", "true")
    monkeypatch.setenv("THREADS_WORKER_THREAD_OPEN_ENABLED", "true")
    assert (
        worker_main.configured_value(config.control_plane_url, "THREADS_WORKER_CONTROL_PLANE_URL")
        == "https://config.example.test"
    )
    assert (
        worker_main.configured_value(None, "THREADS_WORKER_DISPLAY_NAME", "Windows Worker")
        == "Environment worker"
    )
    assert (
        worker_main.configured_integer(
            config.max_concurrent_jobs, "THREADS_WORKER_MAX_CONCURRENT_JOBS"
        )
        == 2
    )
    assert worker_main.configured_integer(None, "THREADS_WORKER_MAX_CONCURRENT_JOBS") == 4
    assert (
        worker_main.configured_optional_value(config.data_root, "THREADS_WORKER_DATA_ROOT")
        == config.data_root
    )
    assert (
        worker_main.configured_optional_value(None, "THREADS_WORKER_DATA_ROOT")
        == "C:\\EnvironmentData"
    )
    monkeypatch.delenv("THREADS_WORKER_DATA_ROOT")
    assert worker_main.configured_optional_value(None, "THREADS_WORKER_DATA_ROOT") is None
    capabilities = set(worker_main.enabled_browser_capabilities(config))
    assert ("threads.browser.feed.browse", 1) not in capabilities
    assert ("threads.browser.thread.open", 1) in capabilities


def test_enrollment_code_remains_environment_only() -> None:
    with pytest.raises(WorkerHostConfigError):
        parse_worker_host_config(_document(enrollment_code="SYNTHETIC_ENROLLMENT"))
    assert not hasattr(WorkerHostConfig, "enrollment_code")
