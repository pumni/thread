# TP-002 Threads API Capability Spike

**Status:** Documentation review is complete for implementation planning. Live TP-002 verification remains open because no local development app or account is available. Coordinator authorization allows TP-005/TP-006 coding against the documented contract and mocked tests; it does not authorize production activation.

**Evidence reviewed:** 2026-09-25. The primary reference was [Meta's Threads API Postman workspace](https://www.postman.com/meta/threads/overview) and its [Threads API collection](https://www.postman.com/meta/threads/documentation/dht3nzz/threads-api). The collection directs implementers to Meta's developer documentation for the latest limits and behavior. Direct developer-documentation requests were rate-limited during this review, so this report does not treat collection examples as live responses.

No API calls were made with a development account. No response fixtures were captured. Any schema example used in code tests is labelled a **documentation-contract fixture** and is not a live fixture or account response.

## Documented contract

| Capability | Meta collection documents | Live validation |
|---|---|---|
| Authorization code exchange | `POST /oauth/access_token` with `client_id`, `client_secret`, one-time `code`, `grant_type=authorization_code`, and the exact registered `redirect_uri`; example response contains `access_token` and `user_id`. | Blocked |
| Long-lived access token | `GET /access_token?grant_type=th_exchange_token` with the app secret; example contains bearer token and `expires_in=5184000` seconds. | Blocked |
| Refresh | `GET /refresh_access_token?grant_type=th_refresh_token`; the collection says `threads_basic` is sufficient, but shows no response body. | Blocked; response and lifecycle behavior unresolved |
| Own identity | `GET /me` with profile fields such as `id`, `username`, `name`, profile picture URL, and biography. | Blocked |
| Text, image, video, carousel publishing | Create a media container with `POST /me/threads`, then publish it with `POST /me/threads_publish`; image/video media URLs must be publicly accessible. Carousel uses child containers followed by a parent container. Quote posts use `quote_post_id`. | Documented; no media was published live. Processing and permissions remain unverified |
| Repost | The collection documents `POST /{thread_id}/repost`, but does not establish an idempotency key or a safe reconciliation contract after a lost response. | **VERIFY**; do not retry an ambiguous repost automatically |
| Published media retrieval and container status | The collection documents retrieving media fields and container states `EXPIRED`, `ERROR`, `FINISHED`, `IN_PROGRESS`, and `PUBLISHED`. The status example returns the container `id` and status; it does not document a published media ID in that response. | Documented; after `PUBLISHED` without a publish response ID, code preserves an ambiguous outcome and never republishes blindly |
| Replies and conversation | `GET /{thread_id}/replies` and `GET /{thread_id}/conversation`; documented replies include `root_post` and `replied_to`, and responses include before/after paging cursors. Conversation is described as a paginated flattened list of top-level and nested replies. | Documented; pagination, cursor and live permission behavior remain unverified |
| Reply creation | `POST /me/threads` with `media_type=TEXT` and `reply_to_id`; the collection also documents `reply_to_id` for responding to replies. | Blocked |
| Reply moderation | The collection documents top-level hide/unhide via `/{reply_id}/manage_reply` and pending-reply approve/ignore via `/{reply_id}/manage_pending_reply`, with a success response example. | Documented; account permissions and ambiguous mutation recovery remain **VERIFY** |
| Quota | `GET /me/threads_publishing_limit` with fields including `quota_usage`, `config`, `reply_quota_usage`, and `reply_config`. The endpoint is documented; no quota was queried for an account. | Documentation contract only; values are read dynamically and never hardcoded |
| Permissions | The collection lists `threads_basic`, `threads_content_publish`, `threads_read_replies`, `threads_manage_replies`, and `threads_manage_insights`, and recommends the token debugger to inspect granted permissions and expiry. It does not provide a reliable per-endpoint scope table. | Blocked; no token or debugger result |
| Error responses | The reviewed examples do not establish current Threads error JSON, status mapping, or retry headers for OAuth, permission, media, quota, rate-limit, and server failures. | Blocked |

## Acceptance and implementation gate

- Documentation-level capability discovery: **reviewed for #6/#7 implementation**, with ambiguous response details kept optional and marked VERIFY.
- OAuth code exchange, refresh, identity, publishing, retrieval, replies, quota, permissions, and error behavior against a development account: **not verified**.
- Scrubbed live request/response fixtures: **not available**; no live fixtures were fabricated from example schemas. Mock tests use documentation-contract fixtures only.
- `FEATURE_PARITY_MATRIX.md`: repost is now marked **VERIFY** because the documented endpoint has no safe lost-response reconciliation contract; quote-post remains documented. Live permissions and behavior remain unverified.
- TP-005/TP-006 implementation against the documented contract is authorized. TP-002 remains a **production/release gate**, not a coding gate. No undocumented response behavior or browser automation is assumed.
- Threads command handlers obtain a `SecretStr` through an injected access-token provider. This batch does not implement credential storage, OAuth exchange, or refresh coordination; those remain outside this code scope and require TP-002/operations review before production.

To complete TP-002 before production/release certification, use a dedicated development app/account and record only scrubbed evidence. Still requiring live verification: **effective OAuth scopes; refresh response and token lifecycle; actual quota; Meta error payload/status/header behavior; account-specific permissions; actual media processing behavior; and moderation permissions**. Never record tokens, app secrets, authorization codes, or sensitive account identifiers.

## Source requests

- [Authorization and token lifecycle](https://www.postman.com/meta/threads/folder/34203612-e0373e84-de6b-46f1-b90d-3fea76ba6782)
- [User profile](https://www.postman.com/meta/threads/request/q9os31a/get-threads-user-s-profile-information)
- [Image container](https://www.postman.com/meta/threads/request/34203612-c5844b32-22e8-4c0c-9564-2694cede8304) and [video container](https://www.postman.com/meta/threads/request/34203612-a51eb091-5c5e-436a-b4bb-c3d53c8aa901)
- [Container publishing status](https://www.postman.com/meta/threads/request/m47wqlq/check-container-s-publishing-status)
- [Publishing and quote/repost requests](https://www.postman.com/meta/threads/documentation/dht3nzz/threads-api?entity=request-34203612-ee0a2365-9d95-4cbe-8087-1cfb04d38c05)
- [Publishing quota](https://www.postman.com/meta/threads/request/34203612-f4590341-9bee-44f5-901b-606078a03c96)
- [Replies, conversations, and moderation](https://www.postman.com/meta/threads/documentation/dht3nzz/threads-api?entity=request-34203612-13ebe336-0176-4d2b-b208-c36646093139)
