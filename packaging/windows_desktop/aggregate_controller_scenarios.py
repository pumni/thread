from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path
from typing import Any

REQUIRED_SCENARIOS = (
    "bootstrap_https_cutover_tray",
    "restart_renewal",
    "database_crash_recovery",
    "parent_crash_recovery",
    "migration_recovery_auth",
    "database_port_collision",
    "endpoint_port_collision",
    "unowned_root",
    "unwritable_root",
    "corrupt_cluster",
)


def _read_evidence(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):  # fmt: skip
        return None
    return value if isinstance(value, dict) else None


def _scenario_attempt(name: str, prefix: str) -> tuple[str, int] | None:
    if not name.startswith(prefix):
        return None

    scenario, separator, attempt_text = name[len(prefix) :].rpartition("-attempt-")
    if not separator or scenario not in REQUIRED_SCENARIOS:
        return None
    if not attempt_text.isdecimal():
        return None

    attempt = int(attempt_text)
    if attempt < 1 or str(attempt) != attempt_text:
        return None
    return scenario, attempt


def aggregate_scenarios(artifact_root: Path, source_sha: str, output_root: Path) -> dict[str, Any]:
    output_root.mkdir(parents=True, exist_ok=True)
    scenario_output = output_root / "controller-scenarios"
    scenario_output.mkdir(parents=True, exist_ok=True)

    candidates: dict[str, list[tuple[int, Path]]] = {
        scenario: [] for scenario in REQUIRED_SCENARIOS
    }
    invalid_artifacts: list[str] = []
    prefix = f"dx04-evidence-{source_sha}-"
    if artifact_root.exists():
        for artifact in sorted(path for path in artifact_root.iterdir() if path.is_dir()):
            if artifact.name.startswith(prefix):
                parsed = _scenario_attempt(artifact.name, prefix)
                if parsed is None:
                    invalid_artifacts.append(artifact.name)
                else:
                    scenario, attempt = parsed
                    candidates[scenario].append((attempt, artifact))

    observed = [scenario for scenario in REQUIRED_SCENARIOS if candidates[scenario]]
    rows: list[dict[str, Any]] = []
    all_valid = not invalid_artifacts and len(observed) == len(REQUIRED_SCENARIOS)

    for required in REQUIRED_SCENARIOS:
        available = candidates[required]
        if not available:
            rows.append(
                {
                    "scenario": required,
                    "artifact_count": 0,
                    "available_attempts": [],
                    "selected_attempt": None,
                    "result": "MISSING",
                    "primary_failure_code": "controller_scenario_artifact_set_invalid",
                }
            )
            all_valid = False
            continue

        selected_attempt = max(attempt for attempt, _path in available)
        selected = [path for attempt, path in available if attempt == selected_attempt]
        available_attempts = sorted(attempt for attempt, _path in available)
        if len(selected) != 1:
            rows.append(
                {
                    "scenario": required,
                    "artifact_count": len(selected),
                    "available_attempts": available_attempts,
                    "selected_attempt": selected_attempt,
                    "result": "DUPLICATE",
                    "primary_failure_code": "controller_scenario_artifact_set_invalid",
                }
            )
            all_valid = False
            continue

        artifact_path = selected[0]
        evidence_path = artifact_path / "controller-lifecycle.json"
        evidence = _read_evidence(evidence_path)
        failure_code = None
        if evidence is None:
            failure_code = "controller_scenario_evidence_invalid"
        elif evidence.get("schema_version") != 2:
            failure_code = "controller_scenario_schema_version_invalid"
        elif evidence.get("scenario") != required:
            failure_code = "controller_scenario_identity_mismatch"
        elif evidence.get("source_revision") != source_sha:
            failure_code = "controller_scenario_source_revision_mismatch"
        else:
            runner = evidence.get("runner")
            if not isinstance(runner, dict) or any(
                runner.get(key) is not True
                for key in ("github_hosted", "windows_x64", "non_administrator", "clean_profile")
            ):
                failure_code = "controller_scenario_runner_preflight_invalid"
            elif evidence.get("result") != "PASS" or evidence.get("failure_code") is not None:
                failure_code = str(evidence.get("failure_code") or "controller_scenario_blocked")
            elif evidence.get("failure_codes") != []:
                failure_code = "controller_scenario_failure_codes_present"
            elif not isinstance(evidence.get("checks"), dict) or any(
                value is not True for value in evidence["checks"].values()
            ):
                failure_code = "controller_scenario_required_check_failed"

        result = "PASS" if failure_code is None else "BLOCKER"
        rows.append(
            {
                "scenario": required,
                "artifact_count": len(available),
                "available_attempts": available_attempts,
                "selected_attempt": selected_attempt,
                "result": result,
                "primary_failure_code": failure_code,
            }
        )
        destination = scenario_output / required
        if destination.exists():
            shutil.rmtree(destination)
        shutil.copytree(artifact_path, destination)
        if result != "PASS":
            all_valid = False

    manifest = {
        "schema_version": 1,
        "source_revision": source_sha,
        "required_scenarios": list(REQUIRED_SCENARIOS),
        "observed_scenarios": observed,
        "invalid_artifacts": invalid_artifacts,
        "scenarios": rows,
        "result": "PASS" if all_valid else "BLOCKER",
    }
    (output_root / "controller-acceptance-manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifact-root", type=Path, required=True)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    args = parser.parse_args()
    manifest = aggregate_scenarios(args.artifact_root, args.source_sha, args.output_root)
    print(f"Controller acceptance aggregate: {manifest['result']}")
    return 0 if manifest["result"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
