# Work Breakdown v2 — Distributed Hybrid Roadmap

## 1. Current project position

Completed:
- Security implementation PR #13 (owner confirmation remains in issue #1).
- Batch A PR #15.
- TP-004A / partial TP-002 PR #18.
- Batch B PR #20.
- C1 PR #32 — issues #21, #22, #23 accepted and merged.
- C2 PR #33 — issue #24 accepted and merged.
- C3-01 PR #34 — issue #25 accepted and merged.
- C3-02 PR #35 — issue #26 accepted and merged.

Current authorized implementation checkpoint:
- **C4 — Discovery, public-profile enrichment and Leads pipeline**
  - #8 only

Issue #3 remains a production/release gate.

## 2. Delivery rule

Only the batch explicitly authorized by the coordinator may be implemented.

A future roadmap entry is not automatic permission for Codex to start it.

Each batch:
1. starts from current main;
2. uses one batch branch unless coordinator says otherwise;
3. keeps issue-level commits or clearly traceable commit groups;
4. runs quality gates at issue boundaries and final checkpoint;
5. stops on architecture/security/reliability stop conditions;
6. opens one checkpoint PR;
7. does not self-merge.

## 3. Roadmap

| Checkpoint | Issues | Purpose | Gate |
|---|---|---|---|
| C1 | #21, #22, #23 | Worker registry/auth/protocol/WorkerJob reliability | Review before any browser runtime |
| C2 | #24 | Capability Router + per-account execution policy | Review before executor expansion |
| C3 | #25, #26 | Windows Worker Agent + browser adapter foundation | Review before Threads UI capability pack |
| C4 | #8 | API-first Discovery + public-profile enrichment + Leads | Browser enrichment only after C3 |
| C5 | #27, #28 | Approved browser capabilities + AccountActivityPlan | Review before broad activity scheduling |
| C6 | #9, #10 | Durable scheduler + fleet operations/deployment | Operational readiness review |
| D | #11 | E2E production certification | Requires issue #3 complete |

## 4. C1 — Distributed Worker Foundation

Status: **ACCEPTED / MERGED** in PR #32, merge commit `8cbcd9e6c4fad73579148a826f3754e92e41e4f3`.

### Order

1. #21 — Worker registry, account affinity and persistence.
2. #22 — enrollment/device auth/presence/protocol.
3. #23 — WorkerJob leases/checkpoints/intervention/reconnect.

Recommended branch: batch/c1-distributed-worker-foundation

### C1 must prove

- stable worker identity independent of hostname;
- persistent account -> worker/profile affinity;
- worker protocol compatibility;
- secure enrollment/authentication;
- WSS presence/notification does not own business truth;
- durable HTTPS job mutations;
- wrong worker cannot claim;
- concurrent claim has one winner;
- stale lease cannot checkpoint/finalize;
- disconnect/reconnect and Control Plane restart preserve recoverable state;
- intervention is durable;
- DRAINING/OFFLINE/UPGRADE_REQUIRED workers get no new work.

### Explicit C1 non-goals

- Playwright/Selenium;
- Threads DOM selectors;
- feed browsing;
- local-media UI publish;
- like/follow;
- discovery;
- ActivityPlan;
- scheduler;
- worker auto-update implementation.

## 5. C2 — Capability Router

Issue #24.

Status: **ACCEPTED / MERGED** in PR #33, merge commit `29895a987d12b7325671fb8ca7c30272f865cd73`.

Must separate business capability from executor.

Required concepts:
- capability_name/version;
- API/BROWSER/HUMAN support;
- account mode;
- preferred/fallback executor;
- READ/MUTATION/SESSION/BACKGROUND coordination;
- WAITING_EXECUTION / WAITING_INTERVENTION;
- deterministic/auditable route decision.

No browser business action is implemented here.

## 6. C3 — Windows Browser Worker Foundation

Order:
1. #25 Worker Agent/profile/session/network foundation — **ACCEPTED / MERGED** in PR #34, merge commit `6b9984fecf7167ce02389be9bd935983f8ab7f1d`.
2. #26 browser engine ADR + fail-closed adapter — **ACCEPTED / MERGED** in PR #35, merge commit `0021517b55b1e7bcddc9f3fd9d2099feb3e0b6ab`.

C3 establishes infrastructure only and is complete.

Do not expand C3 into the full Threads UI feature set.

## 7. C4 — Discovery and Leads

Issue #8.

Status: **CURRENT AUTHORIZED CHECKPOINT** after C3 acceptance.

Scope:
- keyword/topic;
- public profiles/profile posts;
- mentions;
- conversation enrichment;
- normalized discovered Thread/author entities;
- LeadCandidate;
- dedupe/resume;
- API first.

Browser enrichment waits for C3 and must route through C2.

## 8. C5 — Browser Capabilities and Account Activity

Issues:
- #27 capability pack v1;
- #28 AccountActivityPlan/priority/preemption.

Browser capabilities must be explicit and independently reviewable.

No random warm-account loop.

LIKE/FOLLOW remain VERIFY unless explicitly retained at implementation review.

## 9. C6 — Scheduler and Operations

Issues:
- #9 durable scheduler/fleet orchestration;
- #10 observability/security/Windows packaging/deployment.

C6 turns explicit jobs/plans into durable operations and establishes deploy/update/recovery procedures.

## 10. D — Release

Issue #11.

Issue #3 must be complete before production/release certification.

## 11. Open gates outside normal engineering sequence

### #1 security owner follow-up

Does not block engineering.
Must be resolved/accepted before final release security sign-off.

### #3 live Meta validation

Does not block safe documentation-contract implementation.
Does block production activation.

## 12. Stop conditions

Codex stops and reports instead of improvising when:
- an ADR must be contradicted;
- Command and WorkerJob would need to be collapsed;
- Redis/broker/new distributed system is required;
- plaintext account/worker secrets appear necessary;
- automatic profile migration appears necessary;
- browser automation is needed before C3;
- anti-detect/fingerprint-evasion behavior appears in scope;
- a side effect cannot be reconciled safely;
- quality gates would need weakening;
- destructive migration assumptions are required;
- current official API behavior materially contradicts the documented capability contract.

## 13. Parallelism

C1 is intentionally sequential because each issue establishes a dependency for the next.

After C2:
- API-only portions of C4 may proceed independently from browser infrastructure;
- browser enrichment waits for C3;
- C5 waits for C3;
- C6 scheduling may be designed alongside late C5 only when shared contracts are stable.

Coordinator controls parallel authorization.

## 14. Checkpoint evidence

Every checkpoint PR must report:
- branch/HEAD;
- issue -> commit map;
- migrations;
- data/state-machine changes;
- protocol changes;
- exact quality-gate results;
- CI/secret-scan status;
- concurrency/restart/recovery tests;
- security-sensitive design;
- known limitations;
- deferred gates;
- ADR/docs changed.