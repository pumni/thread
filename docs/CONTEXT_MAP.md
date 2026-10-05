# Context Map for Coding Agents

Purpose: load the **smallest sufficient context** for the current task.

Do not treat this as a mandatory reading list. Start with the authorized GitHub issue / coordinator instruction, root `AGENTS.md` (and nearest nested `AGENTS.md` if working within a subtree), and current code/tests; then follow only the route that matches your task.

## Task & Capability Routes

### 1. Domain & Application Behavior
- **Trigger:** Core domain models, command processing, account activities, business validation policies, pure logic.
- **Skill:** None (pure domain logic; standard linters and tests apply).
- **Canonical Docs:** `docs/ARCHITECTURE.md` (Domain/Application sections), `docs/FEATURE_PARITY_MATRIX.md`.
- **Implementation Truth:** `src/threads_platform/domain/`, `src/threads_platform/application/`, `tests/unit/`.
- **Key Invariants:** Pure domain boundary: never import FastAPI, httpx, SQLAlchemy, WebSockets, browser libraries, Windows APIs, or Meta DTOs into domain.

### 2. PostgreSQL, Schemas & Migrations
- **Trigger:** Adding/modifying tables, columns, indexes, Alembic migration revisions, SQLAlchemy models.
- **Skill:** [`.agents/skills/database-migration/SKILL.md`](../.agents/skills/database-migration/SKILL.md).
- **Canonical Docs:** `docs/ACCEPTANCE_AND_REVIEW.md` (Section 3 Data Integrity), `scripts/windows_local_preflight.ps1`.
- **Implementation Truth:** `migrations/versions/`, `src/threads_platform/infrastructure/persistence/models.py`, `tests/integration/`.
- **Key Invariants:** PostgreSQL is authoritative business state; all migrations must be reversible; UTC timestamps; verify with `uv run alembic check`.

### 3. Worker, WorkerJob & Distributed Ownership
- **Trigger:** WorkerJob lifecycle, claim transactions, lease renewals, fencing tokens, stale recovery, execution modes.
- **Skill:** [`.agents/skills/worker-protocol/SKILL.md`](../.agents/skills/worker-protocol/SKILL.md).
- **Canonical Docs:** [`docs/adr/0003-distributed-hybrid-execution.md`](adr/0003-distributed-hybrid-execution.md), [`docs/adr/0004-persistent-worker-affinity-and-worker-jobs.md`](adr/0004-persistent-worker-affinity-and-worker-jobs.md), `docs/ARCHITECTURE.md` (WorkerJob section).
- **Implementation Truth:** `src/threads_platform/application/worker_jobs.py`, `src/threads_platform/domain/worker_jobs.py`, `tests/unit/test_worker_jobs.py`.
- **Key Invariants:** `Command` is business intent; `WorkerJob` is remote execution; independent lease/fencing/checkpoint semantics; stale lease claim fails closed; persistent account -> worker/profile affinity.

### 4. Worker Protocol, Auth, Enrollment, Drain & Pairing
- **Trigger:** Worker WebSocket framing, protocol v1/v2 messages, 256-bit enrollment tokens, device authentication, worker draining.
- **Skill:** [`.agents/skills/worker-protocol/SKILL.md`](../.agents/skills/worker-protocol/SKILL.md).
- **Canonical Docs:** [`docs/protocols/WORKER_PROTOCOL_V1.md`](protocols/WORKER_PROTOCOL_V1.md), `docs/WORKER_UPDATE_RUNBOOK.md`.
- **Implementation Truth:** `src/threads_platform/application/worker_protocol.py`, `src/threads_platform/transport/http/workers.py`, `src/threads_platform/infrastructure/security/worker_auth.py`.
- **Key Invariants:** WebSocket is notification/presence only (never queue or state authority); 256-bit entropy for enrollment tokens; additive version negotiation; row-locked DRAINING quiescence handshake.

### 5. Distributed Worker Browser Capabilities & UI Contracts
- **Trigger:** Distributed Worker browser automation capabilities (`threads.browser.*`), Playwright DOM interactions, synthetic UI contracts, staged mutations.
- **Skill:** [`.agents/skills/browser-capability/SKILL.md`](../.agents/skills/browser-capability/SKILL.md).
- **Canonical Docs:** [`docs/adr/0005-browser-capability-boundary.md`](adr/0005-browser-capability-boundary.md), [`docs/adr/0006-playwright-browser-adapter.md`](adr/0006-playwright-browser-adapter.md), `docs/WORKER_BROWSER_CAPABILITY_PACK_V1.md`.
- **Implementation Truth:** `src/threads_platform/workers/browser.py`, `tests/unit/`.
- **Key Invariants:** Distributed browser/UI recognition mismatch or ambiguity must fail closed; login and session challenges route to human intervention, never bypass; bound ancestor depth traversal; no generated CSS classes; no anti-detect/evasion.

### 5A. Standalone Local Execution
- **Trigger:** `src/threads_platform/standalone/`, `threads-local`, standalone local account/profile execution.
- **Skill:** `browser-capability` only when the task touches browser engine/UI contracts; `threads-api-contract` only when the task touches official API behavior.
- **Canonical Docs:** [ADR-0008](adr/0008-standalone-local-execution.md); browser [ADR-0005](adr/0005-browser-capability-boundary.md) / [ADR-0006](adr/0006-playwright-browser-adapter.md) only for browser work.
- **Implementation Truth:** `src/threads_platform/standalone/` plus reused low-level adapters named by the authorized LOCAL issue.
- **Key Invariants:** No Control Plane/Command/WorkerJob dependency; local state is not PostgreSQL authority; profiles are not shared with Worker mode; human login; fail-closed browser behavior; no evasion.

### 6. Scheduler & Background Work
- **Trigger:** Periodic work generation, AccountActivityPlan recurrence, due occurrence materialization, worker presence expiry, outbox delivery.
- **Skill:** None (or `database-migration` if altering scheduler tables).
- **Canonical Docs:** `docs/ARCHITECTURE.md` (Scheduler section), `docs/MASTER_PLAN.md` (Section 2, 7).
- **Implementation Truth:** `src/threads_platform/application/scheduler.py`, `tests/unit/`.
- **Key Invariants:** Control Plane/Scheduler creates business actions (workers do not); deterministic fixed intervals; presence expiry is separate from WorkerJob lease; bounded sequential outbox delivery.

### 7. External Threads API Behavior
- **Trigger:** Official Meta Threads Graph API client, token exchange/refresh, rate limiting, Graph API webhook handling.
- **Skill:** [`.agents/skills/threads-api-contract/SKILL.md`](../.agents/skills/threads-api-contract/SKILL.md).
- **Canonical Docs:** `docs/THREADS_API_CAPABILITY_SPIKE.md`, `docs/THREADS_CREDENTIAL_OPERATIONS.md`, `docs/THREADS_LIVE_VALIDATION_RUNBOOK.md`.
- **Implementation Truth:** `src/threads_platform/infrastructure/threads_api/client.py`, `src/threads_platform/infrastructure/threads_api/credentials.py`.
- **Key Invariants:** Official Threads API preferred where suitable; test fixtures labeled `documentation-contract` vs scrubbed live evidence; token values never stored plaintext in PostgreSQL.

### 8. Observability, Health & Deployment
- **Trigger:** Health/readiness checks (`/health`, `/ready`), Prometheus metrics (`/metrics`), OpenTelemetry tracing, Docker Compose deployment.
- **Skill:** None.
- **Canonical Docs:** `docs/OBSERVABILITY_RUNBOOK.md`, `docs/CONTROL_PLANE_DEPLOYMENT_RUNBOOK.md`.
- **Implementation Truth:** `src/threads_platform/observability/`, `Dockerfile`, `compose.yaml`, `scripts/control_plane_compose_smoke.py`.
- **Key Invariants:** `/health` is process liveness; `/ready` reports persisted state fail-closed; structured log redaction; bounded metrics/tracing without leaking payloads or credentials.

### 9. Desktop React / UI
- **Trigger:** Operator console React components, session lock, operator authentication UI, status badges.
- **Subtree Instruction:** [`apps/desktop/AGENTS.md`](../apps/desktop/AGENTS.md).
- **Skill:** None.
- **Canonical Docs:** `apps/desktop/README.md`, `docs/desktop/ACCEPTANCE_MATRIX.md`.
- **Implementation Truth:** `apps/desktop/src/`, `apps/desktop/tests/`.
- **Key Invariants:** Server authorization state over UI Automation tree presence; operator session lock semantics.

### 10. Desktop Rust, Native Supervisor, Process & Security
- **Trigger:** Tauri supervisor process tree, Windows DPAPI credentials, local TLS private CA, packaging scripts, Windows rehearsal.
- **Subtree Instruction:** [`apps/desktop/AGENTS.md`](../apps/desktop/AGENTS.md).
- **Skill:** [`.agents/skills/desktop-acceptance/SKILL.md`](../.agents/skills/desktop-acceptance/SKILL.md).
- **Canonical Docs:** [`docs/adr/0007-windows-first-single-app-desktop.md`](adr/0007-windows-first-single-app-desktop.md), `docs/desktop/SECURITY_AND_PROTOCOLS.md`, `docs/desktop/WINDOWS_REHEARSAL.md`.
- **Implementation Truth:** `apps/desktop/src-tauri/`, `scripts/windows_local_preflight.ps1`.
- **Key Invariants:** Windows-first desktop single app; current-user DPAPI isolation; dedicated Windows runtime user; graceful Quit vs crash PostgreSQL WAL recovery.

### 11. Hosted CI & Acceptance Failure
- **Trigger:** GitHub Actions run failure, PR Acceptance triage, interpreting Windows Desktop diagnostic runner artifacts.
- **Skill:** [`.agents/skills/ci-failure-triage/SKILL.md`](../.agents/skills/ci-failure-triage/SKILL.md).
- **Canonical Docs:** [`docs/CI_AGENT_WORKFLOW.md`](CI_AGENT_WORKFLOW.md), `.github/workflows/pr-acceptance.yml`.
- **Key Invariants:** Hosted CI failure is a hard stop; record primary failure signature; classify as product/harness/environment; two consecutive identical signatures require coordinator escalation.

### 12. Architecture & Security Review
- **Trigger:** Proposed architectural changes, trust boundary reviews, new ADR drafting, security model changes.
- **Canonical Docs:** `docs/adr/`, `docs/ACCEPTANCE_AND_REVIEW.md`, `docs/MASTER_PLAN.md`.
- **Key Invariants:** All 14 non-negotiable invariants in root [`AGENTS.md`](../AGENTS.md). Structural changes require an approved ADR before implementation.

---

## Historical & Archival References

For historical context on earlier completed milestones or past planning baselines:
- **Milestone History Archive (Batches A/B, TP-004A, C1–C6):** [`docs/handoffs/PROJECT_STATE_HISTORY_2026-10.md`](handoffs/PROJECT_STATE_HISTORY_2026-10.md).
- **Desktop Planning Baseline:** [`docs/desktop/SESSION_HANDOFF.md`](desktop/SESSION_HANDOFF.md) and [`docs/desktop/PREIMPLEMENTATION_AUDIT.md`](desktop/PREIMPLEMENTATION_AUDIT.md).
- **Compatibility Pointer:** [`docs/PROJECT_STATE_HANDOFF.md`](PROJECT_STATE_HANDOFF.md).