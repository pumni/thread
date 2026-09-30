from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import platform
import re
import shutil
import subprocess
import sys
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from threads_platform.workers.package_manifest import (
    BuildManifest,
    create_build_manifest,
    package_archive_name,
    serialize_build_manifest,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_DIRECTORY_NAME = "threads-worker-windows-x64"
RUNTIME_DIRECTORY_NAME = "threads-worker"
_GIT_SHA_PATTERN = re.compile(r"[0-9a-f]{40}\Z")
_MANIFEST_FIELDS = {
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
_FORBIDDEN_PARTS = {".git", "tests", "profiles", "journal"}
_FORBIDDEN_NAMES = {
    ".env",
    "credentials",
    "credentials.json",
    "secrets.json",
    "token",
    "token.json",
    "tokens.json",
    "worker_id",
    "worker-state.sqlite3",
}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)

    stage = commands.add_parser("stage")
    stage.add_argument("--dist-root", type=Path, required=True)
    stage.add_argument("--output-root", type=Path, required=True)

    archive = commands.add_parser("archive")
    archive.add_argument("--package-root", type=Path, required=True)
    archive.add_argument("--output-root", type=Path, required=True)
    return parser


def main() -> int:
    arguments = _parser().parse_args()
    if arguments.command == "stage":
        if not _is_windows_x64():
            raise RuntimeError("Windows x64 packaging must run on Windows x64")
        manifest = _current_manifest()
        package_root = stage_package(arguments.dist_root, arguments.output_root, manifest)
        validate_package_tree(package_root, require_manifest=True)
        print(f"STAGED {package_root.name} {manifest['project_version']} {manifest['git_sha'][:7]}")
        return 0

    if arguments.command == "archive":
        if not _is_windows_x64():
            raise RuntimeError("Windows x64 packaging must run on Windows x64")
        archive_path, digest = archive_package(arguments.package_root, arguments.output_root)
        print(f"PACKAGE {archive_path.name}")
        print(f"SHA-256 {digest}")
        return 0

    raise RuntimeError("unsupported packaging operation")


def _is_windows_x64() -> bool:
    return sys.platform == "win32" and platform.machine().casefold() in {"amd64", "x86_64"}


def _current_manifest() -> BuildManifest:
    git_sha = _git_sha()
    try:
        return create_build_manifest(
            project_version=importlib.metadata.version("threads-platform"),
            git_sha=git_sha,
            python_version=platform.python_version(),
            playwright_version=importlib.metadata.version("playwright"),
            pyinstaller_version=importlib.metadata.version("pyinstaller"),
            created_at_utc=datetime.now(UTC),
        )
    except importlib.metadata.PackageNotFoundError as error:
        raise RuntimeError("required package version metadata is unavailable") from error


def _git_sha() -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--verify", "HEAD"],
            check=True,
            capture_output=True,
            cwd=REPOSITORY_ROOT,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as error:
        raise RuntimeError("Git commit metadata is unavailable") from error
    git_sha = result.stdout.strip()
    if not _GIT_SHA_PATTERN.fullmatch(git_sha):
        raise RuntimeError("Git commit metadata is invalid")
    return git_sha


def stage_package(dist_root: Path, output_root: Path, manifest: BuildManifest) -> Path:
    source = dist_root / RUNTIME_DIRECTORY_NAME
    validate_package_tree(source, require_manifest=False)
    package_root = output_root / PACKAGE_DIRECTORY_NAME
    if package_root.exists():
        raise FileExistsError("package staging directory already exists")
    output_root.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, package_root)
    (package_root / "BUILD-MANIFEST.json").write_text(
        serialize_build_manifest(manifest), encoding="utf-8", newline="\n"
    )
    return package_root


def validate_package_tree(package_root: Path, *, require_manifest: bool) -> None:
    if not package_root.is_dir():
        raise ValueError("package directory is missing")
    required = {"threads-worker.exe", "_internal"}
    if require_manifest:
        required.add("BUILD-MANIFEST.json")
    if {path.name for path in package_root.iterdir()} != required:
        raise ValueError("package contains unexpected or missing top-level files")
    if not (package_root / "threads-worker.exe").is_file():
        raise ValueError("Worker executable is missing")
    if not (package_root / "_internal").is_dir():
        raise ValueError("PyInstaller runtime directory is missing")

    local_browsers = list(package_root.rglob(".local-browsers"))
    if len(local_browsers) != 1 or not local_browsers[0].is_dir():
        raise ValueError("bundled Playwright browser directory is missing or ambiguous")
    browser_names = {entry.name.casefold() for entry in local_browsers[0].iterdir()}
    if not any(name.startswith("chromium-") for name in browser_names):
        raise ValueError("matching Chromium browser is missing")
    if any(name.startswith(("firefox-", "webkit-")) for name in browser_names):
        raise ValueError("unexpected Firefox or WebKit browser is bundled")

    for path in package_root.rglob("*"):
        if path.is_symlink():
            raise ValueError("package must not contain symbolic links")
        relative_parts = path.relative_to(package_root).parts
        if any(part.casefold() in _FORBIDDEN_PARTS for part in relative_parts):
            raise ValueError("package contains repository tests or Worker durable data")
        name = path.name.casefold()
        if (
            name in _FORBIDDEN_NAMES
            or name.startswith(".env.")
            or name.endswith((".device-key.dpapi", ".sqlite", ".sqlite3", ".p12", ".pfx"))
        ):
            raise ValueError("package contains a forbidden secret or Worker data file")

    if require_manifest:
        _validate_manifest(package_root / "BUILD-MANIFEST.json")


def _validate_manifest(path: Path) -> BuildManifest:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError("build manifest is missing or invalid") from error
    if not isinstance(raw, dict) or set(raw) != _MANIFEST_FIELDS:
        raise ValueError("build manifest contains unexpected or missing fields")
    if (
        raw.get("artifact_schema") != "threads-worker-package-v1"
        or raw.get("target_os") != "windows"
        or raw.get("target_arch") != "x64"
        or raw.get("browser") != "chromium"
    ):
        raise ValueError("build manifest does not match the Windows x64 package")
    manifest = cast(BuildManifest, raw)
    package_archive_name(manifest["project_version"], manifest["git_sha"])
    return manifest


def archive_package(package_root: Path, output_root: Path) -> tuple[Path, str]:
    validate_package_tree(package_root, require_manifest=True)
    manifest = _validate_manifest(package_root / "BUILD-MANIFEST.json")
    archive_path = output_root / package_archive_name(
        manifest["project_version"], manifest["git_sha"]
    )
    checksum_path = archive_path.with_suffix(archive_path.suffix + ".sha256")
    manifest_copy = output_root / "BUILD-MANIFEST.json"
    if archive_path.exists() or checksum_path.exists() or manifest_copy.exists():
        raise FileExistsError("package output already exists")
    output_root.mkdir(parents=True, exist_ok=True)

    _write_deterministic_zip(package_root, archive_path)
    digest = _sha256(archive_path)
    checksum_path.write_text(f"{digest}  {archive_path.name}\n", encoding="ascii")
    shutil.copyfile(package_root / "BUILD-MANIFEST.json", manifest_copy)
    return archive_path, digest


def _write_deterministic_zip(package_root: Path, archive_path: Path) -> None:
    files = sorted(
        (path for path in package_root.rglob("*") if path.is_file()),
        key=lambda path: path.relative_to(package_root).as_posix(),
    )
    with zipfile.ZipFile(
        archive_path, mode="x", compression=zipfile.ZIP_DEFLATED, compresslevel=9
    ) as archive:
        for path in files:
            relative = path.relative_to(package_root).as_posix()
            info = zipfile.ZipInfo(f"{PACKAGE_DIRECTORY_NAME}/{relative}")
            info.date_time = (1980, 1, 1, 0, 0, 0)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 0
            info.external_attr = 0
            archive.writestr(info, path.read_bytes(), compresslevel=9)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


if __name__ == "__main__":
    raise SystemExit(main())
