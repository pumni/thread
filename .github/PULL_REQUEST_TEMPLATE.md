## Summary

Describe what changed and why.

## Issue

Closes #

## Scope

- In scope:
- Out of scope:

## Architecture

Which modules/ports/adapters changed? Which ADRs apply?

## Verification

- [ ] uv sync --locked
- [ ] uv run ruff check .
- [ ] uv run ruff format --check .
- [ ] uv run pyright
- [ ] uv run pytest
- [ ] Relevant integration tests

Paste concise results or CI links.

## Failure cases tested

List retry/error/recovery scenarios relevant to this change.

## External API assumptions

Document any Threads API behavior relied on and when it was verified.

## Security

- [ ] No secrets/tokens committed or logged
- [ ] Sensitive headers/fields are redacted
- [ ] OAuth scope changes are explained

## Risks / follow-ups

List intentionally deferred work.
