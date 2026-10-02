from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

SCRIPT = Path(__file__).parents[2] / "packaging" / "windows_desktop" / "verify_runtime_evidence.py"


def _load_verifier() -> ModuleType:
    spec = importlib.util.spec_from_file_location("windows_desktop_evidence_verifier", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def _process_evidence(layout_root: Path, *, layout: str, revision: str) -> None:
    diagnostic_root = layout_root / "hosted-smoke-process"
    diagnostic_root.mkdir(parents=True, exist_ok=True)
    diagnostics: list[dict[str, Any]] = []
    for name in ("stdout.redacted.log", "stderr.redacted.log"):
        content = b""
        (diagnostic_root / name).write_bytes(content)
        diagnostics.append(
            {
                "name": f"hosted-smoke-process/{name}",
                "size_bytes": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        )
    _write_json(
        layout_root / "smoke-process.json",
        {
            "schema_version": 1,
            "source_revision": revision,
            "layout": layout,
            "child_exit_code": 1,
            "spawn_failure_code": None,
            "smoke_json_copied": True,
            "smoke_status": "FAIL",
            "primary_failure_code": "packaged_http_exited_before_ready",
            "log_scrub_status": "FAIL",
            "status": "BLOCKER",
            "sanitized_diagnostics": diagnostics,
        },
    )


def test_runtime_smoke_keeps_root_failure_separate_from_log_scrub_failure(tmp_path: Path) -> None:
    verifier = _load_verifier()
    layout = "shared"
    revision = "a" * 40
    layout_root = tmp_path / layout
    _process_evidence(layout_root, layout=layout, revision=revision)
    _write_json(
        layout_root / "smoke.json",
        {
            "schema_version": 4,
            "run_kind": "github_hosted_windows_x64_isolated",
            "source_revision": revision,
            "source_tree_dirty": False,
            "status": "FAIL",
            "clean_windows_runner_status": "BLOCKER",
            "primary_failure_code": "packaged_http_exited_before_ready",
            "failure_code": "packaged_http_exited_before_ready",
            "log_scrub_status": "FAIL",
            "log_scrub_failure_code": "runtime_log_scrub_verification_failed",
            "redacted_logs": [],
            "checks": {"redacted_runtime_logs": False},
        },
    )

    result = verifier.verify_smoke(tmp_path, layout=layout, expected_revision=revision)

    assert result["status"] == "BLOCKER"
    assert result["primary_failure_code"] == "packaged_http_exited_before_ready"
    assert result["log_scrub_status"] == "FAIL"
    assert result["log_scrub_failure_code"] == "runtime_log_scrub_verification_failed"


def test_not_run_log_scrub_keeps_pre_runtime_primary_failure(tmp_path: Path) -> None:
    verifier = _load_verifier()
    layout = "shared"
    revision = "d" * 40
    layout_root = tmp_path / layout
    _process_evidence(layout_root, layout=layout, revision=revision)
    _write_json(
        layout_root / "smoke.json",
        {
            "schema_version": 4,
            "run_kind": "github_hosted_windows_x64_isolated",
            "source_revision": revision,
            "source_tree_dirty": False,
            "status": "FAIL",
            "clean_windows_runner_status": "BLOCKER",
            "primary_failure_code": "sanitized_path_leaked_forbidden_tool",
            "failure_code": "sanitized_path_leaked_forbidden_tool",
            "log_scrub_status": "NOT_RUN",
            "log_scrub_failure_code": None,
            "redacted_logs": [],
            "checks": {"redacted_runtime_logs": None},
        },
    )

    result = verifier.verify_smoke(tmp_path, layout=layout, expected_revision=revision)

    assert result["status"] == "BLOCKER"
    assert result["primary_failure_code"] == "sanitized_path_leaked_forbidden_tool"
    assert result["log_scrub_status"] == "NOT_RUN"
    assert result["log_scrub_failure_code"] is None


def test_sanitized_path_inventory_allows_ambient_docker_with_packaged_pg_ctl(
    tmp_path: Path,
) -> None:
    verifier = _load_verifier()
    bundle = r"C:\staging\shared"
    inventory: dict[str, dict[str, Any]] = {
        name: {"resolved": False, "path": None}
        for name in ("python.exe", "python3.exe", "py.exe", "uv.exe", "docker.exe")
    }
    inventory["docker.exe"] = {"resolved": True, "path": r"C:\Windows\System32\docker.exe"}
    inventory["pg_ctl.exe"] = {
        "resolved": True,
        "path": bundle + r"\postgresql\bin\pg_ctl.exe",
    }

    verifier.verify_sanitized_path_inventory(
        {"runtime_bundle_root": bundle, "sanitized_path_tool_inventory": inventory},
        tmp_path / "smoke.json",
    )


def test_sanitized_path_inventory_rejects_resolved_host_tool(tmp_path: Path) -> None:
    verifier = _load_verifier()
    bundle = r"C:\staging\split"
    inventory: dict[str, dict[str, Any]] = {
        name: {"resolved": False, "path": None}
        for name in ("python.exe", "python3.exe", "py.exe", "uv.exe", "docker.exe")
    }
    inventory["py.exe"] = {"resolved": True, "path": r"C:\Windows\py.exe"}
    inventory["pg_ctl.exe"] = {
        "resolved": True,
        "path": bundle + r"\postgresql\bin\pg_ctl.exe",
    }

    with pytest.raises(SystemExit, match="forbidden host tools"):
        verifier.verify_sanitized_path_inventory(
            {"runtime_bundle_root": bundle, "sanitized_path_tool_inventory": inventory},
            tmp_path / "smoke.json",
        )


@pytest.mark.parametrize("tool_name", ["python.exe", "uv.exe"])
def test_sanitized_path_inventory_rejects_host_python_and_uv(
    tmp_path: Path, tool_name: str
) -> None:
    verifier = _load_verifier()
    bundle = r"C:\staging\split"
    inventory: dict[str, dict[str, Any]] = {
        name: {"resolved": False, "path": None}
        for name in ("python.exe", "python3.exe", "py.exe", "uv.exe", "docker.exe")
    }
    inventory[tool_name] = {"resolved": True, "path": rf"C:\host\{tool_name}"}
    inventory["pg_ctl.exe"] = {
        "resolved": True,
        "path": bundle + r"\postgresql\bin\pg_ctl.exe",
    }

    with pytest.raises(SystemExit, match="forbidden host tools"):
        verifier.verify_sanitized_path_inventory(
            {"runtime_bundle_root": bundle, "sanitized_path_tool_inventory": inventory},
            tmp_path / "smoke.json",
        )


def test_sanitized_path_inventory_rejects_host_postgres(tmp_path: Path) -> None:
    verifier = _load_verifier()
    bundle = r"C:\staging\split"
    inventory: dict[str, dict[str, Any]] = {
        name: {"resolved": False, "path": None}
        for name in ("python.exe", "python3.exe", "py.exe", "uv.exe", "docker.exe")
    }
    inventory["pg_ctl.exe"] = {
        "resolved": True,
        "path": r"C:\Program Files\PostgreSQL\bin\pg_ctl.exe",
    }

    with pytest.raises(SystemExit, match="packaged pg_ctl"):
        verifier.verify_sanitized_path_inventory(
            {"runtime_bundle_root": bundle, "sanitized_path_tool_inventory": inventory},
            tmp_path / "smoke.json",
        )


def test_hosted_process_verifier_rejects_unscrubbed_bearer_output(tmp_path: Path) -> None:
    verifier = _load_verifier()
    layout = "split"
    revision = "c" * 40
    layout_root = tmp_path / layout
    _process_evidence(layout_root, layout=layout, revision=revision)

    stdout_path = layout_root / "hosted-smoke-process" / "stdout.redacted.log"
    stdout_path.write_text(f"Authorization: Bearer {'A' * 32}\n", encoding="utf-8")
    process_path = layout_root / "smoke-process.json"
    process = json.loads(process_path.read_text(encoding="utf-8"))
    stdout_record = next(
        item
        for item in process["sanitized_diagnostics"]
        if item["name"].endswith("stdout.redacted.log")
    )
    content = stdout_path.read_bytes()
    stdout_record["size_bytes"] = len(content)
    stdout_record["sha256"] = hashlib.sha256(content).hexdigest()
    process_path.write_text(json.dumps(process), encoding="utf-8")

    with pytest.raises(SystemExit, match="Unredacted hosted smoke process output"):
        verifier.verify_hosted_process_diagnostics(
            layout_root, layout=layout, expected_revision=revision
        )


def test_verification_artifact_records_both_layouts_after_one_smoke_fails(
    tmp_path: Path, monkeypatch: Any
) -> None:
    verifier = _load_verifier()
    revision = "b" * 40
    candidate_root = tmp_path / "candidates"
    candidate_root.mkdir()
    uv_lock = tmp_path / "uv.lock"
    uv_lock.write_text("locked", encoding="utf-8")
    postgres_archive = tmp_path / "postgres.zip"
    postgres_archive.write_bytes(b"pinned postgres archive")
    postgres_digest = hashlib.sha256(postgres_archive.read_bytes()).hexdigest()
    download_manifest = tmp_path / "download-manifest.json"
    _write_json(
        download_manifest,
        {
            "postgresql": {
                "sha256": postgres_digest,
                "content_bytes": postgres_archive.stat().st_size,
            }
        },
    )
    evidence_root = tmp_path / "hosted"
    _write_json(
        evidence_root / "hosted-runner-preflight.json",
        {
            "source_revision": revision,
            "github_actions": True,
            "runner_environment": "github-hosted",
            "runner_os": "Windows",
            "runner_arch": "X64",
            "workflow_runner_label": "windows-2025",
            "architecture_x64": True,
            "runner_image": "windows-2025",
            "runner_ambient_path_prerequisites": {
                "python": True,
                "python_launcher": True,
                "uv": True,
                "docker": True,
                "postgresql": True,
            },
            "runner_process_is_administrator": True,
        },
    )
    result_path = tmp_path / "runtime-evidence-verification.json"

    def verify_candidate(_candidate: Path, *, layout: str, **_kwargs: Any) -> dict[str, Any]:
        return {"layout": layout, "status": "PASS"}

    monkeypatch.setattr(verifier, "verify_candidate", verify_candidate)

    def verify_layout(_root: Path, *, layout: str, expected_revision: str) -> dict[str, Any]:
        if layout == "shared":
            return {
                "layout": layout,
                "status": "BLOCKER",
                "primary_failure_code": "packaged_http_exited_before_ready",
                "log_scrub_status": "FAIL",
                "child_exit_code": 1,
            }
        return {
            "layout": layout,
            "status": "PASS",
            "primary_failure_code": None,
            "log_scrub_status": "PASS",
            "child_exit_code": 0,
        }

    monkeypatch.setattr(verifier, "verify_smoke", verify_layout)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(SCRIPT),
            "--candidate-root",
            str(candidate_root),
            "--expected-source-revision",
            revision,
            "--uv-lock",
            str(uv_lock),
            "--download-manifest",
            str(download_manifest),
            "--postgres-archive",
            str(postgres_archive),
            "--smoke-evidence-root",
            str(evidence_root),
            "--result-path",
            str(result_path),
        ],
    )

    exit_code = verifier.main()
    result = json.loads(result_path.read_text(encoding="utf-8"))

    assert exit_code == 1
    assert result["status"] == "BLOCKER"
    assert [smoke["layout"] for smoke in result["hosted_smokes"]] == ["shared", "split"]
    assert result["hosted_smokes"][0]["primary_failure_code"] == (
        "packaged_http_exited_before_ready"
    )
    assert result["hosted_smokes"][1]["status"] == "PASS"
