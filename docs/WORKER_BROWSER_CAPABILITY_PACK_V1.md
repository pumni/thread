# Worker browser capability pack v1

## Activation status

The C5-01 catalog contains four v1 names. PR #39's
`threads.browser.feed.browse` v1 and PR #40's `threads.browser.thread.open` v1
are accepted and merged, and DONE for their checkpoints. Both are available
only through their reviewed, bounded, account-affine WorkerJob paths and
require explicit worker opt-in via
`THREADS_WORKER_FEED_BROWSE_ENABLED` and `THREADS_WORKER_THREAD_OPEN_ENABLED`
(both default false). PR #41's `threads.browser.profile.open` v1 is accepted
and merged, DONE for its checkpoint, and available only through the same
account-affine WorkerJob path with explicit opt-in via
`THREADS_WORKER_PROFILE_OPEN_ENABLED` (default false). PR #43's
`threads.browser.media.local_upload` v1 is accepted and merged, DONE for its
checkpoint, and available only through the reviewed bounded account-affine
WorkerJob path with `THREADS_WORKER_MEDIA_LOCAL_UPLOAD_ENABLED` (default
false). C3's `worker.synthetic` contract remains a local fixture, not
production UI evidence.

The `ui_contract_id` values below are application contract names. The feed and
thread-open, profile-open, and image-only local-upload version 1 contracts are
mapped to their accepted semantic evidence; they are not versions reported by
Threads.

## Declared contracts

| Capability | Class / operation | Session | Safe checkpoints | Bounded outcome | Preemptible | Irreversible boundary | Status |
|---|---|---|---|---|---:|---:|---|
| `threads.browser.feed.browse` v1 | BROWSER_ASSISTED / READ | AUTHENTICATED | BEFORE_NAVIGATION, FEED_READY, ITEM_BATCH | `BrowserFeedResultV1`; up to 20 observations, 5 feed iterations, 30 seconds | yes | no | AVAILABLE |
| `threads.browser.thread.open` v1 | BROWSER_ASSISTED / READ | AUTHENTICATED | BEFORE_NAVIGATION, THREAD_READY | `BrowserTargetOpenResultV1`; one normalized relative Thread permalink, 30 seconds | yes | no | AVAILABLE |
| `threads.browser.profile.open` v1 | BROWSER_ASSISTED / READ | AUTHENTICATED | BEFORE_NAVIGATION, BEFORE_PROFILE_INSPECTION, PROFILE_READY | `BrowserTargetOpenResultV1`; one normalized relative `/@<username>` path, 30 seconds | yes | no | AVAILABLE |
| `threads.browser.media.local_upload` v1 | BROWSER_ASSISTED / MUTATION | AUTHENTICATED | BEFORE_LOCAL_STAGE, LOCAL_STAGE_COMPLETE | `BrowserMediaStageResultV1`; image kind, bounded byte size, staged flag | no | yes; file selection begins the irreversible upload | AVAILABLE |

The local upload contract permits file selection/staging only. It does not permit
Publish, Submit, or another irreversible Threads action. Browser-side publishing
requires a separate reviewed capability and recovery contract.

## Local media upload v1 reviewed contract

The Control Plane accepts only a logical `media_ref`; the assigned Worker
resolves it beneath its managed media directory. V1 accepts jpg, jpeg, png, and
webp image files only. Video and all other media fail closed. The command is a
`MUTATION` with `preemptible=false`, an irreversible boundary around file
selection, and `RECONCILIATION_REQUIRED` retry safety. A Worker opt-in flag is
required and defaults to false.

The operator must open the composer before the Worker proceeds. If no composer
is open, the Worker requests `OPERATOR_CONFIRMATION_REQUIRED` with
`COMPOSER_OPEN_REQUIRED` before the mutation boundary and does not click a
Create control. After the operator requeues the job, the Worker proves there is
exactly one dialog, one textbox, and one page-wide file input associated with
that dialog before continuing.

The network observer is armed before file selection. Success requires one
correlated POST to the approved current Threads origin, a pathname matching
`^/rupload_igphoto/fb_uploader_[0-9]+$`, HTTP 200, and then a
`img[src^="blob:"]` preview still present in the same composer. A preview
before the response is not success. Timeout, crash, lease loss, network
ambiguity, non-200, missing completion, or a mismatched preview after selection
produces `AMBIGUOUS_OUTCOME`; the Worker never selects the file a second time.
It does not click Publish, Post, Submit, or Remove. The result contains only
image kind, byte size, and staged status.

## Command and result boundary

The four versioned command envelopes use strict, bounded payloads:

- feed browse accepts only `max_items` from 1 through 20;
- Thread open accepts a relative `/@<username>/post/<id>` path only; one trailing slash is normalized;
- profile open accepts a relative `/@<username>` path only; one trailing slash is normalized;
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

## Thread open v1 reviewed contract

Input is one bounded relative `/@<username>/post/<id>` path. The Worker Agent
navigates only to the approved Threads Web origin plus that path. The current
`window.location.pathname` must equal the normalized input exactly; normalization
removes at most one trailing slash. A path prefix or extended path is not a
match.

Recognition uses exact relative href paths. The target permalink href must
equal the input after the same trailing-slash normalization; the author profile
href must equal `/@<username>` for the exact target author. The same bounded
ancestor must also contain non-empty text from the observed `span[dir="auto"]`
marker. Ancestor traversal is limited to eight levels. Multiple exact target
anchors are allowed only if every anchor resolves to the same root association.
Competing roots, a reply permalink/author in the candidate association, missing
author or text evidence, and evidence beyond the bound fail closed.

Redirects, page-initiated off-origin navigation, an exact pathname mismatch, or
a loaded target without the reviewed target href create durable
`REMOTE_STATE_UNCERTAIN` intervention. Known persisted login, expired-session,
and challenge states use their existing durable interventions. No new login or
challenge selector is inferred. Canonical metadata, `main`, `article`, generated
CSS classes, and localized Back controls are not recognition inputs. A
successful result contains only version, `THREAD`, the normalized relative
target path, and `recognized: true`.

## Local media source policy

The Worker Agent prepares a `media` directory under its managed local data root.
`LocalMediaFileResolver` accepts a single logical filename and rejects traversal,
drive/UNC/device names, symlinks resolving outside the managed root, non-regular or
empty files, unsupported extensions, and files larger than 50 MiB. The initial
allowlist is `.jpg`, `.jpeg`, `.png`, `.webp`, `.mp4`, and `.mov`. The source is not
deleted. The resolver returns a worker-local path to infrastructure only; neither
that path nor file bytes are part of Control Plane payloads or the recovery journal.

This validates local source selection policy only. The media.local_upload v1
Worker accepts only jpg/jpeg/png/webp images; video and other formats fail
closed before file selection.

## Session and recovery contract

All capabilities require the existing C3 managed account session to be
`AUTHENTICATED`. The declared session interventions are `LOGIN_REQUIRED`,
`SESSION_EXPIRED`, and `CHALLENGE_REQUIRED`. Feed browse, thread open, profile
open, and local upload permit `REMOTE_STATE_UNCERTAIN`. If an authenticated feed navigation receives a
redirect, it is blocked before following and requests durable
`REMOTE_STATE_UNCERTAIN` intervention. A bounded read with no reviewed permalink
candidates does the same, including when the feed may simply be exhausted; the
available production evidence cannot distinguish that from a remote session
transition. This path does not guess a login or challenge selector. Malformed,
ambiguous, or over-bound feed association evidence remains a fail-closed
contract failure. Thread open applies its target path, anchor, author, and text
checks under the same durable WorkerJob intervention and lease fencing. Profile
open applies exact-path and bounded-header recognition under the same durable
WorkerJob intervention and lease fencing. Local media staging uses the durable
WorkerJob irreversible boundary and lease fencing; once file selection may
have started, an uncertain result is reconciled rather than retried. Existing
C3 session reporting and WorkerJob fencing are the required implementation
paths; these contracts add no alternate session or job journal.

Declared bounded failure codes are `BROWSER_CONTRACT_MISMATCH`,
`BROWSER_REQUIRED_MARKER_NOT_FOUND`, `UNSUPPORTED_UI_STATE`,
`BROWSER_NAVIGATION_TIMEOUT`, `BROWSER_PROCESS_CRASHED`, and
`WORKER_JOB_LEASE_LOST`. Feed browsing, thread open, and profile open also allow
`BROWSER_SESSION_UNAVAILABLE`, `BROWSER_NETWORK_ROUTE_UNSUPPORTED`,
`UNSUPPORTED_BROWSER_CAPABILITY`, `WORKER_JOB_INPUT_INVALID`, and
`WORKER_JOB_RETRY_SAFETY_MISMATCH`. Local media staging additionally declares
`MEDIA_FILE_UNAVAILABLE`, `MEDIA_FILE_REJECTED`, and `MEDIA_FILE_TOO_LARGE`.

The feed workflow renews the WorkerJob lease before navigation, feed collection,
and scrolling. Thread and profile open renew before navigation and target
inspection. These workflows checkpoint only bounded phase codes, and lease loss
stops all subsequent browser work. Browser timeout or crash does not prove that
a target is absent. Local media renews the lease before file selection and
through upload completion; lease loss stops later browser work and yields an
ambiguous outcome after the irreversible boundary.

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

## Accepted profile evidence record

- **Observation date:** 2026-09-28.
- **Surface:** authenticated Threads Web public profiles; two independent
  public-profile samples.
- **Observed path:** `/@<username>` with exact normalized pathname equality to
  the requested target.
- **Observed identity markers:** exactly one non-empty page `<h1>`; the nearest
  bounded `<div>` ancestor containing the heading also contains one or more
  exact target-profile hrefs and no semantic post permalink.
- **Repeatability:** exact target-profile anchors were repeated 8–9 times within
  the observed header; uniqueness is not required. Post cards below the header
  contained `/post/` permalinks and did not contain the page `<h1>`.
- **Reviewer note:** `<main>`, `<article>`, canonical metadata, generated CSS
  classes, and localized labels are not accepted recognition anchors.

The approved workflow requires exact normalized pathname equality and exactly
one non-empty `<h1>`, then inspects only its nearest `<div>` ancestor within
eight levels. That association must contain at least one exact target-profile
href and no semantic post permalink. Duplicate exact target-profile hrefs
inside that `<div>` are allowed. If no exact target href exists anywhere after
the target loads, the worker requests durable `REMOTE_STATE_UNCERTAIN`. Exact
target hrefs only outside the `<div>` association, missing/duplicate headings,
post contamination, ambiguity, and evidence beyond the bound fail closed.
Generic profile links or `[dir="auto"]` outside the proven header association
do not establish identity. A successful result contains only `PROFILE`, the
normalized relative target, and `recognized: true`.

The record contains no username, profile identifier, private content, cookies,
tokens, full DOM, or screenshot. Synthetic fixtures exercise this accepted
contract but are not production evidence.

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

Synthetic fixtures exercise the accepted semantic contracts and their failure
cases; they are not production evidence that the Threads UI matches those
contracts.

PR #41's `threads.browser.profile.open` v1 and PR #43's
`threads.browser.media.local_upload` v1 have been accepted and merged.
`threads.browser.feed.browse`, `threads.browser.thread.open`, and
`threads.browser.profile.open` are DONE for their checkpoints and available
only through the reviewed bounded account-affine WorkerJob path with explicit
worker opt-in. `threads.browser.media.local_upload` is also DONE for its
checkpoint and available only through the same bounded account-affine
WorkerJob path with explicit worker opt-in. It is image-only and stages into an
operator-opened composer after proving dialog/input association and a correlated
successful upload response plus same-composer preview. It never publishes,
submits, or removes staged media. Synthetic fixtures are not production
evidence. LIKE/FOLLOW remain VERIFY. Browser Reply/Repost/Share/Create/Post,
publish/submit, scheduler, AccountActivityPlan, and durable priority preemption
remain outside this scope. Issue #27 remains OPEN pending final coordinator
closure. #28 remains unauthorized; no additional mutation or publish/submit
scope is authorized.
