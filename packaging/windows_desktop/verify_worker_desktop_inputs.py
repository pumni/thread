from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import stat
import sys
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any, cast

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY_ROOT))
_CONTRACT_SPEC = importlib.util.spec_from_file_location(
    "dx07_worker_package_contract",
    REPOSITORY_ROOT / "packaging" / "windows_worker" / "build_package.py",
)
if _CONTRACT_SPEC is None or _CONTRACT_SPEC.loader is None:
    raise RuntimeError("worker package contract is unavailable")
_CONTRACT = importlib.util.module_from_spec(_CONTRACT_SPEC)
_CONTRACT_SPEC.loader.exec_module(_CONTRACT)
_validate_manifest = _CONTRACT._validate_manifest
package_archive_name = _CONTRACT.package_archive_name
validate_package_tree = _CONTRACT.validate_package_tree

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SOURCE_SHA = re.compile(r"^[0-9a-f]{40}$")


def _is_reparse(path: Path) -> bool:
    try:
        attributes = path.stat(follow_symlinks=False).st_file_attributes
    except AttributeError, FileNotFoundError:
        return path.is_symlink()
    return bool(attributes & 0x400)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_archive_entries(archive: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
    files = archive.infolist()
    seen: set[str] = set()
    prefix = "threads-worker-windows-x64/"
    for item in files:
        name = item.filename
        path = PurePosixPath(name)
        mode = item.external_attr >> 16
        if (
            not name.startswith(prefix)
            or path.is_absolute()
            or any(part in {"", ".", ".."} for part in path.parts)
            or "\\" in name
            or name in seen
            or stat.S_ISLNK(mode)
        ):
            raise ValueError("worker_package_archive_entry_invalid")
        seen.add(name)
    if not files:
        raise ValueError("worker_package_archive_empty")
    return files


def verify_and_extract(
    package_root: Path, release_root: Path, expected_source_sha: str
) -> tuple[Path, dict[str, Any]]:
    if not _SOURCE_SHA.fullmatch(expected_source_sha):
        raise ValueError("worker_package_expected_source_sha_invalid")
    package_root = package_root.resolve(strict=True)
    if (
        not package_root.is_dir()
        or _is_reparse(package_root)
        or any(_is_reparse(item) for item in package_root.iterdir())
    ):
        raise ValueError("worker_package_artifact_root_invalid")
    files = sorted(item.name for item in package_root.iterdir())
    archives = [item for item in files if item.endswith(".zip")]
    if len(archives) != 1:
        raise ValueError("worker_package_archive_missing_or_ambiguous")
    archive_path = package_root / archives[0]
    checksum_path = Path(f"{archive_path}.sha256")
    outer_manifest_path = package_root / "BUILD-MANIFEST.json"
    if set(files) != {archive_path.name, checksum_path.name, outer_manifest_path.name}:
        raise ValueError("worker_package_artifact_files_invalid")
    checksum_match = re.fullmatch(
        r"([0-9a-f]{64})  ([A-Za-z0-9._+-]+\.zip)\n?",
        checksum_path.read_text(encoding="ascii"),
    )
    if checksum_match is None or checksum_match.group(2) != archive_path.name:
        raise ValueError("worker_package_checksum_invalid")
    if not _SHA256.fullmatch(checksum_match.group(1)) or _sha256(
        archive_path
    ) != checksum_match.group(1):
        raise ValueError("worker_package_checksum_mismatch")

    outer_manifest = _validate_manifest(outer_manifest_path)
    if outer_manifest["git_sha"] != expected_source_sha:
        raise ValueError("worker_package_source_sha_mismatch")
    expected_archive_name = package_archive_name(
        outer_manifest["project_version"], outer_manifest["git_sha"]
    )
    if archive_path.name != expected_archive_name:
        raise ValueError("worker_package_manifest_archive_mismatch")
    release_root = release_root.resolve()
    program_files = Path(os.environ.get("ProgramFiles", r"C:\Program Files")).resolve()
    if not release_root.is_relative_to(program_files):
        raise ValueError("worker_package_release_root_invalid")
    cursor = release_root
    while cursor != program_files:
        if _is_reparse(cursor):
            raise ValueError("worker_package_release_root_reparse_point")
        cursor = cursor.parent
    if _is_reparse(program_files):
        raise ValueError("worker_package_release_root_reparse_point")
    release_root.mkdir(parents=True, exist_ok=True)
    release_name = f"windows-x64-{outer_manifest['project_version']}-{outer_manifest['git_sha']}"
    release_path = release_root / release_name
    if release_path.exists():
        raise ValueError("worker_package_release_already_exists")

    try:
        with zipfile.ZipFile(archive_path) as archive:
            entries = _validate_archive_entries(archive)
            release_path.mkdir()
            for entry in entries:
                destination = release_path.joinpath(*PurePosixPath(entry.filename).parts[1:])
                if entry.is_dir():
                    destination.mkdir(parents=True, exist_ok=True)
                    continue
                destination.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(entry) as source, destination.open("xb") as target:
                    import shutil

                    shutil.copyfileobj(source, target)
    except Exception:
        if release_path.exists():
            import shutil

            shutil.rmtree(release_path)
        raise

    inner_manifest_path = release_path / "BUILD-MANIFEST.json"
    inner_manifest = _validate_manifest(inner_manifest_path)
    if inner_manifest != outer_manifest:
        raise ValueError("worker_package_manifest_mismatch")
    validate_package_tree(release_path, require_manifest=True)
    if any(_is_reparse(path) for path in (release_path, *release_path.rglob("*"))):
        raise ValueError("worker_package_release_reparse_point")
    return release_path, cast(dict[str, Any], outer_manifest)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--package-root", type=Path, required=True)
    parser.add_argument("--release-root", type=Path, required=True)
    parser.add_argument("--expected-source-sha", required=True)
    parser.add_argument("--output-json", type=Path, required=True)
    args = parser.parse_args()
    try:
        release, manifest = verify_and_extract(
            args.package_root, args.release_root, args.expected_source_sha
        )
    except ValueError as error:
        code = (
            str(error)
            if str(error)
            in {
                "worker_package_expected_source_sha_invalid",
                "worker_package_source_sha_mismatch",
            }
            else "worker_package_verification_failed"
        )
        print(code)
        return 2
    except OSError, zipfile.BadZipFile, json.JSONDecodeError:
        print("worker_package_verification_failed")
        return 2
    result = {
        "release_directory": str(release),
        "project_version": manifest["project_version"],
        "worker_git_sha": manifest["git_sha"],
        "target_os": manifest["target_os"],
        "target_arch": manifest["target_arch"],
        "browser": manifest["browser"],
    }
    args.output_json.parent.mkdir(parents=True, exist_ok=True)
    args.output_json.write_text(json.dumps(result, sort_keys=True) + "\n", encoding="utf-8")
    print("worker_package_verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
