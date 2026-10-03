"""Classify whether a source change requires the Windows Worker gates."""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from collections.abc import Iterable, Sequence
from pathlib import Path

ZERO_SHA = "0" * 40
_SHA_PATTERN = re.compile(r"^[0-9a-fA-F]{40}$")
_WORKER_EXACT_PATHS = frozenset(
    {
        ".github/workflows/windows-worker-package.yml",
        ".github/workflows/pr-acceptance.yml",
        ".github/workflows/main-verification.yml",
        "scripts/ci_changed_paths.py",
        "tests/unit/test_ci_changed_paths.py",
        "pyproject.toml",
        "uv.lock",
        "tests/unit/test_worker_package.py",
        "tests/unit/test_worker_host_config.py",
        "tests/unit/test_worker_control_client_httpx2.py",
        "tests/unit/test_worker_control_client_cancellation.py",
        "tests/unit/test_threads_httpx2_transport.py",
        "tests/unit/test_browser_adapter.py",
    }
)
_WORKER_PATH_PREFIXES = (
    "packaging/windows_worker/",
    "src/threads_platform/workers/",
    "src/threads_platform/infrastructure/worker_agent/",
    "src/threads_platform/infrastructure/browser/",
)


def worker_package_relevant(paths: Iterable[str]) -> bool:
    """Return whether any changed repository path affects Worker packaging/tests."""
    for raw_path in paths:
        path = raw_path.replace("\\", "/").removeprefix("./")
        if path in _WORKER_EXACT_PATHS or path.startswith(_WORKER_PATH_PREFIXES):
            return True
    return False


def _git_output(*arguments: str) -> str:
    result = subprocess.run(
        ("git", *arguments),
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.rstrip("\r\n")


def _changed_paths(mode: str, base_sha: str, head_sha: str) -> list[str]:
    if not _SHA_PATTERN.fullmatch(head_sha):
        raise ValueError("head SHA is not a full commit hash")
    if _git_output("rev-parse", "HEAD") != head_sha:
        raise ValueError("checked-out HEAD does not match requested source SHA")

    if mode == "pull_request":
        if not _SHA_PATTERN.fullmatch(base_sha):
            raise ValueError("PR base SHA is not a full commit hash")
        merge_base = _git_output("merge-base", base_sha, head_sha)
        comparison = f"{merge_base}..{head_sha}"
    elif mode == "main":
        if not _SHA_PATTERN.fullmatch(base_sha):
            raise ValueError("main before SHA is not a full commit hash")
        comparison = f"{base_sha}..{head_sha}"
    else:
        raise ValueError(f"unsupported changed-path mode: {mode}")

    output = _git_output("diff", "--name-only", "--no-renames", "-z", comparison)
    return [path for path in output.split("\x00") if path]


def classify_worker_change(mode: str, base_sha: str, head_sha: str) -> bool:
    """Fail open to Worker execution if an exact comparison cannot be proven."""
    if mode == "main" and base_sha == ZERO_SHA:
        print(
            "changed-path range starts at the all-zero SHA; enabling Worker gates", file=sys.stderr
        )
        return True

    try:
        return worker_package_relevant(_changed_paths(mode, base_sha, head_sha))
    except (OSError, subprocess.CalledProcessError, ValueError) as exc:
        print(
            f"changed-path range unavailable ({type(exc).__name__}); enabling Worker gates",
            file=sys.stderr,
        )
        return True


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", required=True, choices=("pull_request", "main"))
    parser.add_argument("--base-sha", required=True)
    parser.add_argument("--head-sha", required=True)
    args = parser.parse_args(argv)

    worker = classify_worker_change(args.mode, args.base_sha, args.head_sha)
    output = f"worker={str(worker).lower()}\n"
    output_path = os.environ.get("GITHUB_OUTPUT")
    if output_path:
        with Path(output_path).open("a", encoding="utf-8", newline="\n") as output_file:
            output_file.write(output)
    else:
        sys.stdout.write(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
