# Feature Capability Matrix v2

## 1. Purpose

This matrix maps useful business outcomes from the legacy Facebook tool to the new distributed Threads tool.

It does **not** require literal UI parity.

The legacy report is treated only as a requirements inventory. The new project uses Threads-native concepts, official API capabilities, Browser Worker capabilities where justified, and human intervention where necessary.

## 2. Execution classes

- NATIVE_API — official Threads API is the preferred executor.
- HYBRID — API is preferred for some paths, Browser/Human may be valid fallback or complementary execution.
- BROWSER_ASSISTED — capability depends primarily on an authenticated browser/session.
- HUMAN_ASSISTED — tool coordinates/prepares work but operator action is required.
- UNSUPPORTED — intentionally not implemented unless a later product decision changes.
- N/A — Facebook-specific primitive with no useful direct Threads parity.

## 3. Delivery status

- DONE — implemented in current codebase.
- PLANNED — approved future scope.
- VERIFY — capability/permission/recovery semantics require verification before activation.
- DEFERRED — intentionally postponed.
- DROP — explicitly excluded as a project objective.

## 4. Matrix

| Legacy/business outcome | Threads target | Execution class | Preferred executor | Fallback | Status | Priority | Notes |
|---|---|---|---|---|---|---:|---|
| Multi-machine tool | Distributed Worker Fleet | HYBRID | Control Plane + WorkerJob | none | PLANNED | P0 | C1 |
| Per-machine account ownership | Persistent account-worker affinity | BROWSER_ASSISTED | assigned Worker | API when policy allows | PLANNED | P0 | no auto profile migration |
| Multiple account modes | API_ONLY/BROWSER_ONLY/HYBRID/MANUAL | HYBRID | Capability Router | explicit policy | PLANNED | P0 | C2 |
| CRM realtime command | Typed durable Command | NATIVE_API | Control Plane | none | DONE | P0 | command_id idempotency |
| WebSocket reconnect | Realtime worker/CRM notification | HYBRID | WSS notification | DB/HTTPS reconcile | DONE/PLANNED | P0 | socket never source of truth |
| Priority interruption | Durable priority/preemption | HYBRID | Scheduler/WorkerJob | cancellation at safe boundary | PLANNED | P1 | replaces stop_browsing |
| Text publish | Threads text post | HYBRID | Official API | Browser | DONE/API | P0 | Browser fallback C3/C5 |
| Image publish by URL | Threads image post | HYBRID | Official API | Browser | DONE/API | P0 | API requires media contract |
| Local image/file publish | Local browser upload | BROWSER_ASSISTED | Browser Worker | media service -> API later | PLANNED | P1 | no CDN requirement for browser path |
| Video publish | Threads video post | HYBRID | Official API | Browser | DONE/API | P0 | live media behavior still TP-002 gate |
| Multi-media post | Carousel | HYBRID | Official API | Browser | DONE/API | P0 | container workflow |
| Quote content | Quote Thread | HYBRID | Official API | Browser | DONE/API | P1 | live permission still VERIFY |
| Re-share | Repost | HYBRID | Official API | Browser/Human | VERIFY | P2 | lost-response reconciliation unresolved |
| Comment | Reply | HYBRID | Official API | Browser | DONE/API | P0 | Threads-native terminology |
| Reply to comment | Reply-to-reply | HYBRID | Official API | Browser | DONE/API | P0 | relational parent mapping |
| Crawl own post replies | Conversation sync | NATIVE_API | Official API | Browser enrichment | DONE/API | P0 | deterministic relational sync |
| Deep comment tree | Flattened conversation + relational tree | NATIVE_API | Official API | Browser enrichment | DONE/API | P0 | no DOM indentation guessing |
| Periodic comment crawl | Scheduled conversation sync | NATIVE_API | Scheduler + API | Browser if required | PLANNED | P1 | C6 |
| Search public posts | Keyword/topic search | NATIVE_API | Official API | none in C4 | DONE/API (docs contract) | P1 | issue #3 live verification remains open |
| Search topic/tag | Topic-tag discovery | NATIVE_API | Official API | none in C4 | DONE/API (docs contract) | P1 | search_mode TAG; issue #3 live verification remains open |
| Public user lookup | Public profile lookup | NATIVE_API | Official API | none in C4 | DONE/API (docs contract) | P1 | response fields require issue #3 live verification |
| Public profile post collection | profile_posts | NATIVE_API | Official API | none in C4 | DONE/API (docs contract) | P1 | response fields require issue #3 live verification |
| Mention monitoring | Mentions | NATIVE_API | Official API | none | DONE/API (docs contract) | P1 | time/cursor support requires issue #3 live verification |
| Lead discovery | DiscoveryCampaign -> LeadCandidate | NATIVE_API | API-first pipeline | none in C4 | DONE/API (docs contract) | P1 | no browser enrichment or scheduler |
| Competitor/public-account monitoring | Public profile/posts + discovery | HYBRID | API | Browser enrichment | PLANNED | P1 | policy/retention required |
| CSV/report export | Query/export | NATIVE_API | Application | none | DEFERRED | P2 | reporting concern |
| Post/comment local JSON tree | PostgreSQL relational persistence | NATIVE_API | PostgreSQL | none | DONE | P0 | replaces post_structure.json |
| Device/account registry | Worker + Account registry | HYBRID | Control Plane | none | PLANNED | P0 | C1 |
| Chrome profile per account | BrowserProfile | BROWSER_ASSISTED | assigned Worker | none | PLANNED | P0 | logical profile_ref |
| Login using persistent profile | Operator login + persisted session | HUMAN_ASSISTED | Worker + operator | none | PLANNED | P0 | no password-as-core model |
| Session health | Browser session lifecycle | BROWSER_ASSISTED | Worker | Human intervention | PLANNED | P0 | challenge/session-expired states |
| Per-account proxy | NetworkProfile | BROWSER_ASSISTED | assigned Worker | direct connection if policy allows | PLANNED | P1 | routing config, not evasion |
| Feed browsing | Explicit BrowseFeed capability | BROWSER_ASSISTED | Browser Worker | none | PLANNED | P1 | no random warm-up loop |
| Open/read thread | OpenThread/ReadThread | BROWSER_ASSISTED | Browser Worker | API when equivalent exists | PLANNED | P1 | explicit capability |
| Open profile | OpenProfile | HYBRID | API when sufficient | Browser | PLANNED | P1 | UI enrichment only where needed |
| Like | LikeThread | BROWSER_ASSISTED | Browser Worker | none | VERIFY | P2 | retain only if product requires |
| Follow/unfollow | FollowUser | BROWSER_ASSISTED | Browser Worker | none | VERIFY | P2 | no Facebook friend semantics |
| Random reactions | None | UNSUPPORTED | none | none | DROP | - | random engagement is not an architecture objective |
| Add Facebook friend | Threads follow semantics | BROWSER_ASSISTED | Browser Worker | none | VERIFY | P2 | adapted outcome only |
| Facebook group search | Recruitment/community discovery | HYBRID | API discovery | Browser enrichment | PLANNED | P1 | business outcome, not group primitive |
| Join Facebook group | No direct parity | N/A | none | none | DROP | - | Threads-native community capability may be evaluated separately |
| Auto-answer group join questions | No direct parity | N/A | none | none | DROP | - | Facebook-specific |
| Messenger DM | Threads/private messaging workflow | HUMAN_ASSISTED | Human | Browser only after separate review | VERIFY | P2 | do not assume public API support |
| Facebook Watch loop | Feed/media browsing | BROWSER_ASSISTED | Browser Worker | none | DROP direct parity | - | no dedicated Watch clone |
| “Account nurturing” | AccountActivityPlan | HYBRID | Scheduler -> explicit jobs | Human | PLANNED | P1 | product term may remain in UI |
| Human-like typing/scrolling for detection avoidance | None | UNSUPPORTED | none | none | DROP | - | not a project objective |
| Anti-detect browser flags/fingerprint spoofing | None | UNSUPPORTED | none | none | DROP | - | explicitly excluded |
| Process hiding | Service/worker management | NATIVE_API | OS service manager | none | DROP direct parity | - | operational packaging replaces hiding |
| Worker process recovery | Worker health/restart | HYBRID | Worker Agent | Control Plane | PLANNED | P0 | C1/C6 |
| Posting quota | API quota query | NATIVE_API | Official API | unknown-safe policy | DONE/API | P1 | no hardcoded quota |
| Reply moderation | Reply management | NATIVE_API | Official API | Browser only if separately approved | DONE/API | P1 | live account permission still TP-002 gate |
| Insights | Threads Insights | NATIVE_API | Official API | none | PLANNED | P2 | snapshots, not overwritten counters |
| Worker software update | Drain/update workflow | HYBRID | Control Plane + Worker | manual | PLANNED | P1 | C6 |
| Remote diagnostics | Worker health/diagnostics | HYBRID | Worker Agent | manual | PLANNED | P1 | no secrets in diagnostics |

## 5. Official API evidence baseline

Current official Meta Threads workspace (checked 2026-09-25) documents:
- OAuth flows and token refresh;
- public profile lookup;
- public profile posts;
- publishing;
- reply/conversation management;
- keyword/topic search;
- mentions;
- insights;
- quota retrieval.

Meta explicitly warns that the Postman collection may lag the latest developer changelog. Implementation must verify the current developer docs/changelog before changing a contract.

## 6. Rules

1. DONE/API means implemented against documentation contracts; it does not imply TP-002 live verification is complete.
2. VERIFY cannot be silently promoted to production-ready.
3. Browser execution requires the distributed-worker architecture and an explicit capability; no one-off Selenium/Playwright calls in application/domain code.
4. Browser fallback is not an excuse to bypass account authorization, challenges, platform controls, or rate limits.
5. Human-assisted is a valid first-class outcome.
6. N/A/DROP rows do not need literal parity if the useful business outcome is covered elsewhere.
7. Every future feature PR must update this matrix when its execution class or delivery status changes.
