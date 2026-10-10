# Standalone Nurture — operator runbook (SN-14)

**Status:** partial SN-14 acceptance, not a production activation guide. Accepted source baseline: `main@0dfbd36ff3756f8a68edcd6cc0bd083f6f3570a1`. This runbook records the safe one-shot operator flow; #229 owns the remaining acceptance gates, #80 the wider canonical discovery matrix, and #3 the release gate.

## Authority and scope

Standalone is local, developer-operated execution under ADR-0008. It uses local account metadata, API credentials from a process environment, and bounded local Nurture run/target/operation state. It does **not** use the distributed PostgreSQL/Command/WorkerJob/scheduler authority. Default Nurture runs have API reads, no browser runtime and no mutation runtime; they still write local observation receipts/dedupe state. Explicit reply/content apply constructs the existing journaled mutation runtime only with appropriate reviewed inputs. No background automated mutation or recurrence is authorized.

**Live evidence as of 2026-10-10 (one controlled tester case):**
- Canonical Mentions returned a non-empty first page and a distinct second page with a functioning `after` cursor; terminal pagination, other discovery endpoints and permission mapping remain unverified.
- A recruitment Nurture observe returned `SUCCESS / OBSERVE_ONLY`; one selected fingerprint was independently mapped to an approved mention.
- One approved reply draft was applied once: `SUCCESS / REPLY_APPLIED`, journal `CREATE_REPLY / PUBLISHED`, target `CONFIRMED`; manual operator Threads UI inspection found one matching visible reply with the approved author/text.
- Operator locally verified two scrubbed evidence files outside Git; coordinator did not directly inspect file bytes.
- No proof of post-publish suppression after restart, broad idempotency, or independent browser-launch telemetry follows from this one case.

## Minimum setup — PowerShell 7

Run from the checked-out repository. Use a separate, operator-controlled local data root. The `THREADS_LOCAL_DATA_ROOT` value is **local process scope**; do not reuse an old root without reviewing its operations/target state. Never recreate an account merely because a new shell was opened.

~~~powershell
$env:THREADS_LOCAL_DATA_ROOT = Join-Path $env:LOCALAPPDATA 'ThreadsOperations\standalone'
uv run --locked threads-local account add ACCOUNT
uv run --locked threads-local account credential set-env ACCOUNT THREADS_PLATFORM_THREADS_TOKEN_OPERATOR
$token = Read-Host "Threads operator token" -MaskInput
if ([string]::IsNullOrWhiteSpace($token)) { throw "Missing token" }
[Environment]::SetEnvironmentVariable("THREADS_PLATFORM_THREADS_TOKEN_OPERATOR", $token, "Process")
Remove-Variable token
uv run --locked threads-local api quota ACCOUNT
~~~

Replace `ACCOUNT` with an authorized local alias. Run `account add` only once per selected data root; a repeat can be rejected, and a fresh root generates a new local account UUID. Only the name `env://THREADS_PLATFORM_THREADS_TOKEN_OPERATOR` is stored in account metadata; the actual secret must exist in the current process environment. Avoid PowerShell transcript capture, terminal output sharing, raw HTTP dumps and command-line token literals. `account list` and operation inspection may print identifiers; do not copy raw output into issues or logs.

Browser login is optional for the API-only observe/reply path, not a requirement for the successful live smoke. If a separately approved browser capability is needed, use the reviewed standalone `account login` command with human intervention; do not infer browser permission from API success.

## Observe and inspect

~~~powershell
uv run --locked threads-local nurture run ACCOUNT --preset recruitment
~~~

Expected summary format: `nurture run=... account=... preset=recruitment outcome=SUCCESS discovered=N selected=N decision=... [target=...]`.

- `SUCCESS` is the valid Nurture success outcome (not `SUCCEEDED`).
- `OBSERVE_ONLY`, `INBOUND_CANDIDATE` and `NO_ACTION` may all be legitimate decisions depending on the current candidate set. A selected target is **not** proof that it came from Mentions; verify its provenance and target fingerprint before considering an action.
- Default observe makes no remote publish call, but writes local run/target state and may affect cooldown; do not repeat it just to force a desired candidate.

When testing canonical Mentions separately, `threads-local api mentions ACCOUNT --limit 1` and one `--after` continuation are API reads. Capture CLI output in private process memory and report only counts, cursor presence and equality booleans. Do not persist or publish raw cursor, thread ID, post text, username or permalink.

## Explicit reply approval and one-shot apply

A real reply requires **all** of: an authorized/test post; verified target identity; an approved reply text; a valid `reply-file`; and operator authorization for exactly one public remote mutation. The accepted Nurture draft JSON has exactly four fields: `version` (1), `action` (`REPLY`), `target_fingerprint` (64 lowercase hexadecimal chars computed for that target), and `text` (non-empty, max 500 chars). Keep this private operator file outside the repo and do not put draft text or the fingerprint in Git issues/evidence.

The command interface for an approved one-shot reply is:

~~~text
uv run --locked threads-local nurture run ACCOUNT --preset recruitment --apply --reply-file PRIVATE_DRAFT_PATH
~~~

**Do not invoke this command merely to test a parser.** First recheck the exact account, target fingerprint, target `action_state=NONE`, draft text, quota, approval and one-shot guard. Publishing is an irreversible side effect. A live attempt that succeeds should link a `SUCCESS/REPLY_APPLIED` receipt to one `CREATE_REPLY/PUBLISHED` journal and mark the target `CONFIRMED`. Independently inspect the published reply on Threads UI before claiming end-to-end success.

If execution fails, is interrupted, returns an ambiguous result or lacks a clear success receipt: **STOP; do not retry**. Inspect the local Nurture receipt and operation journal through safe bounded summaries, and perform only approved read-only reconciliation. A failed request does not prove no remote publication occurred. Do not rewrite or retry historical ambiguous operations. Repeat apply is not a dedupe test.

Own-content `--post-file` and quote `--quote-file` use separate approval/provenance/contract checks; this 2026-10-10 session did not live-verify them. Recurrence is READ-only and cannot be used to automate a mutation-bearing plan.

## Evidence and status discipline

Report only safe summary fields: baseline, timestamp, outcome/decision codes, bounded counts, booleans, journal kind/phase and operator manual UI verification. Keep access/refresh tokens, app/client secrets, authorization headers, exact account/thread/reply/container/media IDs, usernames, permalinks, raw cursor, post/draft text, HTTP body and error body out of Git, chat, logs and evidence files. Local verified artifacts are not automatically repository-reviewed artifacts; label operator attestation honestly.

Scope the status vocabulary independently:
- `IMPLEMENTED` / `TESTED` describe code and deterministic tests.
- `LIVE-VERIFIED` covers only the specific real API/UI behavior actually observed.
- `DOCUMENTATION-CONTRACT` means no sufficient live behavior for that subpath.
- `BLOCKED` / `DEFERRED` preserve remaining gates and intentionally unimplemented options.

Do not close #229 or #215 until required synthetic/integration/static, cooldown/restart, own-content, security, timing and documentation gates are reviewed. #80 continues to own the B01–B13 discovery/cursor matrix; `polling_cursor_ready_evidence=false` until that matrix is satisfied. #3 independently blocks production/release activation.
