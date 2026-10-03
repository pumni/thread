from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _text(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_heavy_pr_acceptance_runs_only_at_ready_boundaries() -> None:
    for path in (
        ".github/workflows/ci.yml",
        ".github/workflows/docker-control-plane-smoke.yml",
        ".github/workflows/desktop.yml",
    ):
        workflow = _text(path)
        assert "types: [ready_for_review, reopened]" in workflow
        assert "github.event.pull_request.draft == false" in workflow
        assert "synchronize" not in workflow.split("push:", 1)[0]


def test_secret_scan_enforces_ready_head_discipline() -> None:
    workflow = _text(".github/workflows/secret-scan.yml")
    assert "types: [opened, synchronize, reopened, ready_for_review]" in workflow
    assert "Reject head changes while Ready for Review" in workflow
    assert "github.event.action == 'synchronize'" in workflow
    assert "github.event.pull_request.draft == false" in workflow


def test_agent_and_windows_preflight_contract_is_repository_visible() -> None:
    agents = _text("AGENTS.md")
    process = _text("docs/CI_AGENT_WORKFLOW.md")
    preflight = _text("scripts/windows_local_preflight.ps1")

    assert "docs/CI_AGENT_WORKFLOW.md" in agents
    assert "two consecutive hosted attempts" in agents
    assert "hosted failure is a hard stop" in process.lower()
    assert "scripts/windows_local_preflight.ps1" in process
    assert "THREADS_PLATFORM_TEST_DATABASE_URL" in preflight
    assert "--basetemp=" in preflight
    assert "SpecialFolder]::LocalApplicationData" in preflight
    assert 'Join-Path $repoRoot "build/local-ci/' not in preflight
    assert "windows_local_preflight_requires_bundled_postgresql" in preflight
