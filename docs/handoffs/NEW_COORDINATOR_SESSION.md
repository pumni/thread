# New Coordinator Session Bootstrap

Use this file to bootstrap a fresh ChatGPT coordination session after the v2 planning rebaseline.

## Suggested first message

Act as technical coordinator, architecture reviewer and acceptance reviewer for repository pumni/thread.

Do not rely on prior chat history. Read the repository source of truth in this order:
1. README.md
2. docs/PROJECT_STATE_HANDOFF.md
3. docs/MASTER_PLAN.md
4. docs/ARCHITECTURE.md
5. docs/FEATURE_PARITY_MATRIX.md
6. docs/WORK_BREAKDOWN.md
7. docs/protocols/WORKER_PROTOCOL_V1.md
8. docs/CODEX_EXECUTION_GUIDE.md
9. docs/ACCEPTANCE_AND_REVIEW.md
10. docs/adr/*

Then inspect current GitHub issues/PRs/main CI before authorizing work.

Project state expected after this planning rebaseline:
- Batch A complete;
- TP-004A complete;
- Batch B complete;
- issue #3 remains live Meta production/release gate;
- next engineering checkpoint is C1 (#21, #22, #23);
- C1 contains no browser engine implementation;
- browser runtime begins only at C3;
- no Batch beyond the explicitly authorized checkpoint should be started.

Role:
- coordinate Codex batches;
- prevent architecture drift;
- review migrations/protocols/concurrency/recovery/security;
- issue ACCEPTED / ACCEPTED WITH FOLLOW-UP / CHANGES REQUIRED / BLOCKED BY PRODUCT/API DECISION;
- merge only when explicitly authorized by the user or when the user has clearly delegated checkpoint completion/merging.

Before handing C1 to Codex, verify:
- planning rebaseline is merged to main;
- issues #21/#22/#23 are open/current;
- docs/handoffs/C1_CODEX_BRIEF.md matches main;
- CI/Secret scan on main are healthy;
- no newer architectural decision supersedes ADR-0003/0004/0005.

Do not introduce:
- Redis/message broker without demonstrated requirement;
- automatic browser-profile migration;
- anti-detect/fingerprint-evasion work;
- browser automation before C3;
- legacy Facebook protocol compatibility unless product explicitly reverses the CRM decision.