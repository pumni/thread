# Worker browser capability pack v1

## Activation status

The C5-01 contract catalog declares four capability names at version 1. All four
are `BLOCKED_UI_EVIDENCE` and are unavailable to production routing. No reviewed,
scrubbed observation of the corresponding Threads UI surfaces was present in the
repository on 2026-09-26. C3's `worker.synthetic` contract is a local test fixture
and is not evidence of the production Threads interface.

Until the UI evidence gate is reviewed, the Capability Router returns
`UNSUPPORTED / BROWSER_UI_EVIDENCE_REQUIRED`, WorkerJob enqueue is skipped, and
`worker_execution_allowed()` rejects direct claims for these names. Worker hello
rejects an advertisement of a blocked name with
`WORKER_ADVERTISED_BLOCKED_CAPABILITY`, and WorkerJobService rejects direct enqueue
or claim. Account modes cannot select an API fallback.
This keeps a blocked capability visible without creating stranded jobs or
pretending that synthetic coverage verifies production selectors.

The `ui_contract_id` values below are reserved application contract names. Their
version 1 is a planned semantic contract version, not an observed UI version and
does not authorize selectors.

## Declared contracts

| Capability | Class / operation | Session | Safe checkpoints | Bounded outcome | Preemptible | Irreversible boundary | Status |
|---|---|---|---|---|---:|---:|---|
| `threads.browser.feed.browse` v1 | BROWSER_ASSISTED / READ | AUTHENTICATED | BEFORE_NAVIGATION, FEED_READY, ITEM_BATCH | `BrowserFeedResultV1`; up to 20 observations, 5 scroll iterations, 30 seconds | yes | no | BLOCKED_UI_EVIDENCE |
| `threads.browser.thread.open` v1 | BROWSER_ASSISTED / READ | AUTHENTICATED | BEFORE_NAVIGATION, THREAD_READY | `BrowserTargetOpenResultV1`; one opaque Thread reference | yes | no | BLOCKED_UI_EVIDENCE |
| `threads.browser.profile.open` v1 | BROWSER_ASSISTED / READ | AUTHENTICATED | BEFORE_NAVIGATION, PROFILE_READY | `BrowserTargetOpenResultV1`; one opaque profile reference | yes | no | BLOCKED_UI_EVIDENCE |
| `threads.browser.media.local_upload` v1 | BROWSER_ASSISTED / MUTATION | AUTHENTICATED | BEFORE_LOCAL_STAGE, LOCAL_STAGE_COMPLETE | `BrowserMediaStageResultV1`; image/video kind, bounded byte size, staged flag | yes | no external boundary | BLOCKED_UI_EVIDENCE |

The local upload contract permits file selection/staging only. It does not permit
Publish, Submit, or another irreversible Threads action. Browser-side publishing
requires a separate reviewed capability and recovery contract.

## Command and result boundary

The four versioned command envelopes use strict, bounded payloads:

- feed browse accepts only `max_items` from 1 through 20;
- Thread and profile open accept an opaque identifier, not a URL or arbitrary path;
- local upload accepts a simple worker-local `media_ref`, never an absolute path;
- unknown payload fields are rejected.

The normalized result models are independent of Playwright and DOM types. Feed
observations contain only an optional Thread reference, optional author username,
a text excerpt capped at 500 characters, and an observation position. Target open
returns the target kind/reference and whether its expected contract was recognized.
Local media staging returns media kind, byte size, and staged status only. Schemas
reject extra fields such as HTML, screenshots, browser storage, or filesystem paths.

## Local media source policy

The Worker Agent prepares a `media` directory under its managed local data root.
`LocalMediaFileResolver` accepts a single logical filename and rejects traversal,
drive/UNC/device names, symlinks resolving outside the managed root, non-regular or
empty files, unsupported extensions, and files larger than 50 MiB. The initial
allowlist is `.jpg`, `.jpeg`, `.png`, `.webp`, `.mp4`, and `.mov`. The source is not
deleted. The resolver returns a worker-local path to infrastructure only; neither
that path nor file bytes are part of Control Plane payloads or the recovery journal.

This validates local source selection policy only. It does not select a file in a
Threads composer. That UI step remains blocked until the composer surface has
reviewed production evidence.

## Session and recovery contract

All capabilities require the existing C3 managed account session to be
`AUTHENTICATED`. The declared session interventions are `LOGIN_REQUIRED`,
`SESSION_EXPIRED`, and `CHALLENGE_REQUIRED`; local upload also permits
`REMOTE_STATE_UNCERTAIN`. Existing C3 session reporting and WorkerJob fencing are
the required implementation paths. The capability contracts add no alternate
session or job journal.

Declared bounded failure codes are `BROWSER_CONTRACT_MISMATCH`,
`BROWSER_REQUIRED_MARKER_NOT_FOUND`, `UNSUPPORTED_UI_STATE`,
`BROWSER_NAVIGATION_TIMEOUT`, `BROWSER_PROCESS_CRASHED`, and
`WORKER_JOB_LEASE_LOST`. Local media staging also declares
`MEDIA_FILE_REJECTED` and `MEDIA_UPLOAD_FAILED`.

The read checkpoint plan stops before more navigation/collection after lease loss.
Browser timeout or crash does not prove that a target is absent. The local staging
plan checkpoints before and after file selection and must stop on uncertain browser
state rather than selecting repeatedly. These execution behaviors are not enabled
while the corresponding production UI contract is blocked.

## Evidence gate to unblock a capability

For each surface, collect and review a separate scrubbed record of observation date,
surface, semantic markers, expected session-state classification, contract version,
and reviewer note. Do not commit tokens, cookies, account identifiers, private
content, full DOM, or sensitive screenshots. A synthetic fixture may test parsing,
bounds, typed errors, and lease fencing after an adapter exists; it cannot clear
this production evidence gate.

No Threads selectors or browser workflow implementations were added in C5-01
because the reviewed evidence gate is not satisfied. LIKE/FOLLOW, browser
reply/publish, scheduler, AccountActivityPlan, and durable priority preemption are
outside this checkpoint.
