# Coding-agent CI and acceptance workflow

This document is mandatory process for coding agents working in `pumni/thread`. It exists because a green local run and a hosted Windows acceptance run exercise different environments, and because repeated hosted pushes are an expensive and unreliable debugging strategy.

## 1. State model

Implementation work starts from the coordinator-authorized base and remains on a **Draft PR** while the agent iterates.

Normal flow:

```text
authorized issue
  -> local implementation
  -> deterministic local preflight
  -> Draft PR
  -> optional coordinator-authorized targeted hosted diagnostic
  -> coordinator marks Ready
  -> one exact-head full PR acceptance wave
  -> coordinator acceptance / merge
  -> one main-push verification wave
```

Heavy PR workflows intentionally do not run on every Draft synchronization. Secret scan may still run because it is cheap and prevents credential mistakes from accumulating.

A hosted failure is a hard stop for the coding agent and invalidates the acceptance checkpoint. The coordinator may return the PR to Draft, classify the evidence and authorize one bounded correction.

## 2. Local PASS means preflight only

Do not say "local gates pass" if a required gate silently skipped because the environment was absent.

On Windows, use:

```powershell
powershell -NoProfile -File scripts/windows_local_preflight.ps1
```

The script:
- creates a fresh short CurrentUser-owned TEMP/TMP and pytest basetemp under `LocalApplicationData/TOCI/<run-id>`, outside the Git worktree, so Chromium persistent-profile paths remain bounded on Windows;
- starts a disposable PostgreSQL 17 cluster from the accepted bundled `shared/postgresql` tree;
- creates a test database whose name ends in `_test`;
- sets both Control Plane database URLs;
- runs locked dependency sync, Ruff, formatting, Pyright, Alembic upgrade/check and pytest;
- stops/removes only its own disposable PostgreSQL state; cleanup tries fast stop first and may use immediate stop only for that disposable local cluster.

If the accepted bundled PostgreSQL tree is missing, the script fails explicitly. Build the existing `shared` candidate first or provide `-BundleRoot`; do not let PostgreSQL integration tests skip and then report a full local PASS.

The Windows CurrentUser Root certificate mutation remains the separate manual interactive release contract. Do not weaken, skip or xfail the underlying test in repository code to make unattended local runs green.

## 3. Hosted failure protocol

For every failed hosted run, record before changing code:

1. exact source SHA and workflow/run/job;
2. primary failure code/signature from artifact, not only the final shell exception;
3. all checks that already passed before the failure;
4. authoritative state relevant to the failure: PostgreSQL/session rows, native process state, exact profile/principal, filesystem/registry state, etc.;
5. classification: **product**, **harness**, or **environment**;
6. one root-cause hypothesis that explains all evidence;
7. what deterministic local/unit/integration test or smaller diagnostic will prove/disprove that hypothesis.

Then stop and report to the coordinator.

Do **not**:
- rerun the same SHA hoping timing changes;
- push focus/sleep/listener tweaks one at a time;
- modify product code merely because a UI smoke failed;
- infer authenticated state from a visible or hidden React/UI Automation control;
- call a timeout proof of product failure when authoritative state contradicts it.

### Repeated-signature rule

If two consecutive hosted attempts have the same primary failure code/signature, there is no third attempt by the coding agent.

The agent must provide both artifacts and explain why the previous correction did not change the authoritative state. The coordinator decides whether to change the harness, add a smaller acceptance seam, or reopen product scope.

## 4. UI and session truth

For authentication/RBAC work, precedence is:

1. server/session/database authorization state;
2. Rust in-memory bearer/session state through deterministic tests;
3. semantic UI state;
4. raw UI Automation tree presence last.

A hidden/offscreen webview element can remain discoverable by Windows UI Automation. Therefore text such as "Signed in as" or a hidden "Sign out" button is **not** evidence that an Operator session is currently valid.

When a smoke requires reauthentication, explicitly perform the sign-in flow and verify authoritative session state before the privileged action.

## 5. Ready-for-review gate

The coordinator, not the coding agent, decides when the PR becomes Ready for Review.

Before requesting Ready:
- worktree/branch head is stable;
- local preflight is complete, with any unavailable manual-only gate explicitly named;
- no unresolved hosted failure signature remains;
- task-specific deterministic tests cover the new state transitions;
- no known product/harness ambiguity is being deferred to the full smoke.

Moving the PR to Ready triggers the expensive exact-head acceptance wave. A new code push after an accepted Ready checkpoint invalidates that evidence and requires a new coordinator checkpoint.

## 6. Targeted hosted diagnostics

Use `workflow_dispatch` only when the failure materially depends on hosted Windows/runtime behavior that cannot be reproduced locally.

A targeted run must have:
- a specific hypothesis;
- a specific expected changed signal;
- an exact SHA;
- one bounded attempt.

It is not a substitute for local tests.

## 7. DX-05 lesson

DX-05 PR #135 demonstrated the failure mode this policy prevents: many Draft-PR commits repeatedly launched the full workflow set, while the same Controller failure signature survived several heads. The final artifacts showed the UI smoke was treating UI tree presence as authentication evidence while PostgreSQL session evidence showed no active Operator session.

Future auth/session acceptance must verify authoritative session state before Controller/Worker lifecycle authorization and must not use the full Controller smoke as the first debugger for React/Rust session transitions.
