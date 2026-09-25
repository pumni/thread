# C1 Codex Handoff Brief

This document is a handoff template, not automatic authorization.

A coordinator should verify current main/issue state, then explicitly tell Codex to execute C1.

## Authorized C1 issues when approved

- #21 — Worker registry, account affinity and persistence
- #22 — Worker enrollment/auth/presence/protocol
- #23 — durable WorkerJob lease/checkpoint/intervention/reconnect

Recommended branch: batch/c1-distributed-worker-foundation

## Mandatory reading

- README.md
- docs/PROJECT_STATE_HANDOFF.md
- docs/MASTER_PLAN.md
- docs/ARCHITECTURE.md
- docs/FEATURE_PARITY_MATRIX.md
- docs/WORK_BREAKDOWN.md
- docs/CODEX_EXECUTION_GUIDE.md
- docs/ACCEPTANCE_AND_REVIEW.md
- docs/protocols/WORKER_PROTOCOL_V1.md
- ADR-0003
- ADR-0004
- ADR-0005
- issues #21/#22/#23 including coordinator comments

## Execution order

#21 -> #22 -> #23

Continue through the three issues without intermediate reviewer stop only after explicit C1 authorization and while all gates remain green.

## Hard boundaries

C1 does NOT include Playwright/Selenium, Threads DOM/UI automation, feed/like/follow, browser publishing, discovery, scheduler/ActivityPlan, automatic profile migration, or Redis/message broker.

## Required final evidence

- PR number/branch/HEAD;
- issue/commit map;
- migrations;
- Worker/account affinity constraints;
- enrollment/auth replay tests;
- protocol/version tests;
- WorkerJob concurrency/lease/fencing tests;
- reconnect/restart tests;
- intervention tests;
- exact quality-gate output;
- CI/Secret scan status;
- known limitations.

Do not merge. Stop at C1 checkpoint for coordinator acceptance.