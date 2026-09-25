# TP-002 Threads API Capability Spike

**Status:** Documentation review is sufficient for implementation planning; **live TP-002 validation remains open** because no dedicated local development app/account has been used. Issue #3 is a production/release gate.

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

## Official source links

- https://www.postman.com/meta/threads/overview
- https://www.postman.com/meta/threads/collection/dht3nzz/threads-api
- https://www.postman.com/meta/threads/folder/34203612-e0373e84-de6b-46f1-b90d-3fea76ba6782
- https://www.postman.com/meta/threads/request/34203612-b3b2c12a-7ce6-4d86-a3c6-6d31e3b66ea1
- https://www.postman.com/meta/threads/request/34203612-fc3f21da-0a53-44ab-80e2-8cd8c376a42a
- https://www.postman.com/meta/threads/request/34203612-f4590341-9bee-44f5-901b-606078a03c96
- https://www.postman.com/meta/threads/documentation/dht3nzz/threads-api