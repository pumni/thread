---
name: threads-api-contract
description: Integrate or update official Meta Threads Graph API client endpoints, OAuth credential handling, and API test fixtures. Not for browser scraping.
---

# Threads API Contract Skill

## Purpose
Guide interactions with the official Meta Threads Graph API, ensuring compliance with Meta developer contracts, proper error subcode handling, credential security, and fixture classification.

## Trigger Conditions
Use this skill when a task involves:
- Modifying HTTP client calls or response parsing in `src/threads_platform/infrastructure/threads_api/client.py`.
- Implementing or changing OAuth credential management in `src/threads_platform/infrastructure/threads_api/credentials.py` or `composition.py`.
- Handling Meta Graph API error codes, subcodes (e.g., token expiry), pagination cursors, or rate limits.
- Authoring or updating Threads API documentation-contract fixtures or verified live evidence fixtures.

## Non-Trigger Conditions
Do NOT use this skill for:
- Web browser scraping or Playwright capabilities (use `browser-capability`).
- Modifying database schemas or Alembic tables for credentials (use `database-migration`).
- Worker-to-Controller WebSocket communication (use `worker-protocol`).

## Canonical References
Inspect these sources before modifying API client contracts:
- `docs/THREADS_API_CAPABILITY_SPIKE.md`.
- `docs/THREADS_CREDENTIAL_OPERATIONS.md`.
- `docs/THREADS_LIVE_VALIDATION_RUNBOOK.md`.
- `src/threads_platform/infrastructure/threads_api/client.py`.
- `src/threads_platform/infrastructure/threads_api/credentials.py`.
- Official Meta Threads Developer Documentation and Changelog.
- Tests: `tests/unit/test_threads_discovery_contract.py`, `tests/unit/test_threads_api_contract.py`.

## Invariants & Design Rules
1. **API-First, Not API-Only:** Prefer the official Threads API where it is the suitable and approved executor, while respecting account execution modes (`API_ONLY`, `BROWSER_ONLY`, `HYBRID`, `MANUAL`). Bounded `HYBRID` fallback must follow capability policy, not arbitrary error inference.
2. **External Contract Fidelity:** When changing external API handling, verify current official Meta developer documentation and changelog. Do not invent undocumented response fields.
3. **Fixture Classification:** Explicitly distinguish `documentation-contract` fixtures (derived from published API documentation) from scrubbed live evidence fixtures.
4. **Secret Protection:** Never log, store in git, or return in error traces any access tokens, client secrets, or authorization headers.
5. **Idempotency & Retry Discipline:** HTTP 5xx or network timeouts do not confirm failure; use idempotency keys where supported and bounded retries with exponential backoff.

## Step-by-Step Procedure
1. **Verify official Meta contract:** Check official Meta Threads documentation for the endpoint, parameter specifications, and error subcodes.
2. **Update client implementation:** Modify `src/threads_platform/infrastructure/threads_api/client.py` using typed request/response models.
3. **Update credential handling:** If token refresh or rotation is involved, ensure credentials update atomically without leaking tokens.
4. **Verify tests:**
   ```bash
   uv run pytest tests/unit/test_threads_discovery_contract.py tests/unit/test_threads_api_contract.py
   ```
