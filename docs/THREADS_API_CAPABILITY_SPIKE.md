# TP-002 Threads API Capability Spike

**Status:** Partially complete. Current Meta-owned API documentation has been reviewed, but live development-account verification is blocked because no local development app or account is available.

**Evidence reviewed:** 2026-09-25. The primary reference was [Meta's Threads API Postman workspace](https://www.postman.com/meta/threads/overview) and its [Threads API collection](https://www.postman.com/meta/threads/documentation/dht3nzz/threads-api). The collection directs implementers to Meta's developer documentation for the latest limits and behavior. Direct developer-documentation requests were rate-limited during this review, so this report does not treat collection examples as live responses.

No API calls were made with a development account. No response fixtures were captured. The example schemas below are documentation contracts only; they are not account responses.

## Documented contract

| Capability | Meta collection documents | Live validation |
|---|---|---|
| Authorization code exchange | `POST /oauth/access_token` with `client_id`, `client_secret`, one-time `code`, `grant_type=authorization_code`, and the exact registered `redirect_uri`; example response contains `access_token` and `user_id`. | Blocked |
| Long-lived access token | `GET /access_token?grant_type=th_exchange_token` with the app secret; example contains bearer token and `expires_in=5184000` seconds. | Blocked |
| Refresh | `GET /refresh_access_token?grant_type=th_refresh_token`; the collection says `threads_basic` is sufficient, but shows no response body. | Blocked; response and lifecycle behavior unresolved |
| Own identity | `GET /me` with profile fields such as `id`, `username`, `name`, profile picture URL, and biography. | Blocked |
| Text, image, video, carousel publishing | Create a media container with `POST /me/threads`, then publish it with `POST /me/threads_publish`; image/video media URLs must be publicly accessible. Text can optionally use `auto_publish_text`. Carousel is documented as a separate workflow. | Blocked; no media was published |
| Published media retrieval and container status | The collection documents retrieving media fields and container states `EXPIRED`, `ERROR`, `FINISHED`, `IN_PROGRESS`, and `PUBLISHED`. | Blocked |
| Replies and conversation | `GET /{thread_id}/replies` and `GET /{thread_id}/conversation`; documented replies include `root_post` and `replied_to`, and responses include before/after paging cursors. Conversation is described as a paginated flattened list of top-level and nested replies. | Blocked |
| Reply creation | `POST /me/threads` with `media_type=TEXT` and `reply_to_id`; the collection also documents `reply_to_id` for responding to replies. | Blocked |
| Reply moderation | The collection documents top-level hide/unhide via `/{reply_id}/manage_reply` and pending-reply approve/ignore via `/{reply_id}/manage_pending_reply`. | Blocked |
| Quota | `GET /me/threads_publishing_limit` with fields including `quota_usage`, `config`, `reply_quota_usage`, and `reply_config`. The endpoint is documented; no quota was queried for an account. | Blocked |
| Permissions | The collection lists `threads_basic`, `threads_content_publish`, `threads_read_replies`, `threads_manage_replies`, and `threads_manage_insights`, and recommends the token debugger to inspect granted permissions and expiry. It does not provide a reliable per-endpoint scope table. | Blocked; no token or debugger result |
| Error responses | The reviewed examples do not establish current Threads error JSON, status mapping, or retry headers for OAuth, permission, media, quota, rate-limit, and server failures. | Blocked |

## Acceptance and implementation gate

- Documentation-level capability discovery: **partial**; see the table above.
- OAuth code exchange, refresh, identity, publishing, retrieval, replies, quota, permissions, and error behavior against a development account: **not verified**.
- Scrubbed request/response fixtures: **not available**; none were fabricated from example schemas.
- `FEATURE_PARITY_MATRIX.md`: unchanged. The reviewed material did not establish a capability mismatch, while live behavior remains unverified.
- TP-005/TP-006 live API implementation remains gated on development-account evidence. This document does not authorize assuming undocumented response behavior or using browser automation.

To complete the live portion, run the documented calls with a dedicated development app/account and record only scrubbed responses. Never record tokens, app secrets, authorization codes, or sensitive account identifiers. Confirm the exact per-call scopes, refresh response and expiry behavior, quota values, error body/status/header shapes, media processing behavior, and moderation permissions.

## Source requests

- [Authorization and token lifecycle](https://www.postman.com/meta/threads/folder/34203612-e0373e84-de6b-46f1-b90d-3fea76ba6782)
- [User profile](https://www.postman.com/meta/threads/request/q9os31a/get-threads-user-s-profile-information)
- [Image container](https://www.postman.com/meta/threads/request/34203612-c5844b32-22e8-4c0c-9564-2694cede8304) and [video container](https://www.postman.com/meta/threads/request/34203612-a51eb091-5c5e-436a-b4bb-c3d53c8aa901)
- [Container publishing status](https://www.postman.com/meta/threads/request/m47wqlq/check-container-s-publishing-status)
- [Publishing quota](https://www.postman.com/meta/threads/request/34203612-f4590341-9bee-44f5-901b-606078a03c96)
- [Replies, conversations, and moderation](https://www.postman.com/meta/threads/documentation/dht3nzz/threads-api?entity=request-34203612-13ebe336-0176-4d2b-b208-c36646093139)
