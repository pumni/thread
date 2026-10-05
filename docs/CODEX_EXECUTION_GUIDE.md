# Codex Execution Guide (Deprecated / Compatibility Router)

> [!NOTE]
> **DEPRECATED — USE CANONICAL PROCESS & CONTEXT MAP**
> This standalone guide has been deprecated to eliminate duplicated policies and maintain single canonical homes for all engineering standards.
> Coding agents must follow the live progressive disclosure context layer instead of relying on static guide copies.

## Canonical Homes for Engineering Standards

| Topic / Requirement | Canonical Home | Purpose |
|---|---|---|
| **Live Task Authorization & Current State** | Authorized GitHub issue / coordinator instruction | Sole source of live task authority. |
| **Global Architectural & Security Invariants** | Root [`AGENTS.md`](../AGENTS.md) | Universal laws, domain boundaries, stop conditions. |
| **Component / Subtree Instructions** | Nearest nested `AGENTS.md` (e.g. [`apps/desktop/AGENTS.md`](../apps/desktop/AGENTS.md)) | Local subsystem guidelines and build constraints. |
| **Task & Capability Documentation Routing** | [`docs/CONTEXT_MAP.md`](CONTEXT_MAP.md) | JIT capability-based document and skill router. |
| **Specialized Workflows & Wire Protocols** | Project Skills ([`.agents/skills/`](../.agents/skills/)) | Specialized playbooks for DB migrations, worker protocol, browser capabilities, etc. |
| **Hosted CI Failure Triage & Diagnostics** | [`docs/CI_AGENT_WORKFLOW.md`](CI_AGENT_WORKFLOW.md) & `ci-failure-triage` | Merge-authoritative CI gate, exact-SHA reporting, two-strike stop rule. |
| **Review Severity & Acceptance Gates** | [`docs/ACCEPTANCE_AND_REVIEW.md`](ACCEPTANCE_AND_REVIEW.md) | Gate criteria (Architecture, Data, Reliability, Security, Browser). |
| **Durable Architectural Decisions** | Accepted ADRs ([`docs/adr/`](adr/)) | Binding architectural contracts. |
| **Task Contract & Deliverables Format** | [`.github/ISSUE_TEMPLATE/codex-task.md`](../.github/ISSUE_TEMPLATE/codex-task.md) | Task specification and acceptance checklist. |
| **PR Evidence & Verification Structure** | [`.github/PULL_REQUEST_TEMPLATE.md`](../.github/PULL_REQUEST_TEMPLATE.md) | Mandatory evidence-capture format for pull requests. |
| **Historical Milestone Archive** | [`docs/handoffs/PROJECT_STATE_HISTORY_2026-10.md`](handoffs/PROJECT_STATE_HISTORY_2026-10.md) | Milestone chronology for Batches A/B, TP-004A, C1–C6. |
