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
    except OSError, json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def aggregate_scenarios(artifact_root: Path, source_sha: str, output_root: Path) -> dict[str, Any]:
    output_root.mkdir(parents=True, exist_ok=True)
    scenario_output = output_root / "controller-scenarios"
    scenario_output.mkdir(parents=True, exist_ok=True)

    discovered: list[tuple[str, Path]] = []
    if artifact_root.exists():
        for artifact in sorted(path for path in artifact_root.iterdir() if path.is_dir()):
            prefix = f"dx04-evidence-{source_sha}-"
            if artifact.name.startswith(prefix):
                discovered.append((artifact.name[len(prefix) :], artifact))

    observed = [scenario for scenario, _ in discovered]
    counts = {scenario: observed.count(scenario) for scenario in set(observed)}
    rows: list[dict[str, Any]] = []
    all_valid = len(observed) == len(REQUIRED_SCENARIOS) and set(observed) == set(
        REQUIRED_SCENARIOS
    )

    for required in REQUIRED_SCENARIOS:
        matching = [(scenario, path) for scenario, path in discovered if scenario == required]
        if len(matching) != 1:
            rows.append(
                {
                    "scenario": required,
                    "artifact_count": len(matching),
                    "result": "MISSING" if not matching else "DUPLICATE",
                    "primary_failure_code": "controller_scenario_artifact_set_invalid",
                }
            )
            all_valid = False
            continue

        _scenario, artifact_path = matching[0]
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
                "artifact_count": 1,
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

    if any(count != 1 for count in counts.values()):
        all_valid = False
    if set(observed) != set(REQUIRED_SCENARIOS):
        all_valid = False

    manifest = {
        "schema_version": 1,
        "source_revision": source_sha,
        "required_scenarios": list(REQUIRED_SCENARIOS),
        "observed_scenarios": observed,
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
