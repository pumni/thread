---
name: browser-capability
description: Author or review fail-closed Threads browser capabilities, Playwright recognition contracts, and staged mutations. Not for official Meta API integration.
---

# Browser Capability Skill

## Purpose
Guide the development and review of Worker browser automation capabilities, ensuring strict fail-closed DOM recognition, human intervention boundaries, and staged mutation safeguards without copying capability-specific selector contracts into generic procedure.

## Trigger Conditions
Use this skill when a task involves:
- Implementing, editing, or testing browser capabilities in `src/threads_platform/workers/browser.py`.
- Defining or refining synthetic DOM recognition contracts for Threads web UI interactions.
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
- Canonical tests: `tests/unit/test_browser_adapter.py`, `tests/unit/test_browser_capabilities.py`, or the unit test matching the specific capability (e.g. `tests/unit/test_feed_browse.py`, `test_thread_open.py`, `test_profile_open.py`, `test_media_local_upload.py`).

## Invariants & Design Rules
1. **Explicit & Versioned Capabilities:** Every browser action corresponds to an explicit, versioned capability approved in architecture.
2. **Fail-Closed Recognition:** Never guess selectors or use unstable generated CSS classes. Ambiguous elements, redirects, unexpected DOM layouts, or missing target associations must fail closed.
3. **Strict Human Intervention Boundary:** Captchas, 2FA, session challenges, login prompts, and account checkpoints require human operator intervention. Automated evasion, stealth plugins, and fingerprint spoofing are strictly prohibited.
4. **Staged Mutation Safeguards:** Any browser action causing external mutations must establish pre-action validation, monitor network boundaries, and provide explicit reconciliation for ambiguous outcomes.
5. **Lease Loss Discipline:** Lease expiration or heartbeat loss immediately blocks new irreversible browser actions.

## Step-by-Step Procedure
1. **Inspect canonical contract:** Review the target capability specification in `docs/WORKER_BROWSER_CAPABILITY_PACK_V1.md` and inspect existing implementation in `src/threads_platform/workers/browser.py`.
2. **Consult matching tests:** Examine existing contract tests for the capability (e.g. `tests/unit/test_browser_capabilities.py` or capability-specific unit tests).
3. **Implement fail-closed behavior:** Ensure unmatched, ambiguous, or challenged states produce durable intervention requests or typed uncertain errors (`REMOTE_STATE_UNCERTAIN`).
4. **Verify locally:**
   ```bash
   uv run pytest tests/unit/test_browser_adapter.py tests/unit/test_browser_capabilities.py
   ```
