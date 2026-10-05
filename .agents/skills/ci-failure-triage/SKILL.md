---
name: ci-failure-triage
description: Use this skill when investigating, classifying, or resolving hosted GitHub Actions CI workflow failures (PR Acceptance, Main Verification, Desktop Diagnostic), analyzing runner failure artifacts, or following the mandatory hosted failure and repeated-signature triage protocol. Do NOT use for routine local test failures before a PR is opened.
---

# CI Failure Triage Skill

## Purpose
Enforce the repository's disciplined triage protocol for hosted CI failures, ensuring that failures are properly classified and investigated from authoritative state rather than speculative trial-and-error.

## Trigger Conditions
Use this skill when:
- A hosted GitHub Actions run fails (`PR Acceptance`, `Main Verification`, `Secret scan`, `PR Head Guard`, or `Desktop Diagnostic`).
- Downloading and inspecting hosted run logs, artifacts, or Windows diagnostic dumps.
- Classifying hosted failures into product, harness, or environment causes.
- Applying the mandatory two-consecutive failure stop rule.

## Non-Trigger Conditions
Do NOT use this skill for:
- Routine local unit or integration test failures prior to pushing a PR (debug locally with pytest).
- Pre-commit formatting or linting failures.
- Regular feature development without hosted CI issues.

## Canonical References
Inspect these sources when triaging CI failures:
- `docs/CI_AGENT_WORKFLOW.md` (authoritative process and hard-stop rules).
- `.github/workflows/pr-acceptance.yml` and related reusable workflow definitions.
- Workflow run logs, artifacts, and execution diagnostics.
- `scripts/windows_local_preflight.ps1`.

## Invariants & Design Rules
1. **Hosted Failure is a Hard Stop:** Never push speculative timing or sleep tweaks to see if hosted CI passes.
2. **Authoritative Evidence First:** Server/database authorization state and native process state supersede UI Automation tree presence. Discoverable or hidden UI elements do not prove valid sessions.
3. **Exact SHA Traceability:** Always identify the exact `source_sha`, workflow run ID, and job name.
4. **Primary Failure Signature:** Extract the primary failure code or exception from artifacts, not just the outer shell exit code.
5. **Repeated-Signature Rule:** If two consecutive hosted attempts produce the same primary failure signature, do not make a third attempt. Stop and escalate to the coordinator with both artifacts and root-cause analysis.

## Step-by-Step Procedure
1. **Download artifacts:** Retrieve the workflow run artifacts and raw logs for the failing job.
2. **Record evidence:**
   - Exact source commit SHA and workflow run ID.
   - Primary failure code/signature from artifact.
   - All checks that succeeded prior to the failure point.
   - Authoritative state at failure time (PostgreSQL records, process tree, TLS certificates).
3. **Classify root cause:**
   - **Product:** Implementation logic bug or unhandled edge case.
   - **Harness:** Flaky test harness, timing window in test runner, or incorrect test assertion.
   - **Environment:** Runner resource exhaustion, network partition, or OS configuration discrepancy.
4. **Formulate single hypothesis:** Define one falsifiable hypothesis explaining all evidence.
5. **Reproduce locally:** Author or run a deterministic local test or targeted diagnostic to prove/disprove the hypothesis before changing code.
6. **Report to coordinator:** Present the classification, evidence, and proposed fix before pushing changes.
