from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import pytest
import yaml  # type: ignore[reportMissingTypeStubs]

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_ROOT = ROOT / ".github" / "workflows"
REUSABLE_COMPONENTS = (
    ".github/workflows/ci.yml",
    ".github/workflows/docker-control-plane-smoke.yml",
    ".github/workflows/desktop.yml",
    ".github/workflows/windows-worker-package.yml",
)
ACCEPTANCE_WORKFLOWS = (
    ".github/workflows/ci.yml",
    ".github/workflows/docker-control-plane-smoke.yml",
    ".github/workflows/desktop.yml",
    ".github/workflows/windows-worker-package.yml",
    ".github/workflows/secret-scan.yml",
    ".github/workflows/pr-head-guard.yml",
    ".github/workflows/pr-acceptance.yml",
    ".github/workflows/main-verification.yml",
)
PINNED_ACTIONS = {
    "actions/checkout": "3d3c42e5aac5ba805825da76410c181273ba90b1",
    "astral-sh/setup-uv": "c18668ad3cf93ea998bef934396af7bb5c839dc7",
    "oven-sh/setup-bun": "0c5077e51419868618aeaa5fe8019c62421857d6",
    "dtolnay/rust-toolchain": "89b12181fb390509a0842a86cc55eeb8eb928c1d",
    "actions/cache": "55cc8345863c7cc4c66a329aec7e433d2d1c52a9",
    "actions/upload-artifact": "043fb46d1a93c77aae656e7c1c64a875d1fc6a0a",
    "actions/download-artifact": "3e5f45b2cfb9172054b4087a40e8e0b5a5461e7c",
}


def _text(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def _workflow(path: str) -> dict[str, Any]:
    parsed = yaml.load(_text(path), Loader=yaml.BaseLoader)
    assert isinstance(parsed, dict)
    return cast(dict[str, Any], parsed)


def _walk(value: object) -> Iterator[dict[str, Any]]:
    if isinstance(value, dict):
        mapping = cast(dict[str, Any], value)
        yield mapping
        for child in mapping.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in cast(list[object], value):
            yield from _walk(child)


def _jobs(workflow: dict[str, Any]) -> dict[str, Any]:
    jobs = workflow.get("jobs")
    assert isinstance(jobs, dict)
    return cast(dict[str, Any], jobs)


def _checkout_steps(workflow: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        node for node in _walk(workflow) if node.get("uses", "").startswith("actions/checkout@")
    ]


def test_ready_and_main_each_have_one_authoritative_workflow() -> None:
    ready_workflows: list[str] = []
    main_push_workflows: list[str] = []
    for path in sorted(WORKFLOW_ROOT.glob("*.yml")):
        workflow = _workflow(str(path.relative_to(ROOT)))
        triggers = workflow.get("on", {})
        if not isinstance(triggers, dict):
            continue
        trigger_map = cast(dict[str, object], triggers)
        pull_request = trigger_map.get("pull_request", {})
        if isinstance(pull_request, dict):
            pull_request_types = cast(dict[str, object], pull_request).get("types", [])
            if isinstance(pull_request_types, list) and "ready_for_review" in pull_request_types:
                ready_workflows.append(path.name)
        push = trigger_map.get("push", {})
        if isinstance(push, dict):
            push_branches = cast(dict[str, object], push).get("branches", [])
            if isinstance(push_branches, list) and "main" in push_branches:
                main_push_workflows.append(path.name)

    assert ready_workflows == ["pr-acceptance.yml"]
    assert main_push_workflows == ["main-verification.yml"]

    pr = _workflow(".github/workflows/pr-acceptance.yml")
    assert pr["on"] == {"pull_request": {"types": ["ready_for_review"]}}
    assert pr["name"] == "PR Acceptance"
    assert _jobs(pr)["acceptance-gate"]["name"] == "PR Acceptance"
    assert _jobs(pr)["acceptance-gate"]["if"] == "always()"
    assert "github.event.pull_request.number" in pr["concurrency"]["group"]

    main = _workflow(".github/workflows/main-verification.yml")
    assert main["on"] == {"push": {"branches": ["main"]}}
    assert _jobs(main)["main-verification-gate"]["name"] == "Main Verification"
    assert _jobs(main)["main-verification-gate"]["if"] == "always()"
    assert main["concurrency"]["cancel-in-progress"] == "false"


def test_heavy_components_are_reusable_and_exact_sha_bound() -> None:
    for path in REUSABLE_COMPONENTS:
        workflow = _workflow(path)
        triggers = workflow["on"]
        expected_triggers = (
            {"workflow_call"}
            if path
            in {
                ".github/workflows/ci.yml",
                ".github/workflows/docker-control-plane-smoke.yml",
            }
            else {"workflow_call", "workflow_dispatch"}
        )
        assert set(triggers) == expected_triggers
        assert "workflow_call" in triggers
        assert "pull_request" not in triggers
        assert "push" not in triggers
        source_sha = triggers["workflow_call"]["inputs"]["source_sha"]
        assert source_sha["required"] == "true"
        assert source_sha["type"] == "string"

        checkout_steps = _checkout_steps(workflow)
        assert checkout_steps, path
        for step in checkout_steps:
            checkout = step["with"]
            assert checkout["ref"] == "${{ inputs.source_sha }}", (path, checkout)
            assert checkout["persist-credentials"] == "false", (path, checkout)
        for job in _jobs(workflow).values():
            steps = job.get("steps", [])
            if not isinstance(steps, list):
                continue
            step_list = cast(list[dict[str, Any]], steps)
            for index, step in enumerate(step_list):
                if step.get("uses", "").startswith("actions/checkout@"):
                    assert any(
                        later.get("name", "").startswith("Assert exact")
                        for later in step_list[index + 1 :]
                    ), (path, job)

    desktop = _workflow(".github/workflows/desktop.yml")
    assert set(desktop["on"]) == {"workflow_call", "workflow_dispatch"}
    assert desktop["on"]["workflow_dispatch"]["inputs"]["source_sha"]["required"] == "true"
    assert desktop["on"]["workflow_call"]["inputs"]["runtime_layout"]["default"] == "shared"

    worker = _workflow(".github/workflows/windows-worker-package.yml")
    assert set(worker["on"]) == {"workflow_call", "workflow_dispatch"}
    assert worker["on"]["workflow_dispatch"]["inputs"]["source_sha"]["required"] == "true"
    assert "${{ inputs.source_sha }}" in _text(".github/workflows/windows-worker-package.yml")


def test_docker_smoke_uses_locked_project_environment() -> None:
    workflow = _workflow(".github/workflows/docker-control-plane-smoke.yml")
    steps = cast(
        list[dict[str, Any]],
        _jobs(workflow)["compose-restart-smoke"]["steps"],
    )
    sync_step = next(
        step for step in steps if step.get("name") == "Install locked smoke environment"
    )
    smoke_step = next(
        step
        for step in steps
        if step.get("name") == "Build image and run Compose restart/recovery smoke"
    )
    cleanup_step = next(
        step
        for step in steps
        if step.get("name") == "Remove smoke containers and PostgreSQL volume"
    )

    assert sync_step["run"] == "uv sync --locked"
    assert smoke_step["run"] == "uv run --locked python scripts/control_plane_compose_smoke.py"
    assert "--no-project" not in smoke_step["run"]
    assert cleanup_step["run"] == (
        "docker compose --profile tls-admin down --volumes --remove-orphans"
    )


def test_secret_scan_and_head_guard_are_cheap_draft_gates() -> None:
    secret = _workflow(".github/workflows/secret-scan.yml")
    assert set(secret["on"]) == {"pull_request", "schedule", "workflow_dispatch"}
    assert secret["on"]["pull_request"]["types"] == ["opened", "synchronize", "reopened"]
    assert _jobs(secret).keys() == {"gitleaks"}
    assert any(step.get("uses") == "./.github/actions/gitleaks" for step in _walk(secret))

    guard = _workflow(".github/workflows/pr-head-guard.yml")
    assert guard["on"]["pull_request"]["types"] == [
        "opened",
        "synchronize",
        "reopened",
        "converted_to_draft",
    ]
    guard_text = _text(".github/workflows/pr-head-guard.yml")
    assert "ACTIONLINT_VERSION: 1.7.12" in guard_text
    assert "sha256sum --check --status" in guard_text
    assert "actionlint" in guard_text and ".github/workflows/*.yml" in guard_text
    assert "github.event.action == 'synchronize'" in guard_text
    assert "github.event.pull_request.draft == false" in guard_text


def test_gitleaks_composite_verifies_pinned_release_and_redacts_both_scans() -> None:
    action = cast(
        dict[str, Any],
        yaml.load(_text(".github/actions/gitleaks/action.yml"), Loader=yaml.BaseLoader),
    )
    assert action["runs"]["using"] == "composite"
    assert set(action["inputs"]) == {"base_sha", "head_sha", "scan_commit_range"}
    action_text = _text(".github/actions/gitleaks/action.yml")
    assert "GITLEAKS_VERSION: 8.30.1" in action_text
    assert "gitleaks_${GITLEAKS_VERSION}_checksums.txt" in action_text
    assert "sha256sum --check --status" in action_text
    assert '"${RUNNER_TEMP}/gitleaks-bin/gitleaks" dir' in action_text
    assert '"${RUNNER_TEMP}/gitleaks-bin/gitleaks" git' in action_text
    assert action_text.count("--redact") == 2


def test_orchestrations_call_shared_components_and_gate_heavy_paths() -> None:
    expected_calls = {
        "python-quality": "./.github/workflows/ci.yml",
        "docker-control-plane": "./.github/workflows/docker-control-plane-smoke.yml",
        "desktop": "./.github/workflows/desktop.yml",
    }
    for path, gate_id in (
        (".github/workflows/pr-acceptance.yml", "acceptance-gate"),
        (".github/workflows/main-verification.yml", "main-verification-gate"),
    ):
        workflow = _workflow(path)
        jobs = _jobs(workflow)
        for job_id, reusable_path in expected_calls.items():
            assert jobs[job_id]["uses"] == reusable_path
            assert jobs[job_id]["with"]["source_sha"] in {
                "${{ github.event.pull_request.head.sha }}",
                "${{ github.sha }}",
            }
        changes = jobs["changes"]
        assert changes["outputs"] == {
            "worker": "${{ steps.classify.outputs.worker }}",
            "desktop": "${{ steps.classify.outputs.desktop }}",
        }
        worker = jobs["worker-package"]
        assert worker["uses"] == "./.github/workflows/windows-worker-package.yml"
        assert "needs.changes.outputs.worker == 'true'" in worker["if"]
        assert "changes" in worker["needs"]
        desktop = jobs["desktop"]
        assert desktop["if"] == "${{ needs.changes.outputs.desktop == 'true' }}"
        assert "changes" in desktop["needs"]
        for mandatory_job in ("python-quality", "docker-control-plane"):
            assert "if" not in jobs[mandatory_job]
        gate = jobs[gate_id]
        assert set(gate["needs"]) >= {
            "changes",
            "python-quality",
            "docker-control-plane",
            "desktop",
            "worker-package",
        }
        gate_run = gate["steps"][0]["run"]
        gate_env = gate["steps"][0]["env"]
        assert gate_env["DESKTOP_NEEDED"] == "${{ needs.changes.outputs.desktop }}"
        assert '[[ "$DESKTOP_NEEDED" == "true" ]]' in gate_run
        assert '[[ "$DESKTOP_NEEDED" == "false" ]]' in gate_run
        assert "desktop_component_required_but_not_successful" in gate_run
        assert "desktop_component_should_be_skipped" in gate_run
        assert "worker_component_required_but_not_successful" in gate_run
        assert "worker_component_should_be_skipped" in gate_run
        mandatory_results = (
            'for result in "$PREFLIGHT_RESULT" "$CHANGES_RESULT" '
            '"$PYTHON_RESULT" "$DOCKER_RESULT"; do'
            if gate_id == "acceptance-gate"
            else 'for result in "$CHANGES_RESULT" "$PYTHON_RESULT" '
            '"$DOCKER_RESULT" "$SECRET_RESULT"; do'
        )
        assert mandatory_results in gate_run

    main = _workflow(".github/workflows/main-verification.yml")
    main_secret = _jobs(main)["main-secret-scan"]
    assert main_secret["runs-on"] == "ubuntu-24.04"
    main_range = next(step for step in main_secret["steps"] if step.get("id") == "scan-range")
    assert "git cat-file -e" in main_range["run"]
    assert '"scan=false"' in main_range["run"]
    assert any(step.get("uses") == "./.github/actions/gitleaks" for step in _walk(main_secret))
    gitleaks_call = next(
        step for step in main_secret["steps"] if step.get("uses") == "./.github/actions/gitleaks"
    )
    assert gitleaks_call["with"]["scan_commit_range"] == "${{ steps.scan-range.outputs.scan }}"

    pr = _workflow(".github/workflows/pr-acceptance.yml")
    pr_changes = _jobs(pr)["changes"]
    pr_checkout = next(
        step for step in pr_changes["steps"] if step.get("uses", "").startswith("actions/checkout@")
    )
    assert pr_checkout["with"]["ref"] == "${{ github.event.pull_request.head.sha }}"
    assert pr_checkout["with"]["persist-credentials"] == "false"
    evidence_run = _jobs(pr)["preflight-evidence"]["steps"][0]["run"]
    assert "gh api --paginate" in evidence_run
    assert "head_sha=${SOURCE_SHA}" in evidence_run
    assert 'event == "pull_request"' in evidence_run
    assert "sort_by([.created_at, .id]) | last" in evidence_run
    assert 'conclusion" != "success"' in evidence_run
    pr_gate_run = _jobs(pr)["acceptance-gate"]["steps"][0]["run"]
    assert "worker_component_required_but_not_successful" in pr_gate_run
    assert "worker_component_should_be_skipped" in pr_gate_run

    main_changes = _jobs(main)["changes"]
    main_checkout = next(
        step
        for step in main_changes["steps"]
        if step.get("uses", "").startswith("actions/checkout@")
    )
    assert main_checkout["with"]["ref"] == "${{ github.sha }}"
    assert main_checkout["with"]["persist-credentials"] == "false"


def test_action_pins_runners_and_top_level_names_are_canonical() -> None:
    names: set[str] = set()
    for path in ACCEPTANCE_WORKFLOWS:
        workflow = _workflow(path)
        names.add(workflow["name"])
        text = _text(path)
        assert "ubuntu-latest" not in text, path
        assert "windows-latest" not in text, path
        for node in _walk(workflow):
            runner = node.get("runs-on")
            if runner is not None:
                assert runner in {"ubuntu-24.04", "windows-2025"}, (path, runner)
            action = node.get("uses", "")
            for action_name, pin in PINNED_ACTIONS.items():
                if action.startswith(f"{action_name}@"):
                    assert action == f"{action_name}@{pin}", (path, action)
                    if action_name == "astral-sh/setup-uv":
                        assert node["with"]["version"] == "0.12.21", (path, node)
                    if action_name == "oven-sh/setup-bun":
                        assert node["with"]["bun-version"] == "1.4.2", (path, node)

    assert "CI" not in names
    assert "Desktop CI" not in names
    assert _workflow(".github/workflows/ci.yml")["name"] == "Python Quality Component"
    assert _workflow(".github/workflows/desktop.yml")["name"] == "Desktop Diagnostic"


def test_preflight_cheap_gate_evidence_is_scoped_to_current_pr() -> None:
    pr = _workflow(".github/workflows/pr-acceptance.yml")
    evidence_step = _jobs(pr)["preflight-evidence"]["steps"][0]
    evidence_run = evidence_step["run"]

    assert evidence_step["env"]["PR_NUMBER"] == "${{ github.event.pull_request.number }}"
    assert '--argjson pr "$PR_NUMBER"' in evidence_run
    assert "any(.pull_requests[]?; .number == $pr)" in evidence_run
    assert 'event == "pull_request"' in evidence_run
    assert "head_sha == $sha" in evidence_run
    assert "name == $name" in evidence_run
    assert "pr=${PR_NUMBER}" in evidence_run


def test_ci_process_docs_describe_the_single_acceptance_gate() -> None:
    agents = _text("AGENTS.md")
    process = _text("docs/CI_AGENT_WORKFLOW.md")
    assert "PR Acceptance" in process and "Main Verification" in process
    assert "CI_AGENT_WORKFLOW.md" in agents
    for phrase in (
        "Secret scan + PR Head Guard only",
        "single merge-authoritative",
        "latest exact-head",
        "source_sha",
        "Main Verification",
        "Reopened PRs do NOT automatically launch heavy acceptance",
    ):
        assert phrase in process
    assert "hosted failure is a hard stop" in process.lower()
    assert "scripts/windows_local_preflight.ps1" in process


def test_agent_and_windows_preflight_contract_remains_repository_visible() -> None:
    preflight = _text("scripts/windows_local_preflight.ps1")
    assert "two consecutive hosted attempts" in _text("docs/CI_AGENT_WORKFLOW.md")
    assert "THREADS_PLATFORM_TEST_DATABASE_URL" in preflight
    assert "--basetemp=" in preflight
    assert "SpecialFolder]::LocalApplicationData" in preflight
    assert 'Join-Path $localAppData "TOCI/$shortRunId"' in preflight
    assert 'Join-Path $repoRoot "build/local-ci/' not in preflight
    assert "retrying immediate stop" in preflight
    assert "windows_local_preflight_postgres_cleanup_failed" in preflight
    assert "windows_local_preflight_requires_bundled_postgresql" in preflight


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        (".github/workflows/pr-acceptance.yml", "PR Acceptance"),
        (".github/workflows/main-verification.yml", "Main Verification"),
    ],
)
def test_only_aggregate_job_uses_authoritative_check_name(path: str, expected: str) -> None:
    workflow = _workflow(path)
    jobs = _jobs(workflow)
    matching = [job_id for job_id, job in jobs.items() if job.get("name") == expected]
    assert len(matching) == 1
    assert jobs[matching[0]]["if"] == "always()"
