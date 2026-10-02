from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any

METADATA_FILES = {"runtime-manifest.json", "runtime-manifest.json.sha256"}
LAYOUTS = ("shared", "split")
SCRUB_PATTERNS = (
    re.compile(r"(?i)(password|database_url)\s*[:=]\s*(?!<REDACTED>)[^\s]+"),
    re.compile(r"(?i)authorization\s*[:=]\s*Bearer\s+(?!<REDACTED>)[^\s]+"),
    re.compile(r"(?i)\bBearer\s+(?!<REDACTED>)[A-Za-z0-9._~+/-]{24,}"),
    re.compile(r"(?i)postgres(?:ql)?(?:\+\w+)?://[^\s:@/]+:[^@\s/]+@"),
)


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


def verify_hosted_process_diagnostics(
    layout_root: Path, *, layout: str, expected_revision: str
) -> dict[str, Any]:
    process_path = layout_root / "smoke-process.json"
    process = read_json(process_path)
    if (
        process.get("schema_version") != 1
        or process.get("source_revision") != expected_revision
        or process.get("layout") != layout
    ):
        raise SystemExit(f"Hosted smoke process evidence source mismatch: {process_path}")

    records = process.get("sanitized_diagnostics")
    if not isinstance(records, list) or len(records) != 2:
        raise SystemExit(f"Hosted smoke process diagnostics are incomplete: {process_path}")
    referenced: set[str] = set()
    for record in records:
        if not isinstance(record, dict) or not isinstance(record.get("name"), str):
            raise SystemExit(f"Invalid hosted smoke process diagnostic: {process_path}")
        name = record["name"]
        relative = Path(name)
        if relative.is_absolute() or ".." in relative.parts or not name.endswith(".redacted.log"):
            raise SystemExit(f"Unsafe hosted smoke process diagnostic path: {process_path}")
        diagnostic_path = layout_root / relative
        if (
            not diagnostic_path.is_file()
            or record.get("size_bytes") != diagnostic_path.stat().st_size
            or record.get("sha256") != sha256_file(diagnostic_path)
        ):
            raise SystemExit(
                f"Hosted smoke process diagnostic checksum mismatch: {diagnostic_path}"
            )
        content = diagnostic_path.read_text(encoding="utf-8", errors="replace")
        if any(pattern.search(content) for pattern in SCRUB_PATTERNS):
            raise SystemExit(f"Unredacted hosted smoke process output: {diagnostic_path}")
        referenced.add(name)
    if referenced != {
        "hosted-smoke-process/stdout.redacted.log",
        "hosted-smoke-process/stderr.redacted.log",
    }:
        raise SystemExit(f"Hosted smoke process diagnostic inventory is incomplete: {process_path}")
    diagnostic_root = layout_root / "hosted-smoke-process"
    actual = {
        path.relative_to(layout_root).as_posix() for path in diagnostic_root.glob("*.redacted.log")
    }
    if actual != referenced:
        raise SystemExit(f"Hosted smoke process diagnostic inventory mismatch: {process_path}")
    return process


def verify_runtime_logs(
    layout_root: Path, evidence_path: Path, evidence: dict[str, Any], *, require_nonempty: bool
) -> int:
    redacted_logs = evidence.get("redacted_logs")
    if not isinstance(redacted_logs, list) or (require_nonempty and not redacted_logs):
        raise SystemExit(f"Redacted runtime log inventory is incomplete: {evidence_path}")
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
        if any(pattern.search(content) for pattern in SCRUB_PATTERNS):
            raise SystemExit(f"Unredacted password-like field in runtime log: {log_path}")
        referenced_logs.add(name)
    for raw_log in layout_root.rglob("*.raw.log"):
        raise SystemExit(f"Unredacted runtime log was included: {raw_log}")
    actual_logs = {path.name for path in layout_root.glob("*.redacted.log")}
    if actual_logs != referenced_logs:
        raise SystemExit(f"Runtime log inventory mismatch: {evidence_path}")
    return len(referenced_logs)


def verify_smoke(evidence_root: Path, *, layout: str, expected_revision: str) -> dict[str, Any]:
    layout_root = evidence_root / layout
    evidence_path = layout_root / "smoke.json"
    evidence = read_json(evidence_path)
    process = verify_hosted_process_diagnostics(
        layout_root, layout=layout, expected_revision=expected_revision
    )
    if evidence.get("schema_version") != 2:
        raise SystemExit(f"Unsupported runtime smoke evidence version: {evidence_path}")
    if evidence.get("run_kind") != "github_hosted_windows_x64_isolated":
        raise SystemExit(f"Smoke did not use the hosted isolated-runner mode: {evidence_path}")
    if (
        evidence.get("source_revision") != expected_revision
        or evidence.get("source_tree_dirty") is not False
    ):
        raise SystemExit(f"Smoke source revision mismatch: {evidence_path}")
    primary_failure = evidence.get("primary_failure_code") or evidence.get("failure_code")
    log_scrub_status = evidence.get("log_scrub_status")
    smoke_passed = (
        evidence.get("status") == "PASS"
        and evidence.get("clean_windows_runner_status") == "PASS"
        and process.get("child_exit_code") == 0
        and process.get("status") == "PASS"
        and primary_failure is None
        and log_scrub_status == "PASS"
    )
    try:
        redacted_log_count = verify_runtime_logs(
            layout_root, evidence_path, evidence, require_nonempty=smoke_passed
        )
        runtime_log_verification_failure = None
    except SystemExit as exc:
        redacted_log_count = 0
        runtime_log_verification_failure = str(exc)
        if smoke_passed:
            raise
    if not smoke_passed or runtime_log_verification_failure:
        if not primary_failure and log_scrub_status != "PASS":
            primary_failure = "runtime_log_scrub_verification_failed"
        if not primary_failure:
            primary_failure = process.get("spawn_failure_code") or "runtime_smoke_not_passed"
        return {
            "layout": layout,
            "status": "BLOCKER",
            "source_revision": expected_revision,
            "primary_failure_code": primary_failure,
            "log_scrub_status": log_scrub_status,
            "log_scrub_failure_code": evidence.get("log_scrub_failure_code"),
            "child_exit_code": process.get("child_exit_code"),
            "smoke_status": evidence.get("status"),
            "runtime_log_verification_failure": runtime_log_verification_failure,
            "checks": evidence.get("checks"),
        }
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

    return {
        "layout": layout,
        "status": "PASS",
        "source_revision": expected_revision,
        "child_exit_code": process.get("child_exit_code"),
        "primary_failure_code": None,
        "log_scrub_status": "PASS",
        "redacted_log_count": redacted_log_count,
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

    lock_sha256 = None
    expected_postgres_sha256 = None
    candidates: list[dict[str, Any]] = []
    smokes: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    try:
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
    except SystemExit as exc:
        failures.append({"stage": "pinned_inputs", "failure": str(exc)})
    except Exception as exc:
        failures.append({"stage": "pinned_inputs", "failure": str(exc)})

    if lock_sha256 and expected_postgres_sha256:
        for layout in LAYOUTS:
            try:
                candidate_result = verify_candidate(
                    args.candidate_root / layout,
                    layout=layout,
                    expected_revision=args.expected_source_revision,
                    expected_lock_sha256=lock_sha256,
                    expected_postgres_sha256=expected_postgres_sha256,
                )
                candidate_result["status"] = "PASS"
                candidates.append(candidate_result)
            except SystemExit as exc:
                failure = str(exc)
                candidates.append({"layout": layout, "status": "BLOCKER", "failure": failure})
                failures.append({"stage": "candidate", "layout": layout, "failure": failure})
            except Exception as exc:
                failure = str(exc)
                candidates.append({"layout": layout, "status": "BLOCKER", "failure": failure})
                failures.append({"stage": "candidate", "layout": layout, "failure": failure})
    else:
        candidates = [
            {"layout": layout, "status": "BLOCKER", "failure": "pinned_inputs_unavailable"}
            for layout in LAYOUTS
        ]

    preflight_status = "NOT_REQUESTED"
    if args.smoke_evidence_root:
        preflight_path = args.smoke_evidence_root / "hosted-runner-preflight.json"
        try:
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
            preflight_status = "PASS"
        except SystemExit as exc:
            preflight_status = "BLOCKER"
            failures.append({"stage": "preflight", "failure": str(exc)})
        except Exception as exc:
            preflight_status = "BLOCKER"
            failures.append({"stage": "preflight", "failure": str(exc)})

        for layout in LAYOUTS:
            try:
                smoke = verify_smoke(
                    args.smoke_evidence_root,
                    layout=layout,
                    expected_revision=args.expected_source_revision,
                )
                smokes.append(smoke)
                if smoke.get("status") != "PASS":
                    failures.append(
                        {
                            "stage": "smoke",
                            "layout": layout,
                            "primary_failure_code": smoke.get("primary_failure_code"),
                            "log_scrub_status": smoke.get("log_scrub_status"),
                            "child_exit_code": smoke.get("child_exit_code"),
                            "runtime_log_verification_failure": smoke.get(
                                "runtime_log_verification_failure"
                            ),
                        }
                    )
            except SystemExit as exc:
                failure = str(exc)
                smoke_failure: dict[str, Any] = {
                    "layout": layout,
                    "status": "BLOCKER",
                    "failure": failure,
                }
                smoke_path = args.smoke_evidence_root / layout / "smoke.json"
                if smoke_path.is_file():
                    try:
                        raw_smoke = read_json(smoke_path)
                        smoke_failure["primary_failure_code"] = raw_smoke.get(
                            "primary_failure_code"
                        ) or raw_smoke.get("failure_code")
                        smoke_failure["log_scrub_status"] = raw_smoke.get("log_scrub_status")
                    except Exception:
                        pass
                smokes.append(smoke_failure)
                failures.append(
                    {
                        "stage": "smoke",
                        "layout": layout,
                        "primary_failure_code": smoke_failure.get("primary_failure_code"),
                        "log_scrub_status": smoke_failure.get("log_scrub_status"),
                        "verification_failure": failure,
                    }
                )
            except Exception as exc:
                failure = str(exc)
                smokes.append({"layout": layout, "status": "BLOCKER", "failure": failure})
                failures.append({"stage": "smoke", "layout": layout, "failure": failure})

    result = {
        "schema_version": 1,
        "status": "PASS" if not failures else "BLOCKER",
        "source_revision": args.expected_source_revision,
        "project_lock_sha256": lock_sha256,
        "postgresql_archive_sha256": expected_postgres_sha256,
        "candidates": candidates,
        "hosted_preflight_status": preflight_status,
        "hosted_smokes": smokes,
        "failures": failures,
    }
    encoded = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.result_path:
        args.result_path.parent.mkdir(parents=True, exist_ok=True)
        args.result_path.write_text(encoded, encoding="utf-8")
    print(encoded, end="")
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
