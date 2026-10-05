from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any, cast
from uuid import UUID

SCENARIOS = (
    "legacy_running_cutover",
    "legacy_ready_cutover",
    "legacy_disabled_desktop_start",
    "no_task_fail_closed",
    "identity_key_fail_closed",
    "duplicate_process_lock",
    "graceful_busy_drain_quit",
    "drain_failure_force_interrupt",
    "desktop_crash_logout_recovery",
    "rollback_legacy_task",
    "headed_chromium_profile_continuity",
)

SCENARIO_CHECKS: dict[str, tuple[str, ...]] = {
    "legacy_running_cutover": (
        "legacy_running_observed",
        "drain_post_once",
        "authoritative_offline",
        "legacy_natural_exit",
        "process_lock_released",
        "task_disabled_after_offline",
        "exact_desktop_worker_started",
        "same_identity_and_data_root",
        "desktop_online",
        "single_owner",
    ),
    "legacy_ready_cutover": (
        "legacy_ready_observed",
        "legacy_task_started_first",
        "drain_post_once",
        "authoritative_offline",
        "legacy_natural_exit",
        "process_lock_released",
        "task_disabled_after_offline",
        "exact_desktop_worker_started",
        "same_identity_and_data_root",
        "desktop_online",
        "single_owner",
    ),
    "legacy_disabled_desktop_start": (
        "task_disabled_observed",
        "enrolled_identity_unchanged",
        "process_lock_free_before_start",
        "exact_package_and_host_config",
        "desktop_online",
        "task_remains_disabled",
        "single_owner",
    ),
    "no_task_fail_closed": (
        "not_registered_diagnostic",
        "no_executable_discovery",
        "identity_unchanged",
        "no_task_created",
        "no_worker_spawned",
    ),
    "identity_key_fail_closed": (
        "missing_key_rejected",
        "corrupt_key_rejected",
        "mismatched_identity_rejected",
        "wrong_user_unprotect_rejected",
        "no_replacement_identity_or_key",
        "durable_data_unchanged",
        "no_worker_spawned",
    ),
    "duplicate_process_lock": (
        "existing_worker_lock_held",
        "desktop_start_blocked",
        "one_worker_process_only",
        "journal_unchanged",
        "no_second_owner",
    ),
    "graceful_busy_drain_quit": (
        "operator_session_retained_until_offline",
        "drain_post_once",
        "status_get_only_polling",
        "busy_draining_observed",
        "counts_and_quiescence_observed",
        "authoritative_offline",
        "worker_natural_exit",
        "chromium_natural_exit",
        "process_lock_released",
        "no_job_object_termination",
    ),
    "drain_failure_force_interrupt": (
        "graceful_failure_intervention",
        "no_automatic_force_fallback",
        "exact_force_phrase_required",
        "explicit_force_operation_invoked",
        "offline_not_fabricated",
        "drain_complete_not_fabricated",
        "legacy_task_remains_disabled",
        "identity_and_data_preserved",
        "process_tree_terminated",
        "forced_interruption_diagnostic",
        "operator_api_live_after_drain_failure",
        "force_authorization_rechecked",
    ),
    "desktop_crash_logout_recovery": (
        "desktop_parent_abnormal_exit",
        "captured_descendants_alive_at_crash",
        "job_object_reaped_worker_descendants",
        "disabled_task_binding_unchanged",
        "identity_key_root_unchanged",
        "journal_profile_preserved",
        "worker_reconciliation_observed",
        "no_duplicate_identity_tree",
        "not_reported_as_graceful_offline",
    ),
    "rollback_legacy_task": (
        "desktop_drain_post_once",
        "authoritative_offline",
        "desktop_natural_exit",
        "process_lock_released",
        "task_enabled_after_release",
        "legacy_started_after_enable",
        "same_worker_identity",
        "single_owner",
    ),
    "headed_chromium_profile_continuity": (
        "packaged_worker_browser_capability_executed",
        "chromium_started_headed",
        "not_session_zero_or_service",
        "worker_and_chromium_interactive_user",
        "desktop_hide_did_not_stop_worker",
        "worker_restart_requested",
        "restart_drain_post_once",
        "restart_authoritative_offline",
        "old_worker_natural_exit",
        "restart_process_lock_released",
        "legacy_task_disabled_through_restart",
        "new_worker_pid_after_restart",
        "restart_reused_identity_and_root",
        "restarted_worker_online",
        "second_profile_capability_executed",
        "profile_sentinel_survived_boundary",
        "profile_not_copied_or_reinitialized",
        "same_identity_across_boundary",
    ),
}

_SHA = re.compile(r"^[0-9a-f]{40}$")
_HEX_256 = re.compile(r"^[0-9a-f]{64}$")
_FAILURE_CODE = re.compile(r"^[a-z][a-z0-9_]{2,95}$")
_TASK_STATES = {"NOT_REGISTERED", "INVALID", "RUNNING", "READY", "DISABLED"}
_OWNERSHIPS = {"LEGACY", "TAKEOVER_REQUIRED", "DESKTOP", "BLOCKED"}
_WORKER_STATUSES = {
    "REGISTERING",
    "ONLINE",
    "DEGRADED",
    "DRAINING",
    "OFFLINE",
    "DISABLED",
    "UPGRADE_REQUIRED",
}
_LOCK_STATES = {"HELD", "NOT_HELD", "UNAVAILABLE"}
_FACT_FIELDS = {
    "worker_uuid",
    "identity_marker_sha256",
    "protected_key_sha256",
    "journal_sha256",
    "profile_tree_sha256",
    "profile_sentinel_sha256",
    "task_state_timeline",
    "ownership_timeline",
    "worker_status_timeline",
    "lock_observation_timeline",
    "process_ids",
    "drain_post_count",
    "drain_status_get_count",
    "operator_me_get_count",
    "worker_pid_timeline",
    "status_counts_timeline",
}


def _safe_stub(
    source_sha: str,
    scenario: str,
    failure_code: str,
    runner: dict[str, bool] | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "source_sha": source_sha,
        "scenario": scenario,
        "runner": runner
        or {
            "github_hosted": False,
            "windows_x64": False,
            "non_administrator": False,
            "fresh_profile": False,
            "fresh_worker_root": False,
        },
        "result": "BLOCKER",
        "failure_code": failure_code,
        "checks": {check: False for check in SCENARIO_CHECKS[scenario]},
        "facts": {
            "worker_uuid": None,
            "identity_marker_sha256": None,
            "protected_key_sha256": None,
            "journal_sha256": None,
            "profile_tree_sha256": None,
            "profile_sentinel_sha256": None,
            "task_state_timeline": [],
            "ownership_timeline": [],
            "worker_status_timeline": [],
            "lock_observation_timeline": [],
            "process_ids": {"desktop": None, "worker": None, "chromium": []},
            "drain_post_count": 0,
            "drain_status_get_count": 0,
            "operator_me_get_count": 0,
            "worker_pid_timeline": [],
            "status_counts_timeline": [],
        },
    }


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        value: object = json.loads(path.read_text(encoding="utf-8"))
    except OSError, json.JSONDecodeError:
        return None
    if not isinstance(value, dict):
        return None
    return cast(dict[str, Any], value)


def _valid_integer(value: object, *, minimum: int = 0) -> bool:
    return type(value) is int and value >= minimum


def _valid_timeline(value: object, allowed: set[str]) -> bool:
    if not isinstance(value, list):
        return False
    return all(isinstance(item, str) and item in allowed for item in cast(list[object], value))


def validate_evidence(value: object, source_sha: str, scenario: str) -> str | None:
    """Return a stable validation failure code, or None for structurally valid evidence."""
    if not isinstance(value, dict):
        return "worker_desktop_evidence_invalid"
    data = cast(dict[str, Any], value)
    if set(data) != {
        "schema_version",
        "source_sha",
        "scenario",
        "runner",
        "result",
        "failure_code",
        "checks",
        "facts",
    }:
        return "worker_desktop_evidence_fields_invalid"
    if type(data.get("schema_version")) is not int or data.get("schema_version") != 1:
        return "worker_desktop_evidence_schema_invalid"
    if data.get("source_sha") != source_sha or not _SHA.fullmatch(str(data.get("source_sha", ""))):
        return "worker_desktop_evidence_source_sha_mismatch"
    if data.get("scenario") != scenario:
        return "worker_desktop_evidence_scenario_mismatch"

    runner_value = data.get("runner")
    if not isinstance(runner_value, dict):
        return "worker_desktop_runner_evidence_invalid"
    runner = cast(dict[str, Any], runner_value)
    if set(runner) != {
        "github_hosted",
        "windows_x64",
        "non_administrator",
        "fresh_profile",
        "fresh_worker_root",
    }:
        return "worker_desktop_runner_evidence_invalid"
    if any(type(item) is not bool for item in runner.values()):
        return "worker_desktop_runner_evidence_invalid"

    checks_value = data.get("checks")
    if not isinstance(checks_value, dict):
        return "worker_desktop_scenario_checks_invalid"
    checks = cast(dict[str, Any], checks_value)
    if set(checks) != set(SCENARIO_CHECKS[scenario]):
        return "worker_desktop_scenario_checks_invalid"
    if any(type(result) is not bool for result in checks.values()):
        return "worker_desktop_scenario_checks_invalid"

    facts_value = data.get("facts")
    if not isinstance(facts_value, dict):
        return "worker_desktop_safe_facts_invalid"
    facts = cast(dict[str, Any], facts_value)
    if set(facts) != _FACT_FIELDS:
        return "worker_desktop_safe_facts_invalid"
    worker_uuid = facts.get("worker_uuid")
    if worker_uuid is not None:
        if not isinstance(worker_uuid, str):
            return "worker_desktop_safe_facts_invalid"
        try:
            if str(UUID(worker_uuid)) != worker_uuid:
                return "worker_desktop_safe_facts_invalid"
        except ValueError, TypeError, AttributeError:
            return "worker_desktop_safe_facts_invalid"
    for key in (
        "identity_marker_sha256",
        "protected_key_sha256",
        "journal_sha256",
        "profile_tree_sha256",
        "profile_sentinel_sha256",
    ):
        item = facts.get(key)
        if item is not None and (not isinstance(item, str) or not _HEX_256.fullmatch(item)):
            return "worker_desktop_safe_facts_invalid"
    if not _valid_timeline(facts.get("task_state_timeline"), _TASK_STATES):
        return "worker_desktop_safe_facts_invalid"
    if not _valid_timeline(facts.get("ownership_timeline"), _OWNERSHIPS):
        return "worker_desktop_safe_facts_invalid"
    if not _valid_timeline(facts.get("worker_status_timeline"), _WORKER_STATUSES):
        return "worker_desktop_safe_facts_invalid"
    if not _valid_timeline(facts.get("lock_observation_timeline"), _LOCK_STATES):
        return "worker_desktop_safe_facts_invalid"

    process_ids_value = facts.get("process_ids")
    if not isinstance(process_ids_value, dict):
        return "worker_desktop_safe_facts_invalid"
    process_ids = cast(dict[str, Any], process_ids_value)
    if set(process_ids) != {"desktop", "worker", "chromium"}:
        return "worker_desktop_safe_facts_invalid"
    chromium_pids = process_ids["chromium"]
    if not isinstance(chromium_pids, list):
        return "worker_desktop_safe_facts_invalid"
    if any(
        item is not None and not _valid_integer(item, minimum=1)
        for item in (process_ids["desktop"], process_ids["worker"])
    ) or any(not _valid_integer(pid, minimum=1) for pid in cast(list[object], chromium_pids)):
        return "worker_desktop_safe_facts_invalid"
    if (
        not _valid_integer(facts.get("drain_post_count"))
        or not _valid_integer(facts.get("drain_status_get_count"))
        or not _valid_integer(facts.get("operator_me_get_count"))
    ):
        return "worker_desktop_safe_facts_invalid"
    worker_pid_timeline = facts.get("worker_pid_timeline")
    if not isinstance(worker_pid_timeline, list) or any(
        not _valid_integer(pid, minimum=1) for pid in cast(list[object], worker_pid_timeline)
    ):
        return "worker_desktop_safe_facts_invalid"
    if scenario == "headed_chromium_profile_continuity" and data.get("result") == "PASS":
        if (
            len(worker_pid_timeline) != 2
            or worker_pid_timeline[0] == worker_pid_timeline[1]
            or facts.get("drain_post_count") != 1
        ):
            return "worker_desktop_profile_restart_boundary_invalid"
        worker_statuses = facts.get("worker_status_timeline")
        if not isinstance(worker_statuses, list):
            return "worker_desktop_profile_restart_boundary_invalid"
        statuses = cast(list[str], worker_statuses)
        try:
            draining_index = statuses.index("DRAINING")
            offline_index = statuses.index("OFFLINE", draining_index + 1)
            online_index = statuses.index("ONLINE", offline_index + 1)
        except ValueError:
            return "worker_desktop_profile_restart_boundary_invalid"
        if not draining_index < offline_index < online_index:
            return "worker_desktop_profile_restart_boundary_invalid"
    counts = facts.get("status_counts_timeline")
    if not isinstance(counts, list):
        return "worker_desktop_safe_facts_invalid"
    for item in cast(list[object], counts):
        if not isinstance(item, dict):
            return "worker_desktop_safe_facts_invalid"
        status_counts = cast(dict[str, Any], item)
        status = status_counts.get("status")
        if (
            set(status_counts)
            != {"status", "active_browser_sessions", "running_worker_jobs", "quiescent"}
            or not isinstance(status, str)
            or status not in _WORKER_STATUSES
            or not _valid_integer(status_counts.get("active_browser_sessions"))
            or not _valid_integer(status_counts.get("running_worker_jobs"))
            or type(status_counts.get("quiescent")) is not bool
        ):
            return "worker_desktop_safe_facts_invalid"

    result = data.get("result")
    if not isinstance(result, str) or result not in {"PASS", "BLOCKER"}:
        return "worker_desktop_result_invalid"
    failure_code = data.get("failure_code")
    if result == "PASS":
        if failure_code not in (None, "") or any(not result for result in checks.values()):
            return "worker_desktop_pass_evidence_invalid"
        if not all(runner.values()):
            return "worker_desktop_runner_evidence_incomplete"
    elif not isinstance(failure_code, str) or not _FAILURE_CODE.fullmatch(failure_code):
        return "worker_desktop_failure_code_invalid"
    return None


def aggregate_scenarios(artifact_root: Path, source_sha: str, output_root: Path) -> dict[str, Any]:
    if not _SHA.fullmatch(source_sha):
        raise ValueError("source_sha must be a lowercase 40-character commit hash")
    output_root.mkdir(parents=True, exist_ok=True)
    scenario_output = output_root / "worker-desktop-scenarios"
    scenario_output.mkdir(parents=True, exist_ok=True)

    prefix = f"dx07-worker-desktop-{source_sha}-"
    discovered = (
        sorted(
            (
                path
                for path in artifact_root.iterdir()
                if path.is_dir() and path.name.startswith(prefix)
            ),
            key=lambda path: path.name,
        )
        if artifact_root.exists()
        else []
    )
    discovered_names = [path.name[len(prefix) :] for path in discovered]
    observed = [name for name in SCENARIOS if name in discovered_names] + sorted(
        set(discovered_names) - set(SCENARIOS)
    )
    evidence_rows: list[tuple[str, Path, dict[str, Any] | None]] = []
    actual_names: list[str] = []
    for path in discovered:
        suffix = path.name[len(prefix) :]
        evidence = _read_json(path / "worker-desktop.json")
        scenario_name = evidence.get("scenario") if evidence is not None else suffix
        if not isinstance(scenario_name, str):
            scenario_name = suffix
        evidence_rows.append((scenario_name, path, evidence))
        actual_names.append(scenario_name)

    scenario_counts = {name: actual_names.count(name) for name in set(actual_names)}
    rows: list[dict[str, Any]] = []
    aggregate_valid = len(observed) == len(SCENARIOS) and set(observed) == set(SCENARIOS)
    for scenario in SCENARIOS:
        matching = [
            (name, path, evidence) for name, path, evidence in evidence_rows if name == scenario
        ]
        failure_code: str | None = None
        valid_evidence: dict[str, Any] | None = None
        if len(matching) == 0:
            failure_code = "worker_desktop_scenario_evidence_missing"
        elif len(matching) > 1:
            failure_code = "worker_desktop_scenario_evidence_duplicated"
        else:
            name, artifact_path, evidence = matching[0]
            if artifact_path.name != f"{prefix}{scenario}" or name != scenario:
                failure_code = "worker_desktop_scenario_artifact_name_invalid"
            elif evidence is None:
                failure_code = "worker_desktop_evidence_invalid"
            else:
                failure_code = validate_evidence(evidence, source_sha, scenario)
                if failure_code is None and evidence["result"] != "PASS":
                    failure_code = str(evidence["failure_code"])
                    valid_evidence = evidence
                elif failure_code is None:
                    valid_evidence = evidence
        if scenario_counts.get(scenario, 0) != 1:
            aggregate_valid = False
        result = "PASS" if failure_code is None else "BLOCKER"
        rows.append(
            {
                "scenario": scenario,
                "artifact_count": len(matching),
                "result": result,
                "failure_code": failure_code,
            }
        )
        destination = scenario_output / scenario
        destination.mkdir(parents=True, exist_ok=True)
        evidence_destination = destination / "worker-desktop.json"
        if valid_evidence is not None:
            evidence_destination.write_text(
                json.dumps(valid_evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
        else:
            safe_failure = failure_code or "worker_desktop_scenario_evidence_invalid"
            evidence_destination.write_text(
                json.dumps(_safe_stub(source_sha, scenario, safe_failure), indent=2, sort_keys=True)
                + "\n",
                encoding="utf-8",
            )
        if result != "PASS":
            aggregate_valid = False

    unexpected = sorted(set(observed) - set(SCENARIOS))
    if unexpected or any(count != 1 for count in scenario_counts.values()):
        aggregate_valid = False
    manifest = {
        "schema_version": 1,
        "source_sha": source_sha,
        "required_scenarios": list(SCENARIOS),
        "observed_artifact_scenarios": observed,
        "unexpected_scenarios": unexpected,
        "scenarios": rows,
        "result": "PASS" if aggregate_valid else "BLOCKER",
    }
    (output_root / "worker-desktop-acceptance-manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--artifact-root", type=Path)
    mode.add_argument("--write-blocker-evidence", type=Path)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--output-root", type=Path)
    parser.add_argument("--scenario", choices=SCENARIOS)
    parser.add_argument("--failure-code")
    args = parser.parse_args()
    if args.write_blocker_evidence is not None:
        if (
            not _SHA.fullmatch(args.source_sha)
            or args.scenario is None
            or args.failure_code is None
            or not _FAILURE_CODE.fullmatch(args.failure_code)
        ):
            print("worker_desktop_blocker_input_invalid")
            return 2
        args.write_blocker_evidence.parent.mkdir(parents=True, exist_ok=True)
        args.write_blocker_evidence.write_text(
            json.dumps(
                _safe_stub(args.source_sha, args.scenario, args.failure_code),
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        print(args.failure_code)
        return 0
    if args.output_root is None:
        print("worker_desktop_aggregate_input_invalid")
        return 2
    try:
        if args.artifact_root is None:
            print("worker_desktop_aggregate_input_invalid")
            return 2
        manifest = aggregate_scenarios(args.artifact_root, args.source_sha, args.output_root)
    except ValueError:
        print("worker_desktop_aggregate_input_invalid")
        return 2
    print(f"Worker Desktop acceptance aggregate: {manifest['result']}")
    return 0 if manifest["result"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
