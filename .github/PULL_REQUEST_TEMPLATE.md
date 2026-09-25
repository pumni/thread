## Summary

Describe what changed and why.

## Issues / checkpoint

- Closes/relates to:
- Authorized batch:

## Scope

- In scope:
- Out of scope:

## Architecture / ADRs

Which modules, ports, state machines and ADRs changed?

## Data / migrations

- Revision(s):
- Constraints/indexes:
- Upgrade/downgrade/alembic check evidence:

## Protocol

- CRM protocol changes:
- Worker protocol changes:
- Compatibility/version implications:

## Verification

- [ ] uv sync --locked
- [ ] uv run ruff check .
- [ ] uv run ruff format --check .
- [ ] uv run pyright
- [ ] uv run alembic check
- [ ] uv run pytest
- [ ] Relevant PostgreSQL integration tests
- [ ] git diff --check

Paste concise results and CI state.

## Concurrency / failure / recovery

List duplicate delivery, concurrent claim, lease expiry/reclaim, stale owner, disconnect/reconnect, restart, timeout/ambiguity, intervention and cancellation/preemption cases relevant to this PR.

## Security

- [ ] No secrets/tokens/private keys committed or logged
- [ ] Sensitive headers/fields redacted
- [ ] Worker/account/session identity handling reviewed where relevant
- [ ] No anti-detect/fingerprint-evasion behavior introduced

## Browser-specific gate (if applicable)

- [ ] Browser work is authorized for this checkpoint
- [ ] Capability is explicit/versioned
- [ ] UI mismatch fails closed
- [ ] Session/challenge maps to intervention
- [ ] No browser dependency leaks into domain/application business models

## External API evidence

Document current Threads API behavior relied on and whether evidence is documentation-contract or scrubbed live evidence.

## Risks / follow-ups

List intentionally deferred work and open production gates.