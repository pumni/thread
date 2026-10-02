from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any

METADATA_FILES = {"runtime-manifest.json", "runtime-manifest.json.sha256"}
LAYOUTS = ("shared", "split")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_json(path: Path) -> dict[str, Any]:
    value: Any = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise SystemExit(f"Expected a JSON object: {path}")
    return value


def candidate_tree_digest(candidate: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    total_bytes = 0
    files = sorted(
        path for path in candidate.rglob("*") if path.is_file() and path.name not in METADATA_FILES
    )
    for path in files:
        relative = path.relative_to(candidate).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        digest.update(bytes.fromhex(sha256_file(path)))
        total_bytes += path.stat().st_size
    return digest.hexdigest(), total_bytes


def verify_candidate(
    candidate: Path,
    *,
    layout: str,
    expected_revision: str,
    expected_lock_sha256: str,
    expected_postgres_sha256: str,
) -> dict[str, Any]:
    manifest_path = candidate / "runtime-manifest.json"
    checksum_path = candidate / "runtime-manifest.json.sha256"
    if not manifest_path.is_file() or not checksum_path.is_file():
        raise SystemExit(f"Runtime manifest/checksum missing: {candidate}")
    checksum_fields = checksum_path.read_text(encoding="ascii").split()
    if (
        len(checksum_fields) != 2
        or checksum_fields[1] != "runtime-manifest.json"
        or checksum_fields[0] != sha256_file(manifest_path)
    ):
        raise SystemExit(f"Runtime manifest checksum mismatch: {candidate}")

    manifest = read_json(manifest_path)
    if manifest.get("source_revision") != expected_revision:
        raise SystemExit(f"Runtime source revision mismatch: {candidate}")
    if manifest.get("source_tree_dirty") is not False:
        raise SystemExit(f"Runtime was built from a dirty source tree: {candidate}")
    if manifest.get("layout") != layout:
        raise SystemExit(f"Runtime layout mismatch: {candidate}")
    if manifest.get("project_lock_sha256") != expected_lock_sha256:
        raise SystemExit(f"uv.lock checksum mismatch: {candidate}")
    postgres = manifest.get("postgresql")
    if not isinstance(postgres, dict) or postgres.get("archive_sha256") != expected_postgres_sha256:
        raise SystemExit(f"PostgreSQL archive checksum mismatch: {candidate}")
    if postgres.get("version") != "17.11":
        raise SystemExit(f"Unexpected PostgreSQL runtime version: {candidate}")
    if (
        manifest.get("database_data_included") is not False
        or manifest.get("secrets_included") is not False
    ):
        raise SystemExit(f"Runtime manifest must exclude data and secrets: {candidate}")
    if not str(manifest.get("python", "")).startswith("Python 3.14."):
        raise SystemExit(f"Runtime Python version is not the pinned 3.14 line: {candidate}")
    if manifest.get("pyinstaller") != "6.22.3":
        raise SystemExit(f"Unexpected PyInstaller version: {candidate}")

    executables = manifest.get("executables")
    if not isinstance(executables, list) or not executables:
        raise SystemExit(f"Runtime executable inventory is empty: {candidate}")
    for executable in executables:
        if not isinstance(executable, dict) or not isinstance(executable.get("name"), str):
            raise SystemExit(f"Invalid executable inventory: {candidate}")
        executable_path = candidate / executable["name"] / f"{executable['name']}.exe"
        if (
            not executable_path.is_file()
            or executable.get("executable_bytes") != executable_path.stat().st_size
            or executable.get("executable_sha256") != sha256_file(executable_path)
        ):
            raise SystemExit(f"Runtime executable checksum mismatch: {executable_path}")

    for name in ("initdb.exe", "pg_ctl.exe", "postgres.exe", "psql.exe"):
        if not (candidate / "postgresql" / "bin" / name).is_file():
            raise SystemExit(f"Bundled PostgreSQL executable missing: {name}")
    if not (candidate / "THIRD-PARTY-NOTICES.md").is_file():
        raise SystemExit(f"Third-party notices missing: {candidate}")
    postgres_license_count = sum(
        path.is_file() for path in (candidate / "licenses" / "postgresql").rglob("*")
    )
    if postgres_license_count == 0 or postgres.get("license_files") != postgres_license_count:
        raise SystemExit(f"PostgreSQL license files missing: {candidate}")
    python_dependencies = manifest.get("python_dependencies")
    if (
        not isinstance(python_dependencies, dict)
        or not (candidate / "python-dependency-licenses.json").is_file()
    ):
        raise SystemExit(f"Python dependency inventory missing: {candidate}")
    license_records: Any = json.loads(
        (candidate / "python-dependency-licenses.json").read_text(encoding="utf-8")
    )
    if not isinstance(license_records, list) or python_dependencies.get("locked_packages") != len(
        license_records
    ):
        raise SystemExit(
            f"Python dependency inventory does not match the runtime manifest: {candidate}"
        )
    python_license_count = sum(
        path.is_file() for path in (candidate / "licenses" / "python").rglob("*")
    )
    if (
        python_license_count == 0
        or python_dependencies.get("license_files") != python_license_count
    ):
        raise SystemExit(f"Python license files missing: {candidate}")

    actual_tree_digest, actual_bytes = candidate_tree_digest(candidate)
    if manifest.get("candidate_tree_sha256") != actual_tree_digest:
        raise SystemExit(f"Runtime candidate tree checksum mismatch: {candidate}")
    if manifest.get("candidate_bytes") != actual_bytes:
        raise SystemExit(f"Runtime candidate size mismatch: {candidate}")
    return {
        "layout": layout,
        "source_revision": expected_revision,
        "candidate_tree_sha256": actual_tree_digest,
        "candidate_bytes": actual_bytes,
        "runtime_manifest_sha256": sha256_file(manifest_path),
    }


def verify_smoke(evidence_root: Path, *, layout: str, expected_revision: str) -> dict[str, Any]:
    layout_root = evidence_root / layout
    evidence_path = layout_root / "smoke.json"
    evidence = read_json(evidence_path)
    if evidence.get("schema_version") != 2:
        raise SystemExit(f"Unsupported runtime smoke evidence version: {evidence_path}")
    if evidence.get("run_kind") != "github_hosted_windows_x64_isolated":
        raise SystemExit(f"Smoke did not use the hosted isolated-runner mode: {evidence_path}")
    if (
        evidence.get("source_revision") != expected_revision
        or evidence.get("source_tree_dirty") is not False
    ):
        raise SystemExit(f"Smoke source revision mismatch: {evidence_path}")
    if evidence.get("status") != "PASS" or evidence.get("clean_windows_runner_status") != "PASS":
        raise SystemExit(f"Clean Windows runtime smoke did not pass: {evidence_path}")
    host = evidence.get("host")
    checks = evidence.get("checks")
    if not isinstance(host, dict) or not isinstance(checks, dict):
        raise SystemExit(f"Smoke evidence is incomplete: {evidence_path}")
    if (
        host.get("architecture_x64") is not True
        or host.get("current_user_is_administrator") is not False
    ):
        raise SystemExit(f"Smoke runner must be x64 and non-administrator: {evidence_path}")
    if host.get("sanitized_path_missing_python_uv_docker") is not True:
        raise SystemExit(f"Smoke PATH exposes developer runtimes: {evidence_path}")
    sanitized_entries = host.get("sanitized_path_entries")
    if not isinstance(sanitized_entries, list) or not {
        "bundled-postgresql/bin",
        "Windows/System32",
    }.issubset(set(sanitized_entries)):
        raise SystemExit(f"Smoke PATH is not restricted to the bundle and Windows: {evidence_path}")
    required_checks = (
        "runtime_manifest_checksum_valid",
        "runtime_uses_only_packaged_postgresql",
        "private_data_acl_current_user_only",
        "dpapi_current_user_credential_round_trip",
        "postgres_loopback_only",
        "migration_idempotent",
        "scheduler_process_started",
        "postgres_state_survives_restart",
        "incompatible_revision_refused_without_rewrite",
        "redacted_runtime_logs",
    )
    if any(checks.get(name) is not True for name in required_checks):
        raise SystemExit(f"Required runtime smoke check failed: {evidence_path}")
    if str(checks.get("http_health_status")) != "200":
        raise SystemExit(f"Packaged HTTP health check did not return 200: {evidence_path}")

    redacted_logs = evidence.get("redacted_logs")
    if not isinstance(redacted_logs, list) or not redacted_logs:
        raise SystemExit(f"Redacted runtime logs missing: {evidence_path}")
    scrub_patterns = (
        re.compile(r"(?i)(password|database_url)\s*[:=]\s*(?!<REDACTED>)[^\s]+"),
        re.compile(r"(?i)authorization\s*[:=]\s*Bearer\s+(?!<REDACTED>)[^\s]+"),
        re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]{24,}"),
        re.compile(r"(?i)postgres(?:ql)?://[^\s:@/]+:[^\s@/]+@"),
    )
    referenced_logs: set[str] = set()
    for item in redacted_logs:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str):
            raise SystemExit(f"Invalid log inventory: {evidence_path}")
        name = item["name"]
        if Path(name).name != name or not name.endswith(".redacted.log"):
            raise SystemExit(f"Unsafe log path in evidence: {evidence_path}")
        log_path = layout_root / name
        if not log_path.is_file():
            raise SystemExit(f"Redacted runtime log missing: {log_path}")
        if item.get("size_bytes") != log_path.stat().st_size or item.get("sha256") != sha256_file(
            log_path
        ):
            raise SystemExit(f"Runtime log checksum mismatch: {log_path}")
        content = log_path.read_text(encoding="utf-8", errors="replace")
        if any(pattern.search(content) for pattern in scrub_patterns):
            raise SystemExit(f"Unredacted password-like field in runtime log: {log_path}")
        referenced_logs.add(name)
    for raw_log in layout_root.rglob("*.raw.log"):
        raise SystemExit(f"Unredacted runtime log was included: {raw_log}")
    actual_logs = {path.name for path in layout_root.glob("*.redacted.log")}
    if actual_logs != referenced_logs:
        raise SystemExit(f"Runtime log inventory mismatch: {evidence_path}")
    return {
        "layout": layout,
        "status": "PASS",
        "source_revision": expected_revision,
        "redacted_log_count": len(referenced_logs),
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify Windows Desktop runtime manifests and smoke evidence"
    )
    parser.add_argument("--candidate-root", type=Path, required=True)
    parser.add_argument("--expected-source-revision", required=True)
    parser.add_argument("--uv-lock", type=Path, required=True)
    parser.add_argument("--download-manifest", type=Path, required=True)
    parser.add_argument("--postgres-archive", type=Path, required=True)
    parser.add_argument("--smoke-evidence-root", type=Path)
    parser.add_argument("--result-path", type=Path)
    args = parser.parse_args()

    lock_sha256 = sha256_file(args.uv_lock)
    download_manifest = read_json(args.download_manifest)
    postgres_manifest = download_manifest.get("postgresql")
    if not isinstance(postgres_manifest, dict):
        raise SystemExit("Pinned PostgreSQL download manifest is invalid")
    expected_postgres_sha256 = postgres_manifest.get("sha256")
    if (
        not isinstance(expected_postgres_sha256, str)
        or sha256_file(args.postgres_archive) != expected_postgres_sha256
        or args.postgres_archive.stat().st_size != postgres_manifest.get("content_bytes")
    ):
        raise SystemExit("Downloaded PostgreSQL archive does not match its pinned SHA-256")

    candidates = [
        verify_candidate(
            args.candidate_root / layout,
            layout=layout,
            expected_revision=args.expected_source_revision,
            expected_lock_sha256=lock_sha256,
            expected_postgres_sha256=expected_postgres_sha256,
        )
        for layout in LAYOUTS
    ]
    smokes = []
    if args.smoke_evidence_root:
        preflight_path = args.smoke_evidence_root / "hosted-runner-preflight.json"
        preflight = read_json(preflight_path)
        if (
            preflight.get("source_revision") != args.expected_source_revision
            or preflight.get("github_actions") is not True
            or preflight.get("runner_environment") != "github-hosted"
            or preflight.get("runner_os") != "Windows"
            or preflight.get("runner_arch") != "X64"
            or preflight.get("workflow_runner_label") != "windows-2025"
            or preflight.get("architecture_x64") is not True
            or not isinstance(preflight.get("runner_image"), str)
            or not preflight.get("runner_image")
        ):
            raise SystemExit(
                "Hosted runner preflight evidence is incomplete or for another source revision"
            )
        ambient_prerequisites = preflight.get("runner_ambient_path_prerequisites")
        if not isinstance(ambient_prerequisites, dict) or set(ambient_prerequisites) != {
            "python",
            "python_launcher",
            "uv",
            "docker",
            "postgresql",
        }:
            raise SystemExit("Hosted runner prerequisite inventory is incomplete")
        if any(not isinstance(present, bool) for present in ambient_prerequisites.values()):
            raise SystemExit("Hosted runner prerequisite inventory has invalid values")
        if not isinstance(preflight.get("runner_process_is_administrator"), bool):
            raise SystemExit("Hosted runner privilege evidence is incomplete")
        smokes = [
            verify_smoke(
                args.smoke_evidence_root,
                layout=layout,
                expected_revision=args.expected_source_revision,
            )
            for layout in LAYOUTS
        ]
    result = {
        "schema_version": 1,
        "status": "PASS",
        "source_revision": args.expected_source_revision,
        "project_lock_sha256": lock_sha256,
        "postgresql_archive_sha256": expected_postgres_sha256,
        "candidates": candidates,
        "hosted_smokes": smokes,
    }
    encoded = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.result_path:
        args.result_path.parent.mkdir(parents=True, exist_ok=True)
        args.result_path.write_text(encoded, encoding="utf-8")
    print(encoded, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
