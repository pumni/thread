from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import zipfile
from pathlib import Path
from typing import Any, cast

import pytest

ROOT = Path(__file__).resolve().parents[2]
AGGREGATOR_PATH = ROOT / "packaging" / "windows_desktop" / "aggregate_worker_desktop_scenarios.py"
SPEC = importlib.util.spec_from_file_location("worker_desktop_acceptance", AGGREGATOR_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("Could not load Worker Desktop acceptance aggregator")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)

worker_desktop_acceptance: Any = MODULE
SOURCE_SHA = "a" * 40
INPUT_VERIFIER_PATH = ROOT / "packaging" / "windows_desktop" / "verify_worker_desktop_inputs.py"
INPUT_VERIFIER_SPEC = importlib.util.spec_from_file_location(
    "verify_worker_desktop_inputs", INPUT_VERIFIER_PATH
)
if INPUT_VERIFIER_SPEC is None or INPUT_VERIFIER_SPEC.loader is None:
    raise RuntimeError("Could not load Worker Desktop package verifier")
INPUT_VERIFIER_MODULE = importlib.util.module_from_spec(INPUT_VERIFIER_SPEC)
INPUT_VERIFIER_SPEC.loader.exec_module(INPUT_VERIFIER_MODULE)
worker_desktop_input_verifier: Any = INPUT_VERIFIER_MODULE
FIXTURE_PATH = ROOT / "packaging" / "windows_desktop" / "worker_desktop_fixture.py"
FIXTURE_SPEC = importlib.util.spec_from_file_location("worker_desktop_fixture", FIXTURE_PATH)
if FIXTURE_SPEC is None or FIXTURE_SPEC.loader is None:
    raise RuntimeError("Could not load Worker Desktop fixture")
FIXTURE_MODULE = importlib.util.module_from_spec(FIXTURE_SPEC)
FIXTURE_SPEC.loader.exec_module(FIXTURE_MODULE)
worker_desktop_fixture: Any = FIXTURE_MODULE


def _evidence(scenario: str, **overrides: object) -> dict[str, Any]:
    evidence: dict[str, Any] = {
        "schema_version": 1,
        "source_sha": SOURCE_SHA,
        "scenario": scenario,
        "runner": {
            "github_hosted": True,
            "windows_x64": True,
            "non_administrator": True,
            "fresh_profile": True,
            "fresh_worker_root": True,
        },
        "result": "PASS",
        "failure_code": None,
        "checks": {check: True for check in MODULE.SCENARIO_CHECKS[scenario]},
        "facts": {
            "worker_uuid": "12345678-1234-4234-8234-123456789abc",
            "identity_marker_sha256": "b" * 64,
            "protected_key_sha256": "c" * 64,
            "journal_sha256": "d" * 64,
            "profile_tree_sha256": "e" * 64,
            "profile_sentinel_sha256": "f" * 64,
            "task_state_timeline": ["RUNNING", "DISABLED"],
            "ownership_timeline": ["LEGACY", "DESKTOP"],
            "worker_status_timeline": ["ONLINE", "DRAINING", "OFFLINE", "ONLINE"],
            "lock_observation_timeline": ["HELD", "NOT_HELD", "HELD"],
            "process_ids": {"desktop": 1200, "worker": 1300, "chromium": [1400]},
            "drain_post_count": 1,
            "drain_status_get_count": 3,
            "operator_me_get_count": 2,
            "worker_pid_timeline": [1300, 1301]
            if scenario == "headed_chromium_profile_continuity"
            else [1300],
            "status_counts_timeline": [
                {
                    "status": "DRAINING",
                    "active_browser_sessions": 1,
                    "running_worker_jobs": 1,
                    "quiescent": False,
                },
                {
                    "status": "OFFLINE",
                    "active_browser_sessions": 0,
                    "running_worker_jobs": 0,
                    "quiescent": False,
                },
            ],
        },
    }
    evidence.update(overrides)
    return evidence


def _write_artifacts(root: Path, source_sha: str = SOURCE_SHA) -> None:
    for scenario in MODULE.SCENARIOS:
        artifact = root / f"dx07-worker-desktop-{source_sha}-{scenario}"
        artifact.mkdir(parents=True)
        (artifact / "worker-desktop.json").write_text(
            json.dumps(_evidence(scenario)), encoding="utf-8"
        )


def test_worker_desktop_aggregate_requires_exact_scenario_set_and_sha(tmp_path: Path) -> None:
    artifacts = tmp_path / "artifacts"
    _write_artifacts(artifacts)

    output = tmp_path / "combined"
    manifest = MODULE.aggregate_scenarios(artifacts, SOURCE_SHA, output)

    assert manifest["result"] == "PASS"
    assert tuple(manifest["required_scenarios"]) == MODULE.SCENARIOS
    assert manifest["observed_artifact_scenarios"] == list(MODULE.SCENARIOS)
    assert all(row["result"] == "PASS" for row in manifest["scenarios"])
    assert (output / "worker-desktop-acceptance-manifest.json").is_file()
    assert all(
        (output / "worker-desktop-scenarios" / scenario / "worker-desktop.json").is_file()
        for scenario in MODULE.SCENARIOS
    )


def test_worker_desktop_aggregate_rejects_missing_and_malformed_evidence(tmp_path: Path) -> None:
    artifacts = tmp_path / "artifacts"
    _write_artifacts(artifacts)
    missing_scenario = MODULE.SCENARIOS[-1]
    missing_artifact = artifacts / f"dx07-worker-desktop-{SOURCE_SHA}-{missing_scenario}"
    (missing_artifact / "worker-desktop.json").write_text("{invalid", encoding="utf-8")

    output = tmp_path / "combined"
    manifest = MODULE.aggregate_scenarios(artifacts, SOURCE_SHA, output)

    assert manifest["result"] == "BLOCKER"
    row = next(item for item in manifest["scenarios"] if item["scenario"] == missing_scenario)
    assert row["failure_code"] == "worker_desktop_evidence_invalid"
    safe_copy = json.loads(
        (output / "worker-desktop-scenarios" / missing_scenario / "worker-desktop.json").read_text(
            encoding="utf-8"
        )
    )
    assert safe_copy["failure_code"] == "worker_desktop_evidence_invalid"

    missing_artifact = artifacts / f"dx07-worker-desktop-{SOURCE_SHA}-{MODULE.SCENARIOS[0]}"
    import shutil

    shutil.rmtree(missing_artifact)
    missing_output = MODULE.aggregate_scenarios(artifacts, SOURCE_SHA, tmp_path / "missing")
    missing_row = next(
        item for item in missing_output["scenarios"] if item["scenario"] == MODULE.SCENARIOS[0]
    )
    assert missing_row["failure_code"] == "worker_desktop_scenario_evidence_missing"


def test_worker_desktop_aggregate_rejects_duplicate_and_unexpected_scenarios(
    tmp_path: Path,
) -> None:
    artifacts = tmp_path / "artifacts"
    _write_artifacts(artifacts)
    duplicate = artifacts / f"dx07-worker-desktop-{SOURCE_SHA}-extra-copy"
    duplicate.mkdir()
    (duplicate / "worker-desktop.json").write_text(
        json.dumps(_evidence(MODULE.SCENARIOS[0])), encoding="utf-8"
    )
    unexpected = artifacts / f"dx07-worker-desktop-{SOURCE_SHA}-unexpected"
    unexpected.mkdir()
    unexpected_evidence = _evidence(MODULE.SCENARIOS[1])
    unexpected_evidence["scenario"] = "unexpected"
    (unexpected / "worker-desktop.json").write_text(
        json.dumps(unexpected_evidence), encoding="utf-8"
    )

    manifest = MODULE.aggregate_scenarios(artifacts, SOURCE_SHA, tmp_path / "combined")

    assert manifest["result"] == "BLOCKER"
    first = next(item for item in manifest["scenarios"] if item["scenario"] == MODULE.SCENARIOS[0])
    assert first["failure_code"] == "worker_desktop_scenario_evidence_duplicated"
    assert manifest["unexpected_scenarios"] == ["extra-copy", "unexpected"]


@pytest.mark.parametrize(
    ("scenario", "overrides", "expected"),
    [
        (
            MODULE.SCENARIOS[0],
            {"source_sha": "b" * 40},
            "worker_desktop_evidence_source_sha_mismatch",
        ),
        (MODULE.SCENARIOS[0], {"result": "BLOCKER", "failure_code": "worker_drain_timeout"}, None),
        (
            MODULE.SCENARIOS[0],
            {"failure_code": "worker_drain_timeout"},
            "worker_desktop_pass_evidence_invalid",
        ),
        (
            MODULE.SCENARIOS[0],
            {
                "runner": {
                    "github_hosted": True,
                    "windows_x64": True,
                    "non_administrator": False,
                    "fresh_profile": True,
                    "fresh_worker_root": True,
                }
            },
            "worker_desktop_runner_evidence_incomplete",
        ),
        (
            MODULE.SCENARIOS[0],
            {"checks": {"drain_post_once": True}},
            "worker_desktop_scenario_checks_invalid",
        ),
    ],
)
def test_worker_desktop_evidence_rejects_invalid_sha_runner_checks_and_results(
    scenario: str, overrides: dict[str, object], expected: str | None
) -> None:
    evidence = _evidence(scenario, **overrides)
    failure = MODULE.validate_evidence(evidence, SOURCE_SHA, scenario)
    if expected is not None:
        assert failure == expected
    else:
        assert failure is None


def test_worker_desktop_safe_evidence_rejects_secret_fields_and_untyped_values() -> None:
    evidence = _evidence(MODULE.SCENARIOS[0])
    evidence["operator_bearer"] = "synthetic-token"
    assert (
        MODULE.validate_evidence(evidence, SOURCE_SHA, MODULE.SCENARIOS[0])
        == "worker_desktop_evidence_fields_invalid"
    )

    evidence = _evidence(MODULE.SCENARIOS[0])
    evidence["facts"]["password"] = "synthetic-password"
    assert (
        MODULE.validate_evidence(evidence, SOURCE_SHA, MODULE.SCENARIOS[0])
        == "worker_desktop_safe_facts_invalid"
    )

    evidence = _evidence(MODULE.SCENARIOS[0])
    evidence["facts"]["task_state_timeline"] = ["raw task XML"]
    assert (
        MODULE.validate_evidence(evidence, SOURCE_SHA, MODULE.SCENARIOS[0])
        == "worker_desktop_safe_facts_invalid"
    )

    evidence = _evidence(MODULE.SCENARIOS[0])
    evidence["facts"]["status_counts_timeline"][0]["status"] = []
    assert (
        MODULE.validate_evidence(evidence, SOURCE_SHA, MODULE.SCENARIOS[0])
        == "worker_desktop_safe_facts_invalid"
    )

    evidence = _evidence(MODULE.SCENARIOS[0])
    evidence["schema_version"] = True
    assert (
        MODULE.validate_evidence(evidence, SOURCE_SHA, MODULE.SCENARIOS[0])
        == "worker_desktop_evidence_schema_invalid"
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("worker_pid_timeline", [1300]),
        ("worker_pid_timeline", [1300, 1300]),
        ("worker_status_timeline", ["ONLINE", "OFFLINE", "ONLINE"]),
        ("drain_post_count", 0),
    ],
)
def test_profile_evidence_requires_a_real_worker_restart_boundary(
    field: str, value: object
) -> None:
    evidence = _evidence("headed_chromium_profile_continuity")
    evidence["facts"][field] = value

    assert (
        MODULE.validate_evidence(evidence, SOURCE_SHA, "headed_chromium_profile_continuity")
        == "worker_desktop_profile_restart_boundary_invalid"
    )


def test_profile_blocker_evidence_can_report_failure_before_restart_boundary() -> None:
    evidence = _evidence(
        "headed_chromium_profile_continuity",
        result="BLOCKER",
        failure_code="worker_desktop_restart_drain_not_requested",
    )
    evidence["facts"]["worker_pid_timeline"] = []
    evidence["facts"]["worker_status_timeline"] = []
    evidence["facts"]["drain_post_count"] = 0

    assert (
        MODULE.validate_evidence(evidence, SOURCE_SHA, "headed_chromium_profile_continuity") is None
    )


def test_worker_desktop_aggregate_requires_lowercase_full_source_sha(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="source_sha"):
        MODULE.aggregate_scenarios(tmp_path, "A" * 40, tmp_path / "combined")


def test_worker_desktop_workflow_uses_staged_helper_and_secret_free_fixture_boundary() -> None:
    workflow = (ROOT / ".github" / "workflows" / "worker-desktop-acceptance.yml").read_text(
        encoding="utf-8"
    )
    harness = (
        ROOT / "packaging" / "windows_desktop" / "smoke_worker_desktop_lifecycle.ps1"
    ).read_text(encoding="utf-8")
    for required in (
        "Stage-WorkerDesktopTestArtifact.ps1",
        "Manage-ThreadsWorkerTask.ps1",
        "worker-desktop.json",
        "THREADS_WORKER_ENROLLMENT_CODE",
        "THREADS_WORKER_CONTROL_PLANE_URL",
        "THREADS_WORKER_DATA_ROOT",
        "--expected-source-sha",
        "captured_descendants_alive_at_crash",
        "worker_restart_requested",
        "restart_drain_post_once",
        "Restart-PageFixture",
    ):
        assert required in workflow + harness
    assert "github.event.pull_request.head.sha" not in workflow
    assert "workflow_dispatch:" not in workflow


def test_worker_desktop_crash_and_profile_scenarios_cross_real_process_boundaries() -> None:
    harness = (
        ROOT / "packaging" / "windows_desktop" / "smoke_worker_desktop_lifecycle.ps1"
    ).read_text(encoding="utf-8")
    crash = harness.split("function Test-DesktopCrashLogoutRecovery {", 1)[1].split(
        "function Watch-DesktopWorkerRestart(", 1
    )[0]
    profile = harness.split("function Test-HeadedChromiumProfileContinuity {", 1)[1].split(
        "function Complete-SafeScenarioCleanup", 1
    )[0]

    assert "Release-BlockedPage" not in crash
    assert crash.index('Assert-ScenarioCheck "captured_descendants_alive_at_crash"') < crash.index(
        "Stop-Process -Id $desktopId -Force"
    )
    assert "Test-ProcessIdAlive $workerPid" in crash
    assert "Test-ProcessIdAlive $_" in crash
    assert 'Invoke-UiButton $desktopId "Restart…"' in profile
    assert 'Invoke-UiButton $desktopId "Authenticate and restart"' in profile
    assert "Watch-DesktopWorkerRestart" in profile
    assert "Queue-BlockedProfileJob" in profile.split("Watch-DesktopWorkerRestart", 1)[1]


def test_drain_fault_fixture_rejects_only_drain_post_and_keeps_operator_reads_live(
    tmp_path: Path,
) -> None:
    counter_path = tmp_path / "request-counts.json"
    fault_path = tmp_path / "reject-drain-post"
    fault_path.touch()
    passed_through: list[tuple[str, str]] = []

    async def control_plane(scope: dict[str, object], _receive: Any, send: Any) -> None:
        passed_through.append((str(scope["method"]), str(scope["path"])))
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send({"type": "http.response.body", "body": b"{}"})

    app = worker_desktop_fixture._DrainFaultControlPlaneApp(control_plane, counter_path, fault_path)
    responses: dict[str, int] = {}

    async def request(method: str, path: str) -> None:
        messages: list[dict[str, object]] = []

        async def send(message: dict[str, object]) -> None:
            messages.append(message)

        await app(
            {"type": "http", "method": method, "path": path},
            None,
            send,
        )
        start = next(message for message in messages if message["type"] == "http.response.start")
        responses[f"{method} {path}"] = cast(int, start["status"])

    drain_path = "/v1/workers/12345678-1234-4234-8234-123456789abc/drain"
    asyncio.run(request("GET", "/v1/operator/me"))
    asyncio.run(request("GET", drain_path))
    asyncio.run(request("POST", drain_path))

    assert responses == {
        "GET /v1/operator/me": 200,
        f"GET {drain_path}": 200,
        f"POST {drain_path}": 503,
    }
    assert passed_through == [("GET", "/v1/operator/me"), ("GET", drain_path)]
    assert json.loads(counter_path.read_text(encoding="utf-8")) == {
        "drain_post_count": 1,
        "drain_status_get_count": 1,
        "operator_me_get_count": 1,
    }


def _write_worker_package_artifact(root: Path) -> tuple[str, str]:
    root.mkdir()
    package_name = "threads-worker-windows-x64"
    project_version = "0.1.0"
    worker_sha = SOURCE_SHA
    manifest = {
        "artifact_schema": "threads-worker-package-v1",
        "project_version": project_version,
        "git_sha": worker_sha,
        "python_version": "3.14.7",
        "playwright_version": "1.63.0",
        "pyinstaller_version": "6.22.3",
        "target_os": "windows",
        "target_arch": "x64",
        "browser": "chromium",
        "created_at_utc": "2026-10-05T00:00:00Z",
    }
    package_files = {
        "threads-worker.exe": b"synthetic worker executable",
        "_internal/runtime.dll": b"synthetic runtime",
        (
            "_internal/playwright/driver/package/.local-browsers/chromium-123/chrome-win/chrome.exe"
        ): b"synthetic chromium",
        "BUILD-MANIFEST.json": (json.dumps(manifest) + "\n").encode(),
    }
    archive_name = worker_desktop_input_verifier.package_archive_name(project_version, worker_sha)
    archive_path = root / archive_name
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in package_files.items():
            archive.writestr(f"{package_name}/{name}", content)
    checksum = hashlib.sha256(archive_path.read_bytes()).hexdigest()
    (root / f"{archive_name}.sha256").write_text(f"{checksum}  {archive_name}\n", encoding="ascii")
    (root / "BUILD-MANIFEST.json").write_text(json.dumps(manifest) + "\n", encoding="utf-8")
    return archive_name, worker_sha


def test_worker_desktop_package_verifier_checks_exact_archive_before_release_staging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package_root = tmp_path / "artifact"
    archive_name, worker_sha = _write_worker_package_artifact(package_root)
    program_files = tmp_path / "Program Files"
    monkeypatch.setenv("ProgramFiles", str(program_files))

    release, manifest = worker_desktop_input_verifier.verify_and_extract(
        package_root, program_files / "ThreadsWorker" / "releases", SOURCE_SHA
    )

    assert release.name.endswith(worker_sha)
    assert manifest["git_sha"] == worker_sha
    assert (release / "threads-worker.exe").is_file()
    assert (release / "BUILD-MANIFEST.json").is_file()
    assert (package_root / archive_name).is_file()


def test_worker_desktop_package_verifier_rejects_checksum_and_path_traversal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package_root = tmp_path / "artifact"
    archive_name, _worker_sha = _write_worker_package_artifact(package_root)
    program_files = tmp_path / "Program Files"
    monkeypatch.setenv("ProgramFiles", str(program_files))
    checksum_path = package_root / f"{archive_name}.sha256"
    checksum_path.write_text(f"{'0' * 64}  {archive_name}\n", encoding="ascii")

    with pytest.raises(ValueError, match="worker_package_checksum_mismatch"):
        worker_desktop_input_verifier.verify_and_extract(
            package_root, program_files / "ThreadsWorker" / "releases", SOURCE_SHA
        )

    checksum = hashlib.sha256((package_root / archive_name).read_bytes()).hexdigest()
    checksum_path.write_text(f"{checksum}  {archive_name}\n", encoding="ascii")
    archive_path = package_root / archive_name
    with zipfile.ZipFile(archive_path, "a") as archive:
        archive.writestr("threads-worker-windows-x64/../../escape.txt", b"invalid")
    checksum = hashlib.sha256(archive_path.read_bytes()).hexdigest()
    checksum_path.write_text(f"{checksum}  {archive_name}\n", encoding="ascii")

    with pytest.raises(ValueError, match="worker_package_archive_entry_invalid"):
        worker_desktop_input_verifier.verify_and_extract(
            package_root, program_files / "ThreadsWorker" / "releases", SOURCE_SHA
        )


def test_worker_desktop_package_verifier_rejects_self_consistent_package_for_other_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package_root = tmp_path / "artifact"
    archive_name, worker_sha = _write_worker_package_artifact(package_root)
    program_files = tmp_path / "Program Files"
    monkeypatch.setenv("ProgramFiles", str(program_files))

    assert worker_sha == SOURCE_SHA
    with pytest.raises(ValueError, match="worker_package_source_sha_mismatch"):
        worker_desktop_input_verifier.verify_and_extract(
            package_root, program_files / "ThreadsWorker" / "releases", "c" * 40
        )

    assert not (
        program_files / "ThreadsWorker" / "releases" / f"windows-x64-0.1.0-{worker_sha}"
    ).exists()
    assert (package_root / archive_name).is_file()


def test_worker_desktop_preflight_needs_worker_paths_to_run_both_gates() -> None:
    ci_script = (ROOT / "scripts" / "ci_changed_paths.py").read_text(encoding="utf-8")
    assert "worker_desktop_relevant" not in ci_script
    assert "packaging/windows_desktop/smoke_worker_desktop_lifecycle.ps1" in ci_script
    assert "apps/desktop/src-tauri/src/windows_crypto.rs" in ci_script
