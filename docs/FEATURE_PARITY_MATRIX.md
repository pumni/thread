# Feature Parity Matrix

## Status vocabulary

- FULL: direct Threads-native equivalent can satisfy the legacy business outcome.
- ADAPTED: the business outcome exists but Threads uses a different concept.
- ENHANCED: official API enables a cleaner/stronger implementation than the legacy browser approach.
- VERIFY: product capability exists or is suspected, but official automation/API support must be verified before implementation.
- N/A: Facebook-specific behavior has no useful Threads equivalent.
- DEFERRED: intentionally outside the initial production scope.

## Matrix

| Legacy capability | Threads target | Status | Preferred implementation | Notes |
|---|---|---:|---|---|
| Multi-account management | Multiple Threads OAuth identities | ENHANCED | OAuth + DB | No cloned scripts or Chrome profiles |
| Realtime CRM commands | Typed command protocol | FULL | HTTP/WebSocket transport -> command bus | Durable inbox/idempotency |
| Post text | Publish text Thread | FULL | Official Threads API | P0 functional parity |
| Post image | Publish image Thread | FULL | Official Threads API | Use remote media URL requirements |
| Post video | Publish video Thread | FULL | Official Threads API | Verify processing/status behavior |
| Multi-image/video post | Carousel | FULL | Official Threads API | Container workflow |
| Share/re-share | Repost | VERIFY | Official Threads API | Endpoint is documented; outcome reconciliation is not, so mutation is deferred |
| Quote content | Quote post | ENHANCED | Official Threads API | Documented with `quote_post_id`; live permission remains part of TP-002 |
| Comment on post | Reply to Thread | FULL | Official Threads API | Domain term becomes reply |
| Reply to comment | Reply to reply | FULL | Official Threads API | Use reply_to_id |
| Nested reply hierarchy | Conversation tree | ENHANCED | Official Threads API + relational model | No DOM indentation parsing |
| Crawl comments | Sync replies/conversations | ENHANCED | Official Threads API | Cursor/incremental sync |
| CRM-triggered crawl | CRM-triggered conversation sync | FULL | Command runtime | Durable sync command |
| Periodic crawl | Scheduled incremental sync | FULL | Durable scheduler | No sleep loop |
| CSV export | Export synchronized data | FULL | Application query/export | Optional reporting feature |
| Store post/comment tree locally | Persist posts/replies | ENHANCED | PostgreSQL | No post_structure.json |
| Mongo/device account sync | Account registry/integration sync | ADAPTED | DB + CRM adapter | New data model |
| Proxy per browser account | Usually unnecessary for official API | N/A initially | Normal API networking | Revisit only for documented infra need |
| Cookie storage | OAuth token lifecycle | ENHANCED | Encrypted credential store | Never browser cookies as auth foundation |
| Login via browser profile | OAuth authorization | ENHANCED | Official OAuth | Explicit reauth state |
| Friend requests | Follow/social graph equivalent | VERIFY/ADAPTED | Official capability only | Do not assume Facebook friend semantics |
| Recruitment group search | Threads discovery/community workflows | ADAPTED | Search/discovery API where supported | Product/domain mapping required |
| Join Facebook group | Threads community membership equivalent | VERIFY | Official capability only | Capability gate |
| Group moderation questions | No direct assumed equivalent | VERIFY/N/A | None until verified | Do not create browser workaround by default |
| Messenger send message | Threads messaging automation | VERIFY | Official API only if exposed | Product feature does not imply public API support |
| Feed browsing | Content discovery/feed workflow | ADAPTED | Discovery APIs | No human-like scrolling |
| Random reaction automation | Engagement action | VERIFY/DEFERRED | Official API only | Not required for MVP |
| Watch video automation | Threads content consumption | N/A | None | Facebook Watch-specific |
| Human-like typing/scrolling | None | N/A | None | Explicitly excluded |
| Anti-detect browser flags | None | N/A | None | Explicitly excluded |
| Background process hiding | Service deployment | ENHANCED | Linux/Docker/service manager | Operational concern, not product feature |
| WebSocket reconnect | Reliable transport reconnect | FULL | Typed WS client | Does not own business state |
| Priority interruption | Command priority/scheduling | ENHANCED | Durable command scheduler | No global stop flag |
| API result callback | CRM result event | FULL | Outbox delivery | Retry-safe |
| Posting limit tracking | Threads publishing quota | ENHANCED | Official limit endpoint where available | No hardcoded limits |
| Comment/reply moderation | Threads reply management | ENHANCED | Official API | Hide/unhide/pending approval documented; account permissions remain VERIFY |
| Search public discussions | Threads discovery | ENHANCED | Official API where supported | Keyword/tag capabilities must be checked |
| Performance tracking | Threads insights | NEW/ENHANCED | Official Insights API | Snapshot time series |

## Rules for Codex

1. A FULL/ENHANCED row still requires current official API verification before implementation.
2. A VERIFY row cannot be implemented via browser automation without an ADR approved in review.
3. If Meta removes or changes a capability, update this matrix before changing architecture.
4. Product UI existence is not evidence of API availability.
5. Every parity implementation must have a corresponding acceptance test or documented manual integration test.
