# Worker browser capability pack v1

## Activation status

The C5-01 catalog contains four v1 names. On 2026-09-28, the coordinator
accepted scrubbed production UI evidence for `threads.browser.feed.browse` and
authorized implementation of that capability only. This checkpoint implements
its bounded permalink-pivot workflow and routes it through an account-affine
WorkerJob. The other three capabilities remain `BLOCKED_UI_EVIDENCE`, unavailable
to routing and worker advertisement. C3's `worker.synthetic` contract remains a
local fixture, not production UI evidence.

The `ui_contract_id` values below are application contract names. The feed's
version 1 is mapped to the accepted semantic evidence below; it is not a version
reported by Threads. The other three contract names remain planned and blocked.

## Declared contracts

| Capability | Class / operation | Session | Safe checkpoints | Bounded outcome | Preemptible | Irreversible boundary | Status |
|---|---|---|---|---|---:|---:|---|
| `threads.browser.feed.browse` v1 | BROWSER_ASSISTED / READ | AUTHENTICATED | BEFORE_NAVIGATION, FEED_READY, ITEM_BATCH | `BrowserFeedResultV1`; up to 20 observations, 5 feed iterations, 30 seconds | yes | no | AVAILABLE |
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
a text excerpt capped at 500 characters, and an observation position. A successful
target-open result contains a recognized target kind/reference with
`recognized: true`; an unknown or mismatched UI produces a typed failure rather
than a successful result. Local media staging returns media kind, byte size, and
staged status only. Schemas reject extra fields such as HTML, screenshots, browser
storage, or filesystem paths.

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
`WORKER_JOB_LEASE_LOST`. Feed browsing also allows
`BROWSER_SESSION_UNAVAILABLE`, `BROWSER_NETWORK_ROUTE_UNSUPPORTED`,
`UNSUPPORTED_BROWSER_CAPABILITY`, `WORKER_JOB_INPUT_INVALID`, and
`WORKER_JOB_RETRY_SAFETY_MISMATCH`. Local media staging also declares
`MEDIA_FILE_REJECTED` and `MEDIA_UPLOAD_FAILED`.

The feed workflow renews the WorkerJob lease before navigation, feed collection,
and scrolling, and checkpoints only bounded phase codes. Lease loss stops all
subsequent browser work. Browser timeout or crash does not prove that a target is
absent. The other capabilities have no executable workflow in this checkpoint.

## Accepted feed evidence record

- **Observation date:** 2026-09-28.
- **Surface:** authenticated Threads Web feed; three consecutive feed items were
  sampled.
- **Observed semantic markers:** author profile href `/@<username>`; Thread
  permalink href `/@<username>/post/<id>`; post text in `span[dir="auto"]` or
  `div[dir="auto"]`.
- **Session-state classification:** `AUTHENTICATED`.
- **Proposed contract mapping:** `threads.browser.feed` v1, permalink-pivot
  association.
- **Reviewer note:** all three markers appeared for each sampled item. No common
  stable semantic/data ancestor exists; `data-pressable-container` was not
  stable enough. The coordinator accepted a nearest-ancestor strategy with a
  strict finite bound and fail-closed ambiguity checks.

This record contains no username, post ID, private text, cookies, tokens, full
DOM, or screenshot. The accepted evidence unblocks feed.browse only.

## Feed permalink-pivot workflow

The worker navigates only to the canonical Threads Web origin
`https://www.threads.com/`; Meta announced the move from Threads.net to
[Threads.com](https://about.fb.com/news/2025/04/new-features-threads-web-experience/).
For every candidate permalink matching the observed path, it walks at most eight
ancestors from the anchor's parent and selects the nearest ancestor that contains
exactly one matching permalink, exactly one compatible author profile href, and
at least one bounded `[dir="auto"]` text region. It rejects a repeated pivot,
another candidate post permalink, multiple compatible author links, missing
markers, overflow, or an association not proven within the bound. It does not
anchor on static container attributes, generated classes, absolute DOM indexes,
`<main>`, tab roles, or action-button roles.

The worker normalizes only the canonical Thread reference, visible author
username, excerpt capped at 500 characters, and observation position. It
deduplicates by normalized Thread reference within the job. Each feed iteration
scans at most 100 candidate permalink anchors; the page scan fails closed above
1,000 anchor nodes. Five total feed iterations include the initial read and at
most four fixed viewport scrolls. The WorkerJob input stores only the validated
`max_items` bound. Top-level requests are fetched with redirects disabled and
only an allowlisted response is fulfilled into the browser. A persistent guard
rejects later top-level navigation attempts; service workers are blocked so
they cannot bypass it. No Like, Reply, Repost, Share, Create, Publish, or Submit
action is invoked.

Migration `20260928_0010` adds durable WorkerJob input data. Its downgrade refuses
to drop the column while any job contains nonempty input data, preserving queued
and historical feed bounds; the field must be cleared or the downgrade must wait.

The session must already be `AUTHENTICATED` in the C3 session-state manager.
`LOGIN_REQUIRED`, `SESSION_EXPIRED`, and `CHALLENGE_REQUIRED` stop the job and
request the existing intervention; unknown feed structure fails closed. The
workflow has a 30-second wall-clock limit and does not checkpoint feed text or
raw page data.

Synthetic fixtures exercise the accepted semantic contract and its failure
cases. They are not production evidence for the other three capabilities.

`threads.browser.thread.open`, `threads.browser.profile.open`, and
`threads.browser.media.local_upload` remain blocked. LIKE/FOLLOW, browser
Reply/Repost/Share/Create/Post, publish/submit, scheduler, AccountActivityPlan,
and durable priority preemption are outside this checkpoint. Issue #27 remains
open for coordinator review; this implementation does not authorize another
capability.
