---
name: browser-capability
description: Use this skill when implementing, extending, or reviewing fail-closed Threads browser automation capabilities (threads.browser.*), Playwright locators, synthetic DOM recognition contracts, operator-assisted mutations, or browser session reconciliation. Do NOT use for official Threads Graph API calls, worker WebSocket framing, or Tauri desktop shell development.
---

# Browser Capability Skill

## Purpose
Guide the development and review of Worker browser automation capabilities, ensuring strict fail-closed DOM recognition, human intervention boundaries, and staged mutation safeguards.

## Trigger Conditions
Use this skill when a task involves:
- Implementing, editing, or testing browser capabilities in `src/threads_platform/workers/browser.py`.
- Defining or refining synthetic DOM contracts for Threads web UI interactions (feed browse, thread open, profile open, media upload).
- Adding or modifying Playwright locator strategies, bounded ancestor traversals, or response recognition.
- Handling browser session challenges, login state detection, or uncertain outcomes.

## Non-Trigger Conditions
Do NOT use this skill for:
- Official Meta Threads Graph API client integration (use `threads-api-contract`).
- Worker WebSocket protocol framing or lease renewal (use `worker-protocol`).
- Tauri desktop shell, supervisor, or React Console development (use `apps/desktop/AGENTS.md`).

## Canonical References
Inspect these sources before modifying browser capabilities:
- `docs/adr/0005-browser-capability-boundary.md`.
- `docs/adr/0006-playwright-browser-adapter.md`.
- `docs/WORKER_BROWSER_CAPABILITY_PACK_V1.md`.
- `src/threads_platform/workers/browser.py`.
- Contract & adapter tests: `tests/unit/test_worker_browser_contract.py` (or existing browser tests in `tests/`).

## Invariants & Design Rules
1. **Fail-Closed DOM Matching:** Never guess selectors or rely on dynamic generated CSS classes. Match exact target paths, verified semantic tags, and explicit link hrefs.
2. **Bounded Traversal:** Enforce strict ancestor traversal limits (e.g., maximum depth bound of 8). Over-bound or ambiguous matches must fail closed.
3. **Strict Human Intervention Boundary:** Captchas, 2FA, session challenges, login prompts, and account checkpoints require human operator intervention. Automated evasion, stealth plugins, and fingerprint spoofing are strictly prohibited.
4. **Staged Mutation Discipline:** Actions causing external side-effects (e.g., image upload) must prove required state (dialog, file input) before initiating action, arm network observers, and verify server HTTP 200 before claiming success.
5. **No Irreversible Actions on Stale Lease:** Lease expiration or heartbeat failure must immediately halt browser mutations. Ambiguous outcomes produce `AMBIGUOUS_OUTCOME` or `RECONCILIATION_REQUIRED`.

## Step-by-Step Procedure
1. **Inspect existing contracts:** Review patterns in `docs/WORKER_BROWSER_CAPABILITY_PACK_V1.md` and current capabilities in `src/threads_platform/workers/browser.py`.
2. **Draft locator contract:** Define exact normalized pathnames, required semantic elements (`h1`, `dir="auto"` spans), and exact href associations.
3. **Implement fail-closed handler:** Ensure unmatched, ambiguous, or redirected states return explicit typed errors (`REMOTE_STATE_UNCERTAIN` or intervention).
4. **Verify against synthetic contracts:**
   ```bash
   uv run pytest tests/unit/test_worker_agent_foundation.py
   ```
