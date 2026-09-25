# ADR-0005: Browser Capability Boundary and Human Intervention

- Status: Accepted
- Date: 2026-09-25

## Context

The product requires browser/session capabilities that do not map cleanly to the current official API. The legacy system mixed DOM selectors, anti-detect behavior, account credentials and business logic in one large script.

## Decision

Browser automation is allowed only through isolated Worker/browser adapters and explicit capabilities.

Rules:
- capability has a business outcome and version;
- browser job runs through WorkerJob;
- account affinity is enforced;
- session state is explicit;
- initial login is operator-assisted;
- challenge/session-expired states create interventions;
- UI mismatch fails closed;
- selectors/DOM types do not leak into domain/application contracts;
- retries are bounded and side-effect ambiguity is reconciled;
- AccountActivityPlan decomposes into explicit scheduled jobs.

## Excluded objectives

- anti-detect/fingerprint spoofing;
- randomized human-like interaction for evasion;
- automatic challenge/2FA bypass;
- storing plaintext account passwords as the default session model;
- click-anything fallback behavior.

## Consequences

Browser features are more maintainable and auditable than the legacy tool, but some actions may intentionally stop and require operator intervention when UI/session contracts change.
