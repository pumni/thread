from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path
from typing import Any

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "ci_changed_paths.py"
SPEC = importlib.util.spec_from_file_location("ci_changed_paths", SCRIPT)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("Could not load the changed-path classifier")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
ci_changed_paths: Any = MODULE

HEAD_SHA = "a" * 40
BASE_SHA = "b" * 40


def test_worker_path_classifier_covers_required_paths() -> None:
    paths = (
        ".github/workflows/windows-worker-package.yml",
        ".github/workflows/worker-desktop-acceptance.yml",
        ".github/workflows/pr-acceptance.yml",
        ".github/workflows/main-verification.yml",
        "scripts/ci_changed_paths.py",
        "tests/unit/test_ci_changed_paths.py",
        "pyproject.toml",
        "uv.lock",
        "packaging/windows_worker/build_package.py",
        "src/threads_platform/workers/agent.py",
        "src/threads_platform/infrastructure/worker_agent/client.py",
        "src/threads_platform/infrastructure/browser/adapter.py",
        "tests/unit/test_worker_package.py",
        "tests/unit/test_worker_host_config.py",
        "tests/unit/test_worker_control_client_httpx2.py",
        "tests/unit/test_worker_control_client_cancellation.py",
        "tests/unit/test_threads_httpx2_transport.py",
        "tests/unit/test_browser_adapter.py",
        "tests/unit/test_ci_acceptance_workflow.py",
        "tests/unit/test_worker_desktop_acceptance.py",
        "apps/desktop/src-tauri/tauri.conf.json",
        "apps/desktop/src-tauri/src/lib.rs",
        "apps/desktop/src-tauri/src/operator_client.rs",
        "apps/desktop/src-tauri/src/supervisor.rs",
        "apps/desktop/src-tauri/src/windows_crypto.rs",
        "apps/desktop/src-tauri/src/worker_cutover.rs",
        "apps/desktop/src-tauri/src/worker_host.rs",
        "apps/desktop/src/App.tsx",
        "apps/desktop/src/desktop.ts",
        "apps/desktop/tests/desktop.test.tsx",
        "packaging/windows_desktop/Stage-WorkerDesktopTestArtifact.ps1",
        "packaging/windows_desktop/aggregate_worker_desktop_scenarios.py",
        "packaging/windows_desktop/smoke_worker_desktop_lifecycle.ps1",
        "packaging/windows_desktop/verify_worker_desktop_inputs.py",
    )

    for path in paths:
        assert ci_changed_paths.worker_package_relevant([path]), path
        assert ci_changed_paths.desktop_diagnostic_relevant([path]), path


def test_worker_desktop_paths_are_worker_relevant_without_broad_desktop_prefix() -> None:
    assert ci_changed_paths.worker_package_relevant(
        ["packaging/windows_desktop/smoke_worker_desktop_lifecycle.ps1"]
    )
    assert not ci_changed_paths.worker_package_relevant(
        ["packaging/windows_desktop/smoke_controller_lifecycle.ps1"]
    )
    assert not ci_changed_paths.worker_package_relevant(["packaging/windows_desktop/README.md"])


def test_worker_desktop_paths_select_both_heavy_components(monkeypatch: pytest.MonkeyPatch) -> None:
    worker_paths = [
        "apps/desktop/src-tauri/src/worker_host.rs",
        "apps/desktop/src-tauri/src/worker_cutover.rs",
        "apps/desktop/src-tauri/src/operator_client.rs",
        "apps/desktop/src/App.tsx",
        "packaging/windows_desktop/smoke_worker_desktop_lifecycle.ps1",
        ".github/workflows/worker-desktop-acceptance.yml",
    ]

    for path in worker_paths:

        def changed_paths(
            _mode: str, _base_sha: str, _head_sha: str, *, changed_path: str = path
        ) -> list[str]:
            return [changed_path]

        monkeypatch.setattr(ci_changed_paths, "_changed_paths", changed_paths)
        assert ci_changed_paths.classify_changes("main", BASE_SHA, HEAD_SHA) == (True, True), path


def test_readme_and_docs_only_changes_do_not_run_worker_gates() -> None:
    assert not ci_changed_paths.worker_package_relevant(
        ["README.md", "docs/ARCHITECTURE.md", "docs/WORKER_AGENT_WINDOWS.md"]
    )


@pytest.mark.parametrize(
    ("paths", "expected"),
    [
        (["docs/ARCHITECTURE.md"], False),
        (["AGENTS.md"], False),
        (["README.md"], False),
        ([".agents/skills/ci/SKILL.md"], False),
        ([".github/ISSUE_TEMPLATE/bug.yml"], False),
        ([".github/PULL_REQUEST_TEMPLATE.md"], False),
        (["src/threads_platform/standalone/runtime.py"], False),
        (["tests/unit/test_standalone_runtime.py"], False),
        (["apps/desktop/src/App.tsx"], True),
        (["apps/desktop/src-tauri/src/worker_host.rs"], True),
        (["packaging/windows_desktop/smoke_worker_desktop_lifecycle.ps1"], True),
        ([".github/workflows/worker-desktop-acceptance.yml"], True),
    ],
)
def test_desktop_path_classifier_preserves_current_main_skip_policy(
    paths: list[str], expected: bool
) -> None:
    assert ci_changed_paths.desktop_diagnostic_relevant(paths) is expected


def test_pull_request_uses_merge_base_and_exact_head(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, ...]] = []

    def git_output(*arguments: str) -> str:
        calls.append(arguments)
        if arguments == ("rev-parse", "HEAD"):
            return HEAD_SHA
        if arguments == ("merge-base", BASE_SHA, HEAD_SHA):
            return BASE_SHA
        if arguments == ("diff", "--name-only", "--no-renames", "-z", f"{BASE_SHA}..{HEAD_SHA}"):
            return "docs/only.md\x00packaging/windows_worker/build_package.py\x00"
        raise AssertionError(arguments)

    monkeypatch.setattr(ci_changed_paths, "_git_output", git_output)

    assert ci_changed_paths.classify_worker_change("pull_request", BASE_SHA, HEAD_SHA)
    assert calls[1] == ("merge-base", BASE_SHA, HEAD_SHA)


def test_main_zero_before_sha_fails_open_without_git(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def unexpected_git(*arguments: str) -> str:
        raise AssertionError(arguments)

    monkeypatch.setattr(ci_changed_paths, "_git_output", unexpected_git)

    assert ci_changed_paths.classify_worker_change("main", ci_changed_paths.ZERO_SHA, HEAD_SHA)
    assert "enabling Worker gates" in capsys.readouterr().err
    assert ci_changed_paths.classify_desktop_change("main", ci_changed_paths.ZERO_SHA, HEAD_SHA)


def test_main_uses_before_to_exact_head_range(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, ...]] = []

    def git_output(*arguments: str) -> str:
        calls.append(arguments)
        if arguments == ("rev-parse", "HEAD"):
            return HEAD_SHA
        if arguments == ("diff", "--name-only", "--no-renames", "-z", f"{BASE_SHA}..{HEAD_SHA}"):
            return "docs/only.md\x00"
        raise AssertionError(arguments)

    monkeypatch.setattr(ci_changed_paths, "_git_output", git_output)

    assert not ci_changed_paths.classify_worker_change("main", BASE_SHA, HEAD_SHA)
    assert calls[1] == ("diff", "--name-only", "--no-renames", "-z", f"{BASE_SHA}..{HEAD_SHA}")


def test_main_desktop_classification_uses_before_to_exact_head_range(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, ...]] = []

    def git_output(*arguments: str) -> str:
        calls.append(arguments)
        if arguments == ("rev-parse", "HEAD"):
            return HEAD_SHA
        if arguments == ("diff", "--name-only", "--no-renames", "-z", f"{BASE_SHA}..{HEAD_SHA}"):
            return "docs/only.md\x00"
        raise AssertionError(arguments)

    monkeypatch.setattr(ci_changed_paths, "_git_output", git_output)

    assert not ci_changed_paths.classify_desktop_change("main", BASE_SHA, HEAD_SHA)
    assert calls[1] == ("diff", "--name-only", "--no-renames", "-z", f"{BASE_SHA}..{HEAD_SHA}")


def test_unresolvable_main_range_fails_open(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def git_output(*arguments: str) -> str:
        if arguments == ("rev-parse", "HEAD"):
            return HEAD_SHA
        raise subprocess.CalledProcessError(128, arguments)

    monkeypatch.setattr(ci_changed_paths, "_git_output", git_output)

    assert ci_changed_paths.classify_worker_change("main", BASE_SHA, HEAD_SHA)
    assert "enabling Worker gates" in capsys.readouterr().err
    assert ci_changed_paths.classify_changes("main", BASE_SHA, HEAD_SHA) == (True, True)


def test_cli_appends_worker_result_to_github_output(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    output_path = tmp_path / "github-output"
    output_path.write_text("existing=value\n", encoding="utf-8")
    monkeypatch.setenv("GITHUB_OUTPUT", str(output_path))

    def readme_only_changes(_mode: str, _base_sha: str, _head_sha: str) -> list[str]:
        return ["README.md"]

    monkeypatch.setattr(ci_changed_paths, "_changed_paths", readme_only_changes)

    assert (
        ci_changed_paths.main(["--mode", "main", "--base-sha", BASE_SHA, "--head-sha", HEAD_SHA])
        == 0
    )
    assert output_path.read_text(encoding="utf-8") == (
        "existing=value\nworker=false\ndesktop=false\n"
    )
