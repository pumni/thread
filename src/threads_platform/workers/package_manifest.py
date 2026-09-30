import json
import re
from datetime import UTC, datetime
from typing import TypedDict

ARTIFACT_SCHEMA = "threads-worker-package-v1"
_PROJECT_VERSION_PATTERN = re.compile(r"\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.-]+)?\Z")
_TOOL_VERSION_PATTERN = re.compile(r"\d+\.\d+(?:\.\d+)?(?:[-+][A-Za-z0-9.-]+)?\Z")
_GIT_SHA_PATTERN = re.compile(r"[0-9a-f]{40}\Z")


class BuildManifest(TypedDict):
    artifact_schema: str
    project_version: str
    git_sha: str
    python_version: str
    playwright_version: str
    pyinstaller_version: str
    target_os: str
    target_arch: str
    browser: str
    created_at_utc: str


def create_build_manifest(
    *,
    project_version: str,
    git_sha: str,
    python_version: str,
    playwright_version: str,
    pyinstaller_version: str,
    created_at_utc: datetime,
) -> BuildManifest:
    if not _PROJECT_VERSION_PATTERN.fullmatch(project_version):
        raise ValueError("project version is missing or invalid")
    if not _GIT_SHA_PATTERN.fullmatch(git_sha):
        raise ValueError("Git commit metadata is missing or invalid")
    for name, value in (
        ("Python", python_version),
        ("Playwright", playwright_version),
        ("PyInstaller", pyinstaller_version),
    ):
        if not _TOOL_VERSION_PATTERN.fullmatch(value):
            raise ValueError(f"{name} version metadata is missing or invalid")
    offset = created_at_utc.utcoffset()
    if created_at_utc.tzinfo is None or offset is None:
        raise ValueError("build timestamp must be timezone-aware")
    if offset.total_seconds() != 0:
        raise ValueError("build timestamp must be UTC")

    return {
        "artifact_schema": ARTIFACT_SCHEMA,
        "project_version": project_version,
        "git_sha": git_sha,
        "python_version": python_version,
        "playwright_version": playwright_version,
        "pyinstaller_version": pyinstaller_version,
        "target_os": "windows",
        "target_arch": "x64",
        "browser": "chromium",
        "created_at_utc": created_at_utc.astimezone(UTC).isoformat().replace("+00:00", "Z"),
    }


def serialize_build_manifest(manifest: BuildManifest) -> str:
    if set(manifest) != set(BuildManifest.__annotations__):
        raise ValueError("build manifest contains unexpected fields")
    return json.dumps(manifest, indent=2, sort_keys=True) + "\n"


def package_archive_name(project_version: str, git_sha: str) -> str:
    if not _PROJECT_VERSION_PATTERN.fullmatch(project_version):
        raise ValueError("project version is missing or invalid")
    if not _GIT_SHA_PATTERN.fullmatch(git_sha):
        raise ValueError("Git commit metadata is missing or invalid")
    return f"threads-worker-windows-x64-{project_version}-{git_sha[:7]}.zip"
