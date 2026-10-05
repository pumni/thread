# Agent Context Baseline and Evaluation Specification (CTX-01)

**Issue reference:** Refs #147 (Parent: #146)
**Baseline commit:** `96cb2e28a00b17c54fa9a33fde54b4826adda529` (`origin/main`)
**Scope:** Documentation and analysis only. Establishes the authoritative before-state for the context layer refactor. No existing context behavior, product runtime code, or workflow definitions are modified.

---

## 1. Current Boot / Read Graph

### 1.1 Root Instruction Boot Path
Every coding agent operating in the repository enters through root [`AGENTS.md`](../../AGENTS.md). The instruction flow defined in `AGENTS.md` mandates the following sequential boot steps:

```
[Agent Enters Task]
       │
       ▼
1. Read docs/PROJECT_STATE_HANDOFF.md
       │
       ▼
2. Read authorized GitHub issue/batch + coordinator comments
       │
       ▼
3. Use docs/CONTEXT_MAP.md to select task-specific documentation
       │
       ▼
4. Inspect current code and tests in target area
       │
       ▼
5. If external Threads behavior matters, verify Meta docs/changelog
       │
       ▼
(Before opening/advancing PR or reacting to CI)
Read docs/CI_AGENT_WORKFLOW.md
```

### 1.2 Mandatory vs Conditional Traversal

| Document | Trigger / Rule in `AGENTS.md` | Effective Status | Rationale / Failure Mode |
|---|---|---|---|
| [`AGENTS.md`](../../AGENTS.md) | Universal root prompt | **Always loaded** | System prompt / repo-level agent contract. |
| [`docs/PROJECT_STATE_HANDOFF.md`](../PROJECT_STATE_HANDOFF.md) | "1. Read `docs/PROJECT_STATE_HANDOFF.md`" | **Always loaded** | Mandated unconditional step 1. Loads 31 KB of historical milestone narrative regardless of task scope. |
| [`docs/CONTEXT_MAP.md`](../CONTEXT_MAP.md) | "3. Use `docs/CONTEXT_MAP.md`..." | **Always loaded** | Mandated unconditional step 3. Used as routing index, but agents read entire file to locate relevant row. |
| [`docs/CI_AGENT_WORKFLOW.md`](../CI_AGENT_WORKFLOW.md) | "Before opening/advancing a PR or reacting to hosted CI..." | **De facto always loaded** | Any task producing a PR or touching CI requires this document. |
| [`docs/desktop/SESSION_HANDOFF.md`](../desktop/SESSION_HANDOFF.md) | Header of `PROJECT_STATE_HANDOFF.md` + DX row in `CONTEXT_MAP.md` | **Always loaded for Desktop** | Instructs fresh Desktop sessions to read this plus `PREIMPLEMENTATION_AUDIT.md`. |
| [`docs/desktop/PREIMPLEMENTATION_AUDIT.md`](../desktop/PREIMPLEMENTATION_AUDIT.md) | Referenced by `SESSION_HANDOFF.md` | **Always loaded for Desktop** | Historical planning audit with 18 findings (17.7 KB). |
| [`docs/CODEX_EXECUTION_GUIDE.md`](../CODEX_EXECUTION_GUIDE.md) | Historical v2 guide | **Orphaned / Ambient** | Not directly referenced in boot steps, but exists as duplicate policy that agents may discover or preload. |
| [`docs/ACCEPTANCE_AND_REVIEW.md`](../ACCEPTANCE_AND_REVIEW.md) | Referenced by `CONTEXT_MAP.md` rows | **Conditional** | Reviewer checklist loaded for WorkerJob/auth/scheduler changes. |
| Domain ADRs / Protocols | Selected by `CONTEXT_MAP.md` rows | **Conditional** | Genuinely selective JIT documents (e.g., ADR-0003, ADR-0006, Worker Protocol v1/v2). |

### 1.3 Desktop Session Extension
When a task touches the Windows Desktop application (`apps/desktop/` or `packaging/windows_desktop/`), the boot payload expands significantly:
1. `PROJECT_STATE_HANDOFF.md` line 3 instructs the agent to read `docs/desktop/SESSION_HANDOFF.md` and `docs/desktop/PREIMPLEMENTATION_AUDIT.md`.
2. `SESSION_HANDOFF.md` section 1 further instructs the agent to read:
   - `docs/adr/0007-windows-first-single-app-desktop.md`
   - `docs/desktop/DELIVERY_PLAN.md`
   - `docs/desktop/SECURITY_AND_PROTOCOLS.md`
   - `docs/desktop/ACCEPTANCE_MATRIX.md`
   - `docs/desktop/WINDOWS_REHEARSAL.md`
   - `docs/desktop/ISSUE_MAP.md`
3. If an agent follows this chain literally, it reads over **195 KB and 2,000 lines** of governance and architecture text before reading the authorized issue or inspecting code.

---

## 2. Measured Baseline

Measurements taken from exact repository checkout at commit `96cb2e28a00b17c54fa9a33fde54b4826adda529`.

### 2.1 Core Governance and Context Files

| File | Bytes | Lines | Category / Role |
|---|---|---|---|
| [`AGENTS.md`](../../AGENTS.md) | 5,762 | 102 | Universal repo-wide root prompt |
| [`docs/PROJECT_STATE_HANDOFF.md`](../PROJECT_STATE_HANDOFF.md) | 31,179 | 560 | Monolithic milestone history & handoff |
| [`docs/CONTEXT_MAP.md`](../CONTEXT_MAP.md) | 4,996 | 129 | Milestone-based documentation router |
| [`docs/CODEX_EXECUTION_GUIDE.md`](../CODEX_EXECUTION_GUIDE.md) | 4,319 | 133 | Duplicate execution guide (orphaned) |
| [`docs/CI_AGENT_WORKFLOW.md`](../CI_AGENT_WORKFLOW.md) | 7,734 | 122 | CI lifecycle, preflight & triage rules |
| [`docs/ACCEPTANCE_AND_REVIEW.md`](../ACCEPTANCE_AND_REVIEW.md) | 5,655 | 158 | Review gates & invariant acceptance criteria |
| [`docs/desktop/SESSION_HANDOFF.md`](../desktop/SESSION_HANDOFF.md) | 12,519 | 65 | Desktop v1 fresh-session handoff |
| [`docs/desktop/PREIMPLEMENTATION_AUDIT.md`](../desktop/PREIMPLEMENTATION_AUDIT.md) | 17,739 | 81 | Desktop planning audit findings A-01..A-18 |
| **Total Core Governance Set** | **89,903** | **1,350** | Core context architecture footprint |

*Note: Line counts are exact splitline counts matching repository tools and Python `read_bytes().splitlines()`. Token counts are omitted per issue requirements.*

### 2.2 Default Pre-Task Payload Calculations

1. **Standard Default Pre-Task Payload (General / Backend)**
   - Strict "Start every task" path (`AGENTS.md` + `PROJECT_STATE_HANDOFF.md` + `CONTEXT_MAP.md`):
     - **41,937 bytes / 791 lines**
   - With mandatory CI workflow preflight (`+ docs/CI_AGENT_WORKFLOW.md`):
     - **49,671 bytes / 913 lines**

2. **Desktop Task Default Pre-Task Payload**
   - Direct Desktop fresh-session entry (`Standard Default` + `SESSION_HANDOFF.md` + `PREIMPLEMENTATION_AUDIT.md`):
     - **72,195 bytes / 937 lines** (without CI doc)
     - **79,929 bytes / 1,059 lines** (with CI doc)
   - Expanded Desktop chain (if following `SESSION_HANDOFF.md` section 1 links):
     - Adding ADR-0007 (14,958 B), DELIVERY_PLAN (32,583 B), SECURITY_AND_PROTOCOLS (21,175 B), ACCEPTANCE_MATRIX (15,099 B), WINDOWS_REHEARSAL (15,830 B), ISSUE_MAP (7,193 B):
     - **186,767 bytes / 1,937 lines** (before issue inspection)

---

## 3. Authority Matrix

This matrix resolves ambiguity between competing sources of truth across information types:

| Information Type | Current Competing Sources | Desired Canonical Authority | Resolution Principle |
|---|---|---|---|
| **Current Task / Scope Authorization** | `PROJECT_STATE_HANDOFF.md`, `docs/desktop/SESSION_HANDOFF.md`, `docs/WORK_BREAKDOWN.md`, GitHub issues/comments | **Authorized GitHub issue, coordinator comments, direct user instruction** | Static documents cannot authorize work. Roadmaps and milestone handoffs describe history or planning; only live issue/comment authorizes execution. |
| **Global Architecture Invariants** | `AGENTS.md`, `CODEX_EXECUTION_GUIDE.md`, `ACCEPTANCE_AND_REVIEW.md`, `docs/ARCHITECTURE.md` | **Root `AGENTS.md`** | High-signal, load-bearing invariants reside concisely in root `AGENTS.md`. Detailed rationales live in ADRs. |
| **Subtree-Specific Rules** | `SESSION_HANDOFF.md`, `PREIMPLEMENTATION_AUDIT.md`, `docs/desktop/README.md`, scattered comments | **Nearest nested `AGENTS.md` (e.g. `apps/desktop/AGENTS.md`)** | Desktop/Tauri/Rust rules apply only within that subtree and should not pollute root ambient context. |
| **Architecture Decisions** | Re-summaries in `ARCHITECTURE.md`, `MASTER_PLAN.md`, `PROJECT_STATE_HANDOFF.md`, `docs/adr/*.md` | **Accepted ADRs (`docs/adr/0001` through `0007`)** | ADRs are immutable accepted decisions. Prose summaries in other docs must link to ADRs rather than paraphrasing. |
| **Versioned Protocol Contracts** | `docs/protocols/WORKER_PROTOCOL_V1.md`, `docs/desktop/SECURITY_AND_PROTOCOLS.md`, code | **Versioned protocol docs (`docs/protocols/*`) + verified code/tests** | Protocol documents define the wire specification. Filenames reflect versioning; code implements active protocol. |
| **CI & Acceptance Procedure** | `AGENTS.md` ("Hosted CI discipline"), `docs/CI_AGENT_WORKFLOW.md`, `docs/ACCEPTANCE_AND_REVIEW.md` | **`docs/CI_AGENT_WORKFLOW.md` (process) + `docs/ACCEPTANCE_AND_REVIEW.md` (criteria)** | `AGENTS.md` provides a concise pointer and hard-stop rule; detailed triage and failure protocols belong in specialized docs / skills. |
| **Current Project State** | `PROJECT_STATE_HANDOFF.md`, `docs/desktop/SESSION_HANDOFF.md`, Git log, GitHub issues | **Git tree (`main` commit SHA) + GitHub issues/milestones** | Code and git commit graph are historical ground truth. Documents should not require manual updating of closed PR hashes on every commit. |
| **Session Continuity / History** | `PROJECT_STATE_HANDOFF.md`, `docs/handoffs/*`, PR merge commits | **Git commit history, PR descriptions, focused session summaries** | Historical summaries must be archived or consulted on-demand, never preloaded into routine tasks. |
| **External Threads Behavior** | `docs/THREADS_API_CAPABILITY_SPIKE.md`, `FEATURE_PARITY_MATRIX.md`, fixtures | **Current official Meta documentation + scrubbed live/doc fixtures** | Official Meta API docs and reviewed evidence govern external behavior. Prose conjectures must be tested against live/doc evidence. |
| **Implementation Truth** | Prose documentation, comments, type annotations, unit tests, code | **Current code and passing tests** | If code/tests disagree with prose documentation, investigate code and report discrepancy; never assume unverified prose is true. |

---

## 4. Duplication Inventory

Detailed comparison across core governance files revealing problematic overlap, stale coupling, and divergence:

### 4.1 Root `AGENTS.md` vs `docs/CODEX_EXECUTION_GUIDE.md`
- **Overlap:**
  - `CODEX_EXECUTION_GUIDE.md` Section 1 duplicates `AGENTS.md` "Start every task" (read handoff, read issue, use context map, inspect code).
  - Sections 4, 5, 6 duplicate `AGENTS.md` "Non-negotiable invariants" (Postgres authoritative, Command != WorkerJob, WebSocket notification only, persistent affinity, domain dependency rules, fail-closed browser, no anti-detect).
  - Section 9 duplicates `AGENTS.md` "Validation" (exact identical 6 `uv run` commands).
  - Section 11 duplicates `AGENTS.md` "Stop instead of improvising" (exact bullet points).
- **Classification:** **Problematic duplicated policy.** `CODEX_EXECUTION_GUIDE.md` is an orphaned snapshot that receives no updates when `AGENTS.md` evolves, creating policy divergence.

### 4.2 Root `AGENTS.md` vs `docs/CI_AGENT_WORKFLOW.md`
- **Overlap:**
  - `AGENTS.md` lines 68–93 ("Hosted CI discipline") summarize 25 lines of detailed workflow rules: Draft/Ready gates, Secret scan, exact `source_sha`, desktop manual diagnostic, hard stop on failure, two consecutive failures stop rule.
  - `docs/CI_AGENT_WORKFLOW.md` explains the exact same rules with operational context and script instructions.
  - `tests/unit/test_ci_acceptance_workflow.py` asserts exact string matches in both files!
- **Classification:** **Problematic duplicated policy + rigid test coupling.** The duplication is enforced by a unit test asserting phrases in both `AGENTS.md` and `CI_AGENT_WORKFLOW.md`.

### 4.3 Root `AGENTS.md` vs `docs/CONTEXT_MAP.md`
- **Overlap:**
  - `CONTEXT_MAP.md` lines 7–15 ("Always") repeat the exact 4 reading steps from `AGENTS.md` line 16 ("Start every task").
- **Classification:** **Harmless pointer / repetition.** Mild redundancy, but consumes tokens without adding routing value.

### 4.4 `docs/PROJECT_STATE_HANDOFF.md` vs `docs/MASTER_PLAN.md` / `docs/WORK_BREAKDOWN.md`
- **Overlap:**
  - `PROJECT_STATE_HANDOFF.md` contains 560 lines detailing completed milestones: Batch A, Batch B, C1, C2, C3-01, C3-02, C4, C5-01, C5-02, C6-01, listing specific PR numbers, commit SHAs, and selector contracts.
  - This information is historical; once merged into `main`, the code and git log are authoritative.
- **Classification:** **Stale/live-state coupling.** Preloading 31 KB of historical narrative into every agent session wastes context window and risks agents acting on obsolete intermediate milestone constraints.

### 4.5 `docs/desktop/SESSION_HANDOFF.md` vs `docs/desktop/PREIMPLEMENTATION_AUDIT.md`
- **Overlap:**
  - `SESSION_HANDOFF.md` Section 4 ("Critical implementation discoveries from audit") re-summarizes findings A-01, A-02, A-03, A-04, A-05, A-07, A-09, A-17 from `PREIMPLEMENTATION_AUDIT.md`.
  - Both files are marked mandatory reading for any Desktop session.
- **Classification:** **Problematic internal duplication.** Reading both forces the agent to digest the same 8 architectural findings twice.

### 4.6 `docs/ACCEPTANCE_AND_REVIEW.md` vs `AGENTS.md`
- **Overlap:**
  - `ACCEPTANCE_AND_REVIEW.md` sections 2, 5, 6, 7 repeat the domain dependency rules, WorkerJob invariants, and browser fail-closed requirements.
  - Section 11 repeats the standard 6 validation commands.
- **Classification:** **Intentional canonical detail.** `ACCEPTANCE_AND_REVIEW.md` is structured for reviewers/coordinators, whereas `AGENTS.md` is for coding agents.

---

## 5. Reference Inventory

Comprehensive repository search for references to core governance files:

### 5.1 References to `PROJECT_STATE_HANDOFF.md`
- `.github/ISSUE_TEMPLATE/codex-task.md` (line 16) — *Compatibility-sensitive: GitHub issue template*
- `AGENTS.md` (lines 13, 17) — *Mandatory boot instruction*
- `README.md` (line 49) — *Human onboarding pointer*
- `docs/CODEX_EXECUTION_GUIDE.md` (line 10)
- `docs/CONTEXT_MAP.md` (lines 11, 117)
- `docs/MASTER_PLAN.md` (line 412)
- `docs/desktop/PREIMPLEMENTATION_AUDIT.md` (line 33)
- `docs/desktop/SESSION_HANDOFF.md` (lines 7, 65) — *Active Desktop DX workflow reference*
- `docs/handoffs/C1_CODEX_BRIEF.md` (line 19)
- `docs/handoffs/NEW_COORDINATOR_SESSION.md` (line 13)

### 5.2 References to `CODEX_EXECUTION_GUIDE.md`
- `git grep -i "CODEX_EXECUTION_GUIDE"` returns **0 references** across the entire repository.
- File is completely orphaned and unlinked.

### 5.3 References to `CONTEXT_MAP.md`
- `.github/ISSUE_TEMPLATE/codex-task.md` (line 17) — *Compatibility-sensitive: GitHub issue template*
- `AGENTS.md` (line 19) — *Mandatory boot instruction*
- `README.md` (line 49)
- `docs/CODEX_EXECUTION_GUIDE.md` (line 12)
- `docs/MASTER_PLAN.md` (lines 409, 425)
- `docs/handoffs/C1_CODEX_BRIEF.md` (line 21)
- `docs/handoffs/NEW_COORDINATOR_SESSION.md` (line 15)

### 5.4 References to root `AGENTS.md`
- `.github/ISSUE_TEMPLATE/codex-task.md` (line 15) — *Compatibility-sensitive: GitHub issue template*
- `README.md` (line 49)
- `docs/CODEX_EXECUTION_GUIDE.md` (line 9)
- `docs/CONTEXT_MAP.md` (lines 5, 10)
- `docs/MASTER_PLAN.md` (line 409)
- `docs/PROJECT_STATE_HANDOFF.md` (lines 5, 326)
- `docs/desktop/PREIMPLEMENTATION_AUDIT.md` (lines 11, 33)
- `docs/desktop/SESSION_HANDOFF.md` (line 7)
- `docs/handoffs/C1_CODEX_BRIEF.md` (line 18)
- `docs/handoffs/NEW_COORDINATOR_SESSION.md` (line 12)
- `tests/unit/test_ci_acceptance_workflow.py` (lines 337, 339, 355) — **Hard test assertion:** checks for "PR Acceptance", "Main Verification", and "two consecutive hosted attempts" in `AGENTS.md`.

### 5.5 References to `CI_AGENT_WORKFLOW.md`
- `AGENTS.md` (line 68) — *Link from CI discipline section*
- `packaging/windows_desktop/README.md` (line 32)
- `tests/unit/test_ci_acceptance_workflow.py` (lines 338, 340-350) — **Hard test assertion:** checks for 8 exact phrases in `CI_AGENT_WORKFLOW.md`.

### 5.6 References to `docs/desktop/SESSION_HANDOFF.md` and `PREIMPLEMENTATION_AUDIT.md`
- Referenced throughout Desktop planning suite: `docs/desktop/README.md`, `docs/desktop/DELIVERY_PLAN.md`, `docs/desktop/ISSUE_MAP.md`, `docs/desktop/ACCEPTANCE_MATRIX.md`.
- Actively referenced by active implementation issues #95–#108 and in-flight PR #145.

### 5.7 Compatibility-Sensitive Inventory
Later CTX checkpoints must preserve compatibility with:
1. `tests/unit/test_ci_acceptance_workflow.py`: Any modification to `AGENTS.md` or `docs/CI_AGENT_WORKFLOW.md` must preserve the required test strings or update the test atomically in the authorized issue.
2. `.github/ISSUE_TEMPLATE/codex-task.md`: Issue template instructs agents to consult `AGENTS.md`, `docs/PROJECT_STATE_HANDOFF.md`, and `docs/CONTEXT_MAP.md`. Pointers must remain functional or be updated in template.
3. Active Desktop PR #145 and issues #95–#108: `docs/desktop/SESSION_HANDOFF.md` and `PREIMPLEMENTATION_AUDIT.md` paths must remain stable and valid while DX track is active.

---

## 6. Load-Bearing Invariant Checklist

The refactor must preserve the following non-negotiable project invariants in immediately visible, always-on context (root `AGENTS.md` or subtree `AGENTS.md`). A model cannot safely infer these from generic software engineering principles:

1. [x] **PostgreSQL is authoritative business state:** Control Plane + PostgreSQL is the sole durable source of truth. In-memory state, WebSockets, and worker local journals are volatile or secondary.
2. [x] **`Command` != `WorkerJob`:** `Command` represents business intent; `WorkerJob` represents remote execution assignment. They must never be collapsed or combined into a single entity.
3. [x] **WorkerJob lease / fencing / checkpoint semantics:** WorkerJob execution is governed by independent lease tokens, monotonic fencing tokens, attempt limits, and safe checkpoint acknowledgement to prevent stale-worker overwrites.
4. [x] **WebSocket is notification / presence only:** WebSocket is never a durable queue or source of truth. Missed notifications or disconnections must recover via PostgreSQL polling and HTTP endpoints.
5. [x] **Persistent account -> worker/profile affinity:** Browser accounts have immutable affinity to a specific worker and logical browser profile; automatic profile migration between workers is prohibited.
6. [x] **Strict execution modes:** Each account operates in exactly one explicit mode (`API_ONLY`, `BROWSER_ONLY`, `HYBRID`, `MANUAL`).
7. [x] **Workers do not invent business actions:** The Control Plane Scheduler alone generates business commands; workers execute bounded, authorized assignments.
8. [x] **API-first principle:** The official Threads API is the preferred executor where supported; browser automation is strictly reserved for capabilities absent from the API.
9. [x] **Browser automation scope boundary:** Browser automation is restricted to approved Worker boundaries and authorized capabilities only (C3+).
10. [x] **Human intervention for login / challenge:** Captchas, 2FA, session challenges, or re-authentication require human operator intervention; automated challenge evasion or bypass is strictly prohibited.
11. [x] **No anti-detect / fingerprint spoofing:** No human-emulation-for-evasion, canvas spoofing, or stealth plugins.
12. [x] **Domain dependency isolation:** Domain code must never import FastAPI, httpx, SQLAlchemy, Playwright/Chromium, WebSocket libraries, Windows APIs, or Meta DTOs.
13. [x] **External side-effect idempotency & recovery:** Network timeouts or ambiguous responses do not prove failure; mutations require idempotency keys, duplicate checks, and explicit reconciliation.
14. [x] **Secret & credential hygiene:** Secrets, OAuth tokens, private keys, proxy credentials, and Authorization headers must never be logged, committed to Git, or exposed in plain repr.
15. [x] **Hosted CI discipline:** Draft PRs run cheap checks; Ready marks merge-authoritative PR Acceptance; exact-SHA checkout; hard stop on hosted failure; escalation after two consecutive failures with the same signature.

---

## 7. Skill Candidate Analysis

Assessment of the six candidate skills proposed in #146:

### 7.1 `postgres-migration`
- **Trigger:** Schema changes, table alterations, new models, index creation, Alembic migration authoring or debugging.
- **Canonical References:** `alembic/`, `src/threads_platform/infrastructure/models.py`, `docs/ACCEPTANCE_AND_REVIEW.md` (Section 3), `scripts/windows_local_preflight.ps1`.
- **Justification:** Repository has specific database practices: reversible migrations, UTC timestamps, claim/routing indexes, test DB spinup via local preflight, avoiding business data loss, schema check gates (`alembic check`).
- **Overlap:** None. Cleanly scoped to database schema operations.
- **Verdict:** **KEEP** as a narrow specialized skill.

### 7.2 `worker-protocol-change`
- **Trigger:** Modifications to Worker WebSocket messages, framing, enrollment handshakes, capacity reporting, or lease renewal payloads.
- **Canonical References:** `docs/protocols/WORKER_PROTOCOL_V1.md`, `src/threads_platform/application/worker_protocol.py`, `src/threads_platform/transport/websocket/server.py`.
- **Justification:** Worker protocol requires strict additive version negotiation (v1 vs v2), schema validation, fencing token propagation, and fail-closed handling of unknown frames. A generic coding agent easily breaks backward compatibility without this guidance.
- **Overlap:** Clear boundary with `postgres-migration` and `browser-capability`.
- **Verdict:** **KEEP** as a narrow specialized skill.

### 7.3 `browser-capability`
- **Trigger:** Authoring or modifying browser automation capabilities (`threads.browser.*`), Playwright selectors, synthetic UI contracts, or staging mutations.
- **Canonical References:** `docs/adr/0005-browser-capability-boundary.md`, `docs/adr/0006-playwright-browser-adapter.md`, `docs/WORKER_BROWSER_CAPABILITY_PACK_V1.md`, `src/threads_platform/workers/browser.py`.
- **Justification:** Browser automation in this repo follows extremely rigorous, unusual rules: no generated CSS classes, bound ancestor depth traversal, exact pathname equality, fail-closed on unknown UI, no auto-click fallback, staged mutation boundaries.
- **Overlap:** Distinct from worker protocol and API contracts.
- **Verdict:** **KEEP** as a narrow specialized skill.

### 7.4 `threads-api-contract`
- **Trigger:** Interacting with official Meta Threads Graph API, webhook handlers, OAuth credential management, or rate limiting.
- **Canonical References:** `docs/THREADS_API_CAPABILITY_SPIKE.md`, `docs/THREADS_CREDENTIAL_OPERATIONS.md`, `docs/THREADS_LIVE_VALIDATION_RUNBOOK.md`, `src/threads_platform/infrastructure/threads_api.py`.
- **Justification:** Distinguishes documentation-contract fixtures from scrubbed live evidence; documents Meta error subcodes, pagination cursors, and the opt-in environment token provider (#70).
- **Overlap:** Completely orthogonal to browser automation and worker protocols.
- **Verdict:** **KEEP** as a narrow specialized skill.

### 7.5 `ci-failure-triage`
- **Trigger:** Hosted GitHub Actions run failure, PR Acceptance triage, interpreting Windows Desktop diagnostic artifacts, classifying failures (product vs harness vs environment).
- **Canonical References:** `docs/CI_AGENT_WORKFLOW.md`, `.github/workflows/pr-acceptance.yml`, `scripts/windows_local_preflight.ps1`.
- **Justification:** Enforces non-negotiable process: mandatory exact-SHA reporting, failure classification before changing code, two-consecutive failure stop rule, server state over UI state.
- **Overlap:** May touch Desktop tests if a Desktop smoke fails, but focuses on CI diagnostics and triage rather than Desktop feature implementation.
- **Verdict:** **KEEP** as a narrow specialized skill.

### 7.6 `desktop-acceptance`
- **Trigger:** Desktop application verification, Tauri/Rust/React integration testing, Windows packaging smoke (`scripts/windows_local_preflight.ps1`), Controller HTTPS trust setup.
- **Canonical References:** `docs/desktop/ACCEPTANCE_MATRIX.md`, `docs/desktop/WINDOWS_REHEARSAL.md`, `packaging/windows_desktop/`.
- **Overlap Analysis:** Risk of overlap with `ci-failure-triage` and `apps/desktop/AGENTS.md`. If it focuses on hosted CI, it duplicates `ci-failure-triage`.
- **Verdict:** **KEEP WITH NARROW SCOPE.** Scope strictly to *Desktop local preflight, packaging verification, and native supervisor lifecycle*, leaving hosted CI triage to `ci-failure-triage`.

---

## 8. Representative Evaluation Fixtures

Twelve representative task fixtures covering diverse repository workflows. These fixtures serve as the benchmark for CTX-02 through CTX-06 to ensure progressive disclosure succeeds without losing critical invariants.

---

### Fixture 1: Trivial / Local Backend Fix
- **Prompt:** "Fix a typo in error message formatting in `src/threads_platform/application/accounts.py` and ensure formatting remains clean."
- **Expected Always-On Files:** Root `AGENTS.md`.
- **Expected Optional Skill:** None.
- **Expected Canonical Docs:** None needed; target source file and unit test only.
- **Files That Should NOT Be Preloaded:** `PROJECT_STATE_HANDOFF.md`, `CONTEXT_MAP.md`, `CI_AGENT_WORKFLOW.md`, `CODEX_EXECUTION_GUIDE.md`, Desktop docs.
- **Critical Invariants to Preserve:** Domain dependency boundary (no infrastructure imports in domain/application), code formatting passes `ruff`.

---

### Fixture 2: Domain Behavior Change
- **Prompt:** "Add a new lifecycle validation rule to `LeadCandidate` in domain logic ensuring disqualified leads cannot transition directly to converted."
- **Expected Always-On Files:** Root `AGENTS.md`.
- **Expected Optional Skill:** None.
- **Expected Canonical Docs:** Target domain file (`domain/leads.py`), corresponding domain tests (`tests/unit/test_leads.py`).
- **Files That Should NOT Be Preloaded:** `PROJECT_STATE_HANDOFF.md`, `CI_AGENT_WORKFLOW.md`, Desktop docs, browser docs.
- **Critical Invariants to Preserve:** Domain dependency boundary (pure domain logic; no FastAPI, httpx, or SQLAlchemy imports).

---

### Fixture 3: PostgreSQL / Alembic Migration
- **Prompt:** "Add an indexed nullable `last_synced_at` column to `accounts` table with a reversible Alembic migration."
- **Expected Always-On Files:** Root `AGENTS.md`.
- **Expected Optional Skill:** `postgres-migration`.
- **Expected Canonical Docs:** `alembic/versions/*`, `src/threads_platform/infrastructure/models.py`.
- **Files That Should NOT Be Preloaded:** `PROJECT_STATE_HANDOFF.md`, Desktop docs, browser docs, `THREADS_API_CAPABILITY_SPIKE.md`.
- **Critical Invariants to Preserve:** PostgreSQL is authoritative business state, migration must be reversible, UTC timestamps, schema check gate (`alembic check`).

---

### Fixture 4: WorkerJob Lease / Fencing Change
- **Prompt:** "Update WorkerJob lease renewal timeout logic in `WorkerControlService` to enforce monotonic fencing token check on heartbeats."
- **Expected Always-On Files:** Root `AGENTS.md`.
- **Expected Optional Skill:** `worker-protocol-change`.
- **Expected Canonical Docs:** `docs/adr/0004-persistent-worker-affinity-and-worker-jobs.md`, `src/threads_platform/application/worker_control.py`, `tests/unit/test_worker_control.py`.
- **Files That Should NOT Be Preloaded:** `PROJECT_STATE_HANDOFF.md`, Desktop docs, browser capability pack, `THREADS_API_CAPABILITY_SPIKE.md`.
- **Critical Invariants to Preserve:** `Command != WorkerJob`, WorkerJob independent lease/fencing/checkpoint semantics, stale lease claim must fail closed.

---

### Fixture 5: Worker Protocol / Auth / Drain Change
- **Prompt:** "Extend Worker protocol v2 to add an explicit `drain_reason` field to the worker draining status frame."
- **Expected Always-On Files:** Root `AGENTS.md`.
- **Expected Optional Skill:** `worker-protocol-change`.
- **Expected Canonical Docs:** `docs/protocols/WORKER_PROTOCOL_V1.md` (v2 extension section), `src/threads_platform/application/worker_protocol.py`.
- **Files That Should NOT Be Preloaded:** `PROJECT_STATE_HANDOFF.md`, Desktop docs, browser adapter files.
- **Critical Invariants to Preserve:** WebSocket is notification/presence only (not durable queue), additive protocol versioning, fail-closed handling on unknown fields.

---

### Fixture 6: Browser Capability Contract
- **Prompt:** "Implement a read-only browser capability for recognizing the target user's follower count from public profile header."
- **Expected Always-On Files:** Root `AGENTS.md`.
- **Expected Optional Skill:** `browser-capability`.
- **Expected Canonical Docs:** `docs/adr/0005-browser-capability-boundary.md`, `docs/adr/0006-playwright-browser-adapter.md`, `docs/WORKER_BROWSER_CAPABILITY_PACK_V1.md`.
- **Files That Should NOT Be Preloaded:** `PROJECT_STATE_HANDOFF.md`, `CI_AGENT_WORKFLOW.md`, `THREADS_API_CAPABILITY_SPIKE.md`, Desktop UI docs.
- **Critical Invariants to Preserve:** Fail-closed on UI mismatch, no generated CSS selectors, bound ancestor traversal, human intervention on challenge/login, no anti-detect/evasion.

---

### Fixture 7: External Meta Threads API Contract
- **Prompt:** "Handle Meta Graph API subcode 2207051 (token expired) in Threads API client to trigger re-authorization event."
- **Expected Always-On Files:** Root `AGENTS.md`.
- **Expected Optional Skill:** `threads-api-contract`.
- **Expected Canonical Docs:** `docs/THREADS_API_CAPABILITY_SPIKE.md`, `docs/THREADS_CREDENTIAL_OPERATIONS.md`, `src/threads_platform/infrastructure/threads_api.py`.
- **Files That Should NOT Be Preloaded:** `PROJECT_STATE_HANDOFF.md`, Desktop docs, browser capability docs, `WORKER_PROTOCOL_V1.md`.
- **Critical Invariants to Preserve:** API-first executor preference, official Meta API behavior verification, test fixtures labeled `documentation-contract` vs scrubbed live.

---

### Fixture 8: Desktop React / UI Change
- **Prompt:** "Update the Operator Console worker status card in React to display the worker node's active connection protocol version."
- **Expected Always-On Files:** Root `AGENTS.md`, `apps/desktop/AGENTS.md` (once introduced).
- **Expected Optional Skill:** None (handled by local `apps/desktop/AGENTS.md`).
- **Expected Canonical Docs:** `apps/desktop/src/components/*`, `apps/desktop/src/desktop.ts`.
- **Files That Should NOT Be Preloaded:** `PROJECT_STATE_HANDOFF.md`, `docs/desktop/PREIMPLEMENTATION_AUDIT.md`, `WORKER_BROWSER_CAPABILITY_PACK_V1.md`, Python backend docs.
- **Critical Invariants to Preserve:** Server authorization state over UI Automation tree presence, UI session lock semantics.

---

### Fixture 9: Desktop Rust / Native Lifecycle Change
- **Prompt:** "Adjust Tauri supervisor process tree teardown in `supervisor.rs` to ensure PostgreSQL child process is gracefully stopped on explicit app Quit."
- **Expected Always-On Files:** Root `AGENTS.md`, `apps/desktop/AGENTS.md` (once introduced).
- **Expected Optional Skill:** `desktop-acceptance`.
- **Expected Canonical Docs:** `docs/adr/0007-windows-first-single-app-desktop.md`, `apps/desktop/src-tauri/src/supervisor.rs`.
- **Files That Should NOT Be Preloaded:** `PROJECT_STATE_HANDOFF.md`, Python scheduler docs, browser capability docs, Meta API docs.
- **Critical Invariants to Preserve:** Graceful Quit vs crash handling, PostgreSQL WAL recovery, same-user DPAPI isolation, dedicated Windows runtime user.

---

### Fixture 10: Hosted CI Failure Triage
- **Prompt:** "PR #145 failed on hosted `PR Acceptance` in `Desktop Diagnostic (windows-latest)` with error `ProcessExitCodeException: 1` during Controller HTTPS smoke."
- **Expected Always-On Files:** Root `AGENTS.md`.
- **Expected Optional Skill:** `ci-failure-triage`.
- **Expected Canonical Docs:** `docs/CI_AGENT_WORKFLOW.md`, workflow run artifacts / logs.
- **Files That Should NOT Be Preloaded:** `PROJECT_STATE_HANDOFF.md`, `docs/desktop/PREIMPLEMENTATION_AUDIT.md`, unrelated domain docs.
- **Critical Invariants to Preserve:** Draft PR discipline, record primary failure signature, classify as product/harness/env, two-consecutive identical signatures require coordinator escalation, no speculative retries.

---

### Fixture 11: Docs-Only Change
- **Prompt:** "Fix a broken markdown anchor in `docs/CONTROL_PLANE_DEPLOYMENT_RUNBOOK.md`."
- **Expected Always-On Files:** Root `AGENTS.md`.
- **Expected Optional Skill:** None.
- **Expected Canonical Docs:** Target document only (`docs/CONTROL_PLANE_DEPLOYMENT_RUNBOOK.md`).
- **Files That Should NOT Be Preloaded:** `PROJECT_STATE_HANDOFF.md`, `CONTEXT_MAP.md`, `CI_AGENT_WORKFLOW.md`, Desktop docs.
- **Critical Invariants to Preserve:** Documentation integrity, no product or runtime behavior change.

---

### Fixture 12: Architecture & Security Review
- **Prompt:** "Review a proposed design for remote Worker enrollment via mobile QR code for potential RBAC bypass or token leakage."
- **Expected Always-On Files:** Root `AGENTS.md`.
- **Expected Optional Skill:** `worker-protocol-change`.
- **Expected Canonical Docs:** `docs/adr/0004-persistent-worker-affinity-and-worker-jobs.md`, `docs/protocols/WORKER_PROTOCOL_V1.md`, `docs/ACCEPTANCE_AND_REVIEW.md` (Section 6).
- **Files That Should NOT Be Preloaded:** `PROJECT_STATE_HANDOFF.md`, browser automation docs, React UI files.
- **Critical Invariants to Preserve:** 256-bit entropy for enrollment tokens, no static shared passwords, no private keys in DB/logs, server-derived actor attribution (no caller-supplied audit identity).

---

## 9. Migration Risk Register

Risks identified for the upcoming CTX refactoring checkpoints (#148–#152) and their mitigations:

| ID | Risk | Severity | Impact | Mitigation Strategy |
|---|---|---|---|---|
| **R-01** | **Loss of load-bearing invariants** | **HIGH** | Trimming `AGENTS.md` might accidentally remove subtle invariants (e.g. lease fencing, domain isolation, human intervention) leading to architecture drift. | Strict check against Section 6 Load-Bearing Invariant Checklist in every CTX PR. Root `AGENTS.md` retains invariants while shedding workflow manuals. |
| **R-02** | **Overly broad skills triggering universally** | **MEDIUM** | If skill descriptions are generic, agents load them for every task, recreating context bloat under a new name. | Narrow trigger conditions defined in Section 7. Reject generic skills ("clean-code", "python-style"). Ensure each skill has exclusive trigger keywords. |
| **R-03** | **Stale GitHub / live-state coupling** | **HIGH** | Documentation referencing specific PR numbers, commit SHAs, or completed issues quickly goes out of date, confusing coding agents. | Move dynamic milestone history out of the mandatory boot path. Rely on git history and GitHub issue state rather than maintaining a monolithic handoff document. |
| **R-04** | **Breaking active Desktop links / DX workflow** | **HIGH** | Moving or renaming `SESSION_HANDOFF.md` or `PREIMPLEMENTATION_AUDIT.md` breaks in-flight Desktop issues (#95–#108) and PR #145. | Maintain backward-compatible paths and stubs until active Desktop implementation reaches an explicit resting checkpoint. |
| **R-05** | **Altering product semantics during doc cleanup** | **HIGH** | Re-phrasing architecture or protocol summaries might inadvertently alter lease times, security policies, or execution rules. | Strictly enforce CTX rule: CTX checkpoints are pure context/documentation restructuring; zero changes to accepted ADR contracts, code, schemas, or protocols. |
| **R-06** | **Introducing a second source of truth** | **MEDIUM** | Skills that copy protocol specs rather than pointing to canonical documents will drift when protocols change. | Skills act as routers/checklists pointing to canonical docs (`docs/protocols/*`, `docs/adr/*`), never duplicate full specifications. |
| **R-07** | **Breaking unit test assertions** | **MEDIUM** | `tests/unit/test_ci_acceptance_workflow.py` asserts exact string presence in `AGENTS.md` and `docs/CI_AGENT_WORKFLOW.md`. Modifying them naively breaks the test gate. | Track exact string dependencies (Section 5.4, 5.5). When refactoring `AGENTS.md` in CTX-03, adjust the test assertions atomically in that checkpoint. |
