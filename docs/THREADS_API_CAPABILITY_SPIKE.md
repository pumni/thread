# TP-002 Threads API Capability Spike

**Status:** Documentation review is sufficient for implementation planning; **live TP-002 validation remains open** because no dedicated local development app/account has been used. Issue #3 is a production/release gate.

Issue #65 prepares only an offline, secret-safe evidence packet validator and runbook. No live API validation happened in that checkpoint; it does not change any capability from documentation-contract to live-verified. Use [the live validation runbook](THREADS_LIVE_VALIDATION_RUNBOOK.md) and [the packet template](examples/threads-live-evidence-v1.template.json) for a later human-run #3 session.

**Evidence reviewed:** 2026-09-25. Primary evidence is Meta's official Threads API Postman workspace/collection. Meta explicitly states the collection may lag the current developer changelog, so implementation must re-check current developer documentation before changing contracts.

No live account responses are represented by repository documentation-contract fixtures.

## Documented contract baseline

| Capability | Current official workspace evidence | Live status |
|---|---|---|
| OAuth code exchange | OAuth access-token exchange documented | OPEN |
| Long-lived token / refresh | exchange + refresh documented; threads_basic noted as sufficient for exchange/refresh | OPEN |
| Own profile | /me profile fields documented | OPEN |
| Public profile lookup | /profile_lookup?username=... documented | OPEN |
| Public profile posts | /profile_posts?username=... documented | OPEN |
| Text/image/video/carousel publish | /me/threads -> /me/threads_publish documented | OPEN |
| Quote post | quote_post_id documented | OPEN permission/behavior |
| Repost | repost endpoint documented | VERIFY lost-response reconciliation |
| Container/media status | container states documented; PUBLISHED status response does not establish lost publish-response media ID | OPEN |
| Replies | /{thread_id}/replies documented | OPEN live behavior |
| Flattened conversation | /{thread_id}/conversation documented as paginated flattened top-level+nested replies | OPEN cursor semantics |
| Reply creation | reply_to_id documented | OPEN |
| Reply moderation | manage_reply/manage_pending_reply documented | OPEN account permissions |
| Keyword search | /keyword_search; TOP/RECENT; KEYWORD/TAG documented | OPEN |
| Mentions | /me/mentions with time/cursor pagination documented | OPEN |
| Publishing quota | /me/threads_publishing_limit documented | OPEN actual account values |
| Permissions | threads_basic, threads_content_publish, threads_read_replies, threads_manage_replies, threads_manage_insights listed in official workspace | OPEN effective per-account grants |
| Error payloads/headers | examples are not sufficient for full runtime error contract | OPEN |

### SN-08 post-insights documentation contract (2026-10-07)

The current official Meta [post insights reference](https://developers.facebook.com/documentation/threads/reference/insights)
and [Threads insights guide](https://developers.facebook.com/documentation/threads/insights)
were rechecked on 2026-10-07. The documented read is
`GET /{thread_id}/insights?metric=...`. A response is an object whose `data`
array contains metric rows with `name`, `period`, and `values`; each value row
contains `value`. The guide example uses `period: "lifetime"` and an integer
value. Optional row metadata such as `title`, `description`, and `id` is ignored.

The Standalone SN-08 adapter requests only the ordered subset
`likes,replies,reposts,quotes`, accepts `lifetime`, maps absent metrics and
null values to `None`, and rejects malformed/duplicate/unsupported rows. This
is a documentation-contract implementation tested with synthetic fixtures;
no live API request was made and Insights remains OPEN / not LIVE-VERIFIED.

The local refresh policy is fixed and bounded: at most five reads per
invocation, with six hours between snapshots for one content item. Items with
no snapshot are considered first, then the oldest latest observation, with the
content fingerprint as a stable tie-break. Content identity is a versioned
SHA-256 over the SN-07 source and draft fingerprints; media IDs are resolved
ephemerally through the local POST_TEXT/PUBLISHED operation journal and are not
copied into Insights snapshots. A failed request stops the refresh while prior
immutable snapshots remain.

The refresh counters mean: `refreshed` is the number of successful read/append
pairs; `skipped_spacing` counts eligible items whose latest observation is
younger than six hours or is future-dated; `scoreable` counts returned items
with all four metrics present; and the remaining fields count returned items
in each performance bucket. Bucket counts use all timestamp-valid published
items as targets, compared with the scoreable subset of the latest-20 baseline.

Feedback sums likes, replies, reposts, and quotes only when all four values are
present. Its reference set is the latest 20 eligible PUBLISHED items for the
same local account and preset; fewer than ten complete snapshots yields
INSUFFICIENT_DATA. Midpoint percentiles place equal-valued scores at their
midrank, so an all-equal set is BASELINE. Buckets are below 40, 40 to under 60,
60 to under 80, and 80 or above. This feedback is read-only and does not change
posting cadence, provenance checks, or mutation selection.

### C4 documentation-contract implementation

C4 now has API adapter and persistence paths for keyword/tag search, public profile
lookup/posts, mentions, and conversation enrichment. Synthetic fixtures exercise
the documented request parameters and conservative response mapping; they are not
live account evidence, and the table above remains OPEN for production validation.

The discovery adapter currently reads `paging.cursors.after` as the continuation
cursor. A present cursor is treated as another page; cursor repetition is rejected
and each committed cursor is recorded per run. This interpretation and the use of
the `after` request parameter for profile posts and mentions still require live
validation. The public profile lookup request is documented, but the reviewed
workspace has no response example; the implementation therefore treats `id` and
`username` as required and `name`, `threads_biography`, and
`threads_profile_picture_url` as optional fields based on the documented profile
shape. Profile-post and discovery response field combinations also require live
verification. No implementation state here sets `live_verified=true`.

## Implementation vs production gate

Documentation-backed implementation is allowed when behavior can be modeled conservatively.

Requirements:
- label mock schemas as documentation-contract fixtures;
- keep uncertain fields optional/conservative;
- preserve VERIFY states;
- do not claim production/live validation;
- never fabricate account responses;
- do not blind-retry ambiguous remote mutations.

Browser execution is governed separately by ADR-0003/0005 and the distributed Worker roadmap. Browser availability does not make an unverified API assumption true.

## Live TP-002 completion requirements

With a dedicated development app/account, collect only scrubbed evidence for:
- OAuth/code exchange;
- long-lived exchange and refresh;
- effective scopes/token debugger;
- own profile;
- text/image/video publishing and retrieval;
- reply/reply-to-reply;
- replies/conversation;
- quota;
- keyword/tag search;
- public profile lookup/profile posts;
- mentions;
- media/container processing;
- moderation permissions;
- representative 4xx/429/5xx response/status/header behavior;
- reconciliation possibilities after ambiguous publish;
- whether pagination cursors are appropriate for durable cross-run polling.

Never record access tokens, app secrets, authorization codes or sensitive account identifiers.

## #65 evidence tooling checkpoint

The evidence model uses `threads-live-evidence-v1`, strict unknown-field rejection, recursive secret-sensitive scanning, and explicit `TEMPLATE_ONLY_NOT_LIVE_EVIDENCE` classification for its checked-in matrix. Its fingerprint helper accepts opaque input through standard input and outputs only `sha256:<64 lowercase hex characters>`; fingerprints are comparison metadata, never authentication material. Validation is offline and does not call Meta.

Issue #68's accepted scrubbed Phase A evidence is recorded in
`docs/evidence/threads-live-phase-a-2026-09-30.json` and established token
provider readiness (`token_provider_ready_evidence=true`). Issue #74 records
partial Phase B live evidence in
`docs/evidence/threads-live-phase-b-partial-2026-09-30.json`
(`PARTIAL_LIVE_EVIDENCE / NOT_READY`). Canonical discovery endpoints failed or
were blocked on the test app with HTTP 500, while B05/B06 were verified and
collection pagination semantics were corroborated on `/me/threads`.
`polling_cursor_ready_evidence=false`, so discovery/mentions scheduler policy
under #9 remains blocked. Opportunistic Phase C observations (C01, C04, C08)
were recorded but do not equal Phase C acceptance. No discovery/mentions
capability becomes live-verified. Issue #3 remains OPEN as a production/release
gate, and the Postman collection alone is not runtime proof.

## Official source links

- https://developers.facebook.com/docs/threads/changelog
- https://developers.facebook.com/documentation/threads/keyword-search
- https://www.postman.com/meta/threads/overview
- https://www.postman.com/meta/threads/collection/dht3nzz/threads-api
- https://www.postman.com/meta/threads/folder/34203612-e0373e84-de6b-46f1-b90d-3fea76ba6782
- https://www.postman.com/meta/threads/request/34203612-b3b2c12a-7ce6-4d86-a3c6-6d31e3b66ea1
- https://www.postman.com/meta/threads/request/34203612-fc3f21da-0a53-44ab-80e2-8cd8c376a42a
- https://www.postman.com/meta/threads/request/34203612-f4590341-9bee-44f5-901b-606078a03c96
- https://www.postman.com/meta/threads/documentation/dht3nzz/threads-api
