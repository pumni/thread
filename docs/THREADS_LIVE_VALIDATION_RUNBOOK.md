# Threads Live Validation Runbook

**Status:** Preparation instructions for a future human-run TP-002 validation. The #65 tooling checkpoint contains **NO LIVE EVIDENCE**, uses **NO REAL CREDENTIALS**, and makes **NO NETWORK CALLS TO META**. A checked-in template is not a test result.

## Purpose and boundary

Use this runbook only after a coordinator authorizes the live #3 session and a human operator has a dedicated Meta development app and non-production test account. The evidence harness in `threads_platform.tools.threads_live_evidence` is an offline packet validator and fingerprint utility. It does not authorize or perform API requests, create accounts, store credentials, host OAuth callbacks, or infer readiness from successful cases.

Before the session, review current official Threads developer documentation and changelog. The official Meta Postman workspace can help locate requests, but it may lag the changelog and is not proof of current runtime behavior. Record only the safe HTTPS source references actually reviewed.

## Prerequisites

- Coordinator authorization for the human live run under #3.
- A dedicated, non-production Meta development app and test account with permission to exercise the approved cases.
- A named operator and an independent reviewer, represented in the packet only by short aliases.
- A dedicated operator machine with disk encryption, current endpoint protection, a protected password manager or OS credential store, and an approved local API client.
- Current official developer documentation/changelog reviewed alongside the official Postman workspace where useful.
- The checked-in packet template and offline validator available locally. CI must not receive credentials or contact Meta.
- A low-impact test plan for media, publishing, replies, quotas, and moderation. Use only content and targets owned by or explicitly approved for the test account.

Do not start if the app/account is production, the operator cannot protect temporary credentials, or any requested test would require bypassing an authentication challenge, manufacturing load, or contacting an unapproved account.

## Manual/browser authorization boundary

The human operator starts authorization through Meta's official developer flow in a trusted browser profile on the dedicated machine. A human handles consent, login, and any challenge. Do not automate login, bypass 2FA or challenges, host a callback for this harness, or send credentials to Codex, CI, issue comments, chat, or the repository.

Perform token exchange, long-lived exchange, refresh, and debugger checks manually in an approved local API client. If using Postman, put temporary values only in its local secret/vault facility. Do not export an environment containing secret-bearing values. Do not paste secrets into shell commands, command arguments, scripts, source files, test fixtures, or terminal prompts that echo input.

Temporary credentials may exist only in the trusted browser's in-memory authorization flow, the operator's protected local password/OS secret store, or the approved API client's local secret vault while the human session is active. Avoid writing raw requests, responses, redirects, or tokens to disk. Clear local client variables and the clipboard after the session using the client's supported controls. The evidence harness has no credential configuration and must not be given credentials.

## Evidence handling

### Never copy into the packet, Git, logs, screenshots, issue comments, or PR text

- Access tokens, app/client secrets, authorization codes, refresh tokens, or any other credential value.
- Raw `Authorization` or `Proxy-Authorization` headers, including bearer values.
- Postman environment exports or screenshots containing secret-bearing values.
- Raw sensitive account IDs, account-specific usernames, email addresses, or personal names.
- Raw opaque cursors, cursor-bearing request URLs, or full URLs with query values.
- Raw request or response bodies, full HTTP transcripts, or error message text that may identify an account.
- Signed or account-specific media URLs, including temporary upload/download links.
- Credential-bearing browser/client/terminal screenshots or terminal history containing credentials.
- App IDs, client IDs, account identifiers, cookies, authorization state, or private app configuration.

### Safe packet observations

The packet may contain only scrubbed observations: the UTC observation time; operator/reviewer aliases; the dedicated development/test environment classification; reviewed official HTTPS source references; safe API host/version labels; effective scope **names**; token class, expiry/validity metadata without a token value; case IDs and endpoint templates; parameter names and whether `after` was present; HTTP status; presence-only observations for allowlisted safe headers; response field-name/shape observations; item counts; cursor/page booleans and SHA-256 fingerprints; error category/field-presence flags; PASS/FAIL/BLOCKED/NOT_AVAILABLE; and short scrubbed notes.

Endpoint values must remain templates such as `/me`, `/profile_lookup`, `/me/mentions`, `/{thread_id}/replies`, and `/{thread_id}/conversation`. Never replace placeholders with real IDs or usernames. Record only whether safe headers were present; do not copy their raw values. For errors, record category and safe shape flags, not the message or payload.

## Scrub-before-commit workflow

1. Start from `docs/examples/threads-live-evidence-v1.template.json`. Save a working copy outside the repository or in an access-restricted local directory. Keep no raw transcript alongside it.
2. Perform one authorized case at a time in the official browser/client. Observe the status and shape, then enter only the safe observations listed above. If a response contains account-specific or opaque content, do not paste it into the packet.
3. For cursor equality comparisons, pipe the value to standard input and retain only the printed fingerprint. On Windows, if the cursor is already in the local clipboard, use `Get-Clipboard -Raw | uv run python -m threads_platform.tools.threads_live_evidence fingerprint`; the command has no cursor argument and the helper prints only the fingerprint. Avoid typing or pasting an opaque value into an echoing terminal. A fingerprint is deterministic comparison metadata, not authentication material.
4. Set `packet_classification` to `SCRUBBED_LIVE_EVIDENCE` only for an operator-completed packet. Fill observation time in UTC, aliases, dedicated test environment, source references, and the full case matrix. Leave unrun or unsafe cases `BLOCKED` or `NOT_AVAILABLE`; never make up a result.
5. Run the offline validator on the working packet:

   ```powershell
   uv run python -m threads_platform.tools.threads_live_evidence validate path\to\packet.json
   ```

   The validator performs no network calls. It rejects unknown fields, secret-like fields/patterns, unsafe URLs, raw cursor fields, malformed fingerprints, duplicate cases, invalid timestamps, and unsupported shapes. A successful validation is not coordinator acceptance and does not itself mark any readiness state.

6. Have the independent reviewer inspect the packet and compare it against the official sources and the executed case checklist. Reviewer edits should remain limited to safe observations and review classifications.
7. Before staging, inspect the exact packet and Git diff. Search changed files for credential/header/token patterns and inspect matches in context. Run the validator again on the exact file that would be committed. If a secret or raw sensitive value was copied into a repository file, stop; do not stage it. Follow the operator's incident/rotation procedure if a real credential may have been exposed.
8. Commit only the validated scrubbed packet when a later authorized issue requests it. This #65 PR commits only the template, never a completed packet.

The helper cannot prove that arbitrary free-form text is fully scrubbed. Keep notes short, use the structured fields, and rely on independent review in addition to validation.

## Validation phases

Use one case per matrix row in the packet. Record only observations from the authorized dedicated test account. Every row begins as `NOT_AVAILABLE`; update it only after the human observed that case.

### Phase A — token critical path (`A01`–`A06`)

1. Authorization and code exchange (`A01`): human authorization in the trusted browser and manual code exchange. Record flow outcome, status, safe response shape, and effective scope names only.
2. Long-lived token exchange (`A02`) and refresh (`A03`): record whether each step succeeds, status, safe expiry metadata, and whether the resulting token remains valid. Never copy token values.
3. Token debugger/effective scopes (`A04`) and expiry/validity (`A05`): record scope names and safe validity/expiry metadata only.
4. Own profile (`A06`): record status and returned field names/shape. Do not copy the profile ID, username, biography, or media URL.

Phase A evidence is a prerequisite for coordinator consideration of `TOKEN_PROVIDER_READY_EVIDENCE` and later #55 work. It does not authorize implementation or production activation by itself.

### Phase B — polling and cursor critical path (`B01`–`B13`)

1. Keyword/tag search (`B01`), public profile lookup (`B02`), profile posts (`B03`), mentions (`B04`), replies (`B05`), and flattened conversation (`B06`). Record request shape, status, field presence, item count, and endpoint limits without recording search text, usernames, or IDs.
2. `after` request parameter behavior (`B07`) and `paging.cursors.after` observation (`B08`). Record whether the parameter/cursor was present and fingerprint opaque cursor values instead of copying them.
3. Terminal-page behavior (`B09`), repeated-cursor behavior (`B10`), and cursor behavior across separate runs (`B11`). Record only boolean relationships and fingerprints needed to compare equality/change.
4. `owner.id` availability/authorization (`B12`) and endpoint-specific limits (`B13`). Record field availability and normalized counts, never the owner ID.

Phase B evidence is a prerequisite for coordinator consideration of `POLLING_CURSOR_READY_EVIDENCE` and any future discovery/mentions scheduling policy under #9. It does not authorize a cursor policy by itself.

### Phase C — publishing, replies, and media (`C01`–`C10`)

1. Text publish (`C01`), image publish (`C02`), and video publish (`C03`) using approved test content and the dedicated account.
2. Published-media retrieval (`C04`), reply (`C05`), reply-to-reply (`C06`), and container/media state (`C07`).
3. Publishing quota (`C08`) and moderation permissions (`C09`).
4. Ambiguous publish reconciliation (`C10`) only where an ambiguity occurs naturally or can be examined safely without creating duplicate public content. Do not intentionally drop responses to create ambiguity.

Record status, safe response/header shape, field/state names, and presence flags. Do not copy media links, uploaded content URLs, post IDs, full payloads, or account-specific values.

### Phase D — representative failures (`D01`–`D05`)

Use only failures that can be observed safely in the dedicated development setup: authentication (`D01`), permission (`D02`), validation (`D03`), rate/quota (`D04`), and server failure (`D05`). Record status, safe header presence, error category, and whether safe error fields were present. Do not abuse the service, rate-flood, or attempt to manufacture a 5xx. Leave rate/quota or server cases `NOT_AVAILABLE` when not naturally observable.

## Review procedure

The independent reviewer checks:

- The packet validates offline and retains the exact schema version and full ordered A-D matrix.
- Classification is `SCRUBBED_LIVE_EVIDENCE`, observation time is UTC, aliases are non-identifying, and the environment is the dedicated Meta development app/test account.
- Official source references use HTTPS, have no credentials or secret-bearing query, and include current developer documentation/changelog review. Postman material is corroborating request guidance only.
- Endpoint values are templates. No account IDs, usernames, raw cursor values, media URLs, full request/response bodies, credential values, or sensitive header values appear anywhere.
- Every PASS/FAIL is supported by an observed status; skipped cases remain BLOCKED/NOT_AVAILABLE with no fabricated observations.
- Readiness flags are false unless a reviewer deliberately sets them with a UTC review time and rationale after checking the complete required case set. One passing case cannot set readiness.
- The exact staged diff and commit contain only the approved scrubbed packet and no screenshots, exports, raw transcripts, or credentials.

## Coordinator acceptance procedure

Submit the reviewed scrubbed packet for #3 coordinator review using the authorized repository process. The coordinator independently checks source freshness, Phase A/B/C coverage, safe failure evidence, readiness rationale, and whether any external behavior contradicts the current contract. The packet's readiness booleans are review classifications, not auto-derived success flags.

Coordinator acceptance of Phase A may unblock a separate #55 decision. Acceptance of Phase B may support a separately authorized discovery/mentions scheduling decision under #9. Only completion and acceptance of the full #3 gate can close the live validation gate or support `FULL_TP002_READY`. This #65 tooling checkpoint leaves #3 OPEN, #55 blocked, and discovery/mentions scheduler policy blocked.

## Validator threat model and limits

The validator scans every nested mapping, array, and string before strict Pydantic parsing. It rejects known credential-bearing field names, authorization headers, bearer/token-prefix patterns, secret-bearing URLs, raw cursor fields, account-specific identifier/username patterns, unsupported fields, invalid source references, malformed fingerprints, duplicate case IDs, non-UTC times, and readiness claims unsupported by the required reviewed case set. Diagnostics show only a safe location and violation class; they never include input values.

This is a fail-closed packet guard, not a general data-loss-prevention engine. It cannot recognize every arbitrary secret, identifier, username, private fact, or credential copied into innocent-looking prose. Structured observations and human review remain required. SHA-256 fingerprints reveal equality across packets and may permit guessing for low-entropy values; use them only for opaque cursor/request identity comparison and never as authentication material.
