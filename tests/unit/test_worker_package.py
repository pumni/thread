import contextlib
import importlib.util
import io
import json
import sys
import tomllib
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest

from threads_platform.workers import __main__ as worker_main
from threads_platform.workers import package_check
from threads_platform.workers.package_manifest import (
    ARTIFACT_SCHEMA,
    BuildManifest,
    create_build_manifest,
    package_archive_name,
    serialize_build_manifest,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
_build_module_cache: Any | None = None


def _build_module() -> Any:
    global _build_module_cache
    if _build_module_cache is None:
        path = REPOSITORY_ROOT / "packaging" / "windows_worker" / "build_package.py"
        spec = importlib.util.spec_from_file_location("windows_worker_package_build", path)
        if spec is None or spec.loader is None:
            raise AssertionError("Worker package build script could not be loaded")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        _build_module_cache = module
    return _build_module_cache


def _manifest(*, created_at_utc: datetime | None = None) -> BuildManifest:
    return create_build_manifest(
        project_version="0.1.0",
        git_sha="a" * 40,
        python_version="3.14.7",
        playwright_version="1.63.0",
        pyinstaller_version="6.22.3",
        created_at_utc=created_at_utc or datetime(2026, 9, 30, tzinfo=UTC),
    )


def _runtime_tree(root: Path) -> Path:
    runtime = root / "threads-worker"
    (
        runtime
        / "_internal"
        / "playwright"
        / "driver"
        / "package"
        / ".local-browsers"
        / "chromium-1234"
        / "chrome-win"
    ).mkdir(parents=True)
    (runtime / "threads-worker.exe").write_bytes(b"synthetic executable")
    (runtime / "_internal" / "python314.dll").write_bytes(b"synthetic runtime")
    (
        runtime
        / "_internal"
        / "playwright"
        / "driver"
        / "package"
        / ".local-browsers"
        / "chromium-1234"
        / "chrome-win"
        / "chrome.exe"
    ).write_bytes(b"synthetic chromium")
    return runtime


def test_console_entry_and_metadata_only_version(monkeypatch: pytest.MonkeyPatch) -> None:
    project = tomllib.loads((REPOSITORY_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert project["project"]["scripts"]["threads-worker"] == (
        "threads_platform.workers.__main__:main"
    )

    monkeypatch.setattr(sys, "argv", ["threads-worker", "--version"])
    monkeypatch.setattr(worker_main, "_run", lambda: pytest.fail("normal Worker startup ran"))
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        exit_code = worker_main.main()
    assert exit_code == 0
    assert output.getvalue().strip() == "0.1.0"


def test_package_check_rejects_non_windows_with_a_bounded_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(package_check, "_is_windows", lambda: False)
    output = io.StringIO()
    with contextlib.redirect_stderr(output):
        exit_code = package_check.run_package_check()
    assert exit_code == 2
    assert output.getvalue().strip() == "WORKER_PACKAGE_CHECK_UNSUPPORTED_PLATFORM"


def test_package_check_failure_does_not_print_exception_details(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fail_with_sensitive_detail() -> None:
        raise RuntimeError("C:/private/path Bearer SYNTHETIC_TOKEN")

    monkeypatch.setattr(package_check, "_is_windows", lambda: True)
    monkeypatch.setattr(package_check, "_check_packaged_runtime", fail_with_sensitive_detail)
    output = io.StringIO()
    with contextlib.redirect_stderr(output):
        exit_code = package_check.run_package_check()
    assert exit_code == 1
    assert output.getvalue().strip() == "WORKER_PACKAGE_CHECK_FAILED"
    assert "SYNTHETIC_TOKEN" not in output.getvalue()
    assert "private/path" not in output.getvalue()


def test_build_manifest_is_restricted_and_names_artifact_deterministically() -> None:
    manifest = _manifest()
    assert set(manifest) == {
        "artifact_schema",
        "project_version",
        "git_sha",
        "python_version",
        "playwright_version",
        "pyinstaller_version",
        "target_os",
        "target_arch",
        "browser",
        "created_at_utc",
    }
    assert manifest["artifact_schema"] == ARTIFACT_SCHEMA
    assert json.loads(serialize_build_manifest(manifest)) == manifest
    assert package_archive_name("0.1.0", "a" * 40) == (
        "threads-worker-windows-x64-0.1.0-aaaaaaa.zip"
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("project_version", "../secret"),
        ("git_sha", "not-a-commit"),
        ("python_version", ""),
        ("playwright_version", "latest"),
        ("pyinstaller_version", ""),
    ],
)
def test_manifest_rejects_missing_or_invalid_build_metadata(field: str, value: str) -> None:
    values = {
        "project_version": "0.1.0",
        "git_sha": "a" * 40,
        "python_version": "3.14.7",
        "playwright_version": "1.63.0",
        "pyinstaller_version": "6.22.3",
        "created_at_utc": datetime(2026, 9, 30, tzinfo=UTC),
    }
    values[field] = value
    with pytest.raises(ValueError):
        create_build_manifest(**cast(dict[str, Any], values))


def test_package_staging_keeps_durable_worker_data_external(tmp_path: Path) -> None:
    build = _build_module()
    dist_root = tmp_path / "dist"
    _runtime_tree(dist_root)
    worker_data = tmp_path / "LOCALAPPDATA" / "ThreadsOperations" / "worker"
    worker_data.mkdir(parents=True)
    sentinel = worker_data / "worker_id"
    sentinel.write_text("synthetic-worker-id", encoding="ascii")

    package_root = build.stage_package(dist_root, tmp_path / "staged", _manifest())
    build.validate_package_tree(package_root, require_manifest=True)
    assert sentinel.read_text(encoding="ascii") == "synthetic-worker-id"
    assert not (package_root / "LOCALAPPDATA").exists()
    assert not any(path.name == "worker_id" for path in package_root.rglob("*"))


@pytest.mark.parametrize(
    "forbidden_path",
    [
        Path("_internal/.env"),
        Path("_internal/credentials.json"),
        Path("_internal/tokens.json"),
        Path("_internal/tests/test_fixture.py"),
        Path("_internal/profiles/profile.dat"),
        Path("_internal/journal/worker-state.sqlite3"),
        Path("_internal/worker/device.device-key.dpapi"),
    ],
)
def test_package_staging_rejects_secrets_and_worker_data(
    tmp_path: Path, forbidden_path: Path
) -> None:
    build = _build_module()
    dist_root = tmp_path / "dist"
    _runtime_tree(dist_root)
    bad_file = dist_root / "threads-worker" / forbidden_path
    bad_file.parent.mkdir(parents=True, exist_ok=True)
    bad_file.write_text("synthetic", encoding="ascii")
    with pytest.raises(ValueError):
        build.stage_package(dist_root, tmp_path / "staged", _manifest())


def test_package_staging_rejects_firefox_and_webkit_binaries(tmp_path: Path) -> None:
    build = _build_module()
    dist_root = tmp_path / "dist"
    runtime = _runtime_tree(dist_root)
    browser_root = runtime / "_internal" / "playwright" / "driver" / "package" / ".local-browsers"
    (browser_root / "firefox-1234").mkdir()
    with pytest.raises(ValueError, match="Firefox or WebKit"):
        build.stage_package(dist_root, tmp_path / "staged", _manifest())


def test_archive_order_timestamps_and_digest_are_deterministic(tmp_path: Path) -> None:
    build = _build_module()
    dist_root = tmp_path / "dist"
    _runtime_tree(dist_root)
    package_root = build.stage_package(dist_root, tmp_path / "staged", _manifest())
    (package_root / "threads-worker.exe").touch()

    first_archive, first_digest = build.archive_package(package_root, tmp_path / "release-a")
    second_archive, second_digest = build.archive_package(package_root, tmp_path / "release-b")

    assert first_archive.name == second_archive.name
    assert first_digest == second_digest
    assert (
        (tmp_path / "release-a" / f"{first_archive.name}.sha256")
        .read_text(encoding="ascii")
        .startswith(first_digest)
    )
    with zipfile.ZipFile(first_archive) as archive:
        names = archive.namelist()
        assert names == sorted(names)
        assert all(name.startswith("threads-worker-windows-x64/") for name in names)
        assert all(archive.getinfo(name).date_time == (1980, 1, 1, 0, 0, 0) for name in names)
        assert "threads-worker-windows-x64/BUILD-MANIFEST.json" in names
