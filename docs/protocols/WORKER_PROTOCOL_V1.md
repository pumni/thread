# Worker Protocol v1

## 1. Purpose

Define the Control Plane <-> Worker Agent contract for C1.

This protocol intentionally excludes browser-specific commands and DOM selectors. Protocol
version 1 remains supported; the additive C3-01 session/capacity extension uses protocol
version 2 with capability schema version 1.

## 2. Principles

- PostgreSQL is source of truth.
- WebSocket accelerates notification but does not own work.
- Durable mutations occur over authenticated HTTPS.
- Every WorkerJob mutation is fenced by a current lease token.
- Worker identity is independent from hostname.
- Account browser affinity is enforced by Control Plane.
- Worker is strict-online.
- Protocol and capability versions are explicit.

## 3. Worker identity

WorkerNode has:
- worker_id: stable UUID/string identifier;
- display_name/hostname;
- platform;
- agent_version;
- worker_protocol_version;
- status;
- last_heartbeat_at;
- max_browser_sessions;
- active_browser_sessions;
- metadata.

Hostname is descriptive, not an authorization key.

## 4. Enrollment/authentication

Target flow:

1. Admin creates a one-time enrollment token/code.
2. Worker starts and generates a device keypair locally.
3. Worker submits enrollment code + public identity.
4. Control Plane consumes the one-time code and records the worker public identity.
5. Worker private key remains on the device and is protected by OS facilities.
6. On reconnect, Control Plane issues a challenge.
7. Worker signs the challenge.
8. Control Plane verifies it and issues a short-lived worker access token.
9. Worker uses that token for HTTPS/WSS.

Security requirements:
- no shared fleet-wide static worker password;
- no private key in Git;
- no worker access token in logs;
- enrollment token is one-time and expiring;
- replayed challenge response must fail.

### C3-01 device storage binding

The persistent Windows Worker Agent stores the locally generated Ed25519 PKCS#8 private key
only after protecting it with Windows DPAPI `CryptProtectData` for the current user. The
worker-specific UUID is included as optional entropy. The on-disk envelope has a fixed
version marker; DPAPI or key-decoding failure is closed as an identity-store error. It never
creates a replacement key over unreadable existing material. Linux contract tests inject a
fake protector. Only the raw public key is enrolled; runtime code receives signing and public
key operations through `WorkerDeviceIdentity`.

Exact algorithms/key storage must be documented in the implementation PR and security-reviewed.

### C1 implementation binding

- Device identity uses Ed25519 (RFC 8032) through the `cryptography` Python package. The
  worker generates the keypair locally and sends only the 32-byte raw public key during
  enrollment. PostgreSQL stores that public key; it has no private-key column.
- The signed challenge message is the UTF-8 bytes of
  `threads-platform-worker-auth-v1\n{canonical-lowercase-worker-challenge-uuid}\n{nonce}`.
  The nonce is 256 bits of random URL-safe text. Challenges expire after one minute and are
  consumed under a row lock; a failed signature consumes the challenge too.
- Enrollment codes and access tokens each contain 256 bits of random material. Enrollment
  codes expire after ten minutes and are single-use. PostgreSQL stores SHA-256 digests of
  enrollment codes and access tokens, never their raw values. Worker sessions expire after
  fifteen minutes.
- `WorkerKeyStore` is the worker-side secure-storage boundary. The later Windows adapter
  will protect the generated private key with DPAPI and expose signing operations without
  returning key bytes. C1 includes the interface and in-memory signer; it does not persist
  private key material.
- Worker HTTPS and WSS routes reject non-TLS ASGI schemes by default. TLS terminates at the
  trusted deployment ingress. Workers must retain normal certificate verification. WSS
  authenticates with the `Authorization: Bearer` header, never a query parameter.
- C1 supports protocol version 1, and C3-01 adds protocol version 2 while retaining capability
  schema version 1. Unsupported versions put the worker in `UPGRADE_REQUIRED`. Heartbeat
  presence expires after 90 seconds and the
  Control Plane marks stale ONLINE/DEGRADED workers OFFLINE. WSS notifications are process
  local and advisory; workers recover from PostgreSQL-backed HTTPS operations.

## 5. Worker states

- REGISTERING
- ONLINE
- DEGRADED
- DRAINING
- OFFLINE
- DISABLED
- UPGRADE_REQUIRED

Eligibility to claim:
- ONLINE only;
- protocol compatible;
- not draining/disabled;
- enough capacity;
- required capability/version advertised;
- account assignment matches.

## 6. Capability advertisement

Each capability is represented by:
- capability_name
- capability_version
- optional metadata

C1 may use synthetic/test capabilities.

Browser Threads capabilities are introduced only after C3.

## 7. WebSocket messages

### Worker -> Control Plane

worker.hello
- worker_id
- protocol_version
- agent_version
- capacity summary

worker.heartbeat
- worker_id
- timestamp
- health summary

worker.capacity
- max/current capacity

worker.session_status
- account_id
- non-secret session state summary

worker.intervention_required
- intervention_id
- account_id
- intervention_type
- safe descriptive metadata

### Control Plane -> Worker

job.available
- job_id only/minimal metadata

job.cancel_requested
- job_id
- reason code

worker.drain
- reason code

worker.upgrade_required
- minimum protocol/agent information

account.refresh_session_state
- account_id

WebSocket payloads are advisory notifications. Full durable state is fetched via HTTPS.

## 8. Durable HTTPS operations

Logical operations, independent of exact route naming:

- enroll worker
- create auth challenge
- exchange signed challenge for short-lived token
- register/update worker capabilities
- claim next eligible WorkerJob
- renew WorkerJob lease
- save WorkerJob checkpoint
- complete WorkerJob
- fail WorkerJob
- request intervention
- resolve/requeue intervention
- reconcile active jobs after reconnect
- fetch the active account assignment and logical profile/network routing context
- report account browser-session state and revision

All state-mutating job requests require:
- authenticated worker_id;
- job_id;
- current lease_token;
- protocol version where applicable.

### C1 route mapping

- `POST /v1/workers/jobs/claim` returns one claimed job or HTTP 204 when none is eligible.
- `GET /v1/workers/jobs/reconcile` returns the worker's owned or claimable durable jobs.
- `POST /v1/workers/jobs/{job_id}/renew`
- `POST /v1/workers/jobs/{job_id}/checkpoint`
- `POST /v1/workers/jobs/{job_id}/complete`
- `POST /v1/workers/jobs/{job_id}/fail`
- `POST /v1/workers/jobs/{job_id}/interventions`
- `POST /v1/workers/interventions/{intervention_id}/resolve` (Control Plane administrator)

Every worker route uses the short-lived bearer session from the challenge exchange. The
intervention resolution route uses the configured Control Plane administrator credential.
Workers should poll claim/reconcile over HTTPS after reconnect and periodically while online;
they must treat `job.available` only as a prompt to pull durable state.

## 9. WorkerJob claim

Eligibility checks:
- job status claimable;
- scheduled_at <= now;
- deadline not expired;
- worker ONLINE;
- worker capability/version matches;
- worker has capacity;
- job worker_id/assignment matches;
- account affinity matches;
- account coordination policy allows execution.

Successful claim:
- status -> RUNNING;
- new lease_token;
- lease_expires_at;
- attempt created/incremented.

Claim transaction is short and releases before remote/local execution.

## 10. Lease renew/checkpoint

Worker periodically renews its job lease.

Checkpoint write requires:
- correct worker;
- correct lease token;
- non-expired lease;
- job still RUNNING.

Checkpoint is bounded JSON execution metadata, not business storage.

## 11. Completion

Completion requires current lease/fencing ownership.

Control Plane transactionally:
- validates ownership;
- writes resulting business state if applicable;
- updates WorkerJob;
- updates/advances Command;
- writes outbox event.

A stale worker receives a lease-lost response and cannot overwrite newer state.

## 12. Retry/reclaim

RUNNING job with expired lease:
- a `SAFE_TO_RETRY` job can be reclaimed within its attempt bound;
- a `RECONCILIATION_REQUIRED` job enters `WAITING_INTERVENTION` instead of being replayed;
- the previous attempt is closed and a later safe requeue creates a new attempt;
- every new claim receives a different lease token.

Old token never becomes valid again.

Explicit retryable failures use a two-second default delay and the default attempt bound is three.
An operator requeue after an ambiguous outcome requires explicit confirmation that retry is safe.
Expired deadlines finalize the job and its linked Command; an open intervention is cancelled.
Checkpoint and result documents are bounded to 64 KiB and reject recognized secret-bearing keys
and URLs containing credentials.

## 13. Intervention

Examples:
- LOGIN_REQUIRED
- SESSION_EXPIRED
- CHALLENGE_REQUIRED
- OPERATOR_CONFIRMATION_REQUIRED

Flow:
- Worker checkpoints safe state;
- WorkerJob -> WAITING_INTERVENTION;
- WorkerIntervention record created;
- operator resolves;
- job may be requeued with fresh lease later.

Never encode account passwords/2FA secrets in intervention payloads.

## 14. Disconnect semantics

If Control Plane connection is lost:
- no new job claim;
- no new irreversible external action unless the worker still has a valid way to durably checkpoint and the execution contract explicitly permits it;
- run only to a defined safe boundary;
- preserve local recovery metadata if needed;
- reconnect and reconcile.

## 15. DRAINING

DRAINING worker:
- cannot claim new jobs;
- existing jobs may finish or cancel at safe boundaries;
- reports when safe for shutdown/update.

## 16. Version compatibility

worker_protocol_version is independent from agent_version.

Control Plane defines supported protocol range.

Incompatible worker:
- remains diagnosable;
- status -> UPGRADE_REQUIRED/DEGRADED;
- receives no incompatible job.

### Protocol v2 additive extension (C3-01)

- Version 1 remains accepted with its original hello and heartbeat fields.
- Version 2 keeps capability schema version 1 and adds `max_browser_sessions` and
  `active_browser_sessions` to hello; heartbeat carries the current active count.
- Presence responses include capacity fields for protocol v2; protocol v1 keeps its original
  response shape.
- The Control Plane stores aggregate capacity on `WorkerNode`. Values contain no account
  identifiers, paths, or secrets, and active sessions cannot exceed the advertised maximum.
- `GET /v1/workers/accounts/{account_id}/context` returns only the authenticated worker's
  active account assignment, logical `profile_ref`, and account-scoped routing metadata.
- `PUT /v1/workers/accounts/{account_id}/session` persists session UUID, state, and a
  monotonic revision after checking current worker/account/profile affinity. Login, expired
  session, and challenge states are explicitly marked as requiring operator intervention.
  Repeating an identical report revision is idempotent so reconnect can finish an ambiguous
  response; a conflicting or older revision is rejected.
- Both endpoints use the short-lived bearer session over HTTPS. WSS remains advisory and is
  not the source of durable session or capacity state.

## 17. Test matrix

C1 must test:
- successful enrollment;
- enrollment replay rejection;
- challenge replay rejection;
- heartbeat/presence expiry;
- correct worker claim;
- wrong worker rejection;
- account-affinity rejection;
- concurrent claim;
- lease renew;
- stale checkpoint rejection;
- stale completion rejection;
- lease expiry/reclaim;
- worker OFFLINE/DRAINING/UPGRADE_REQUIRED cannot claim;
- intervention/requeue;
- Control Plane restart;
- worker reconnect/reconcile;
- WebSocket notification loss with successful HTTPS/pull recovery.
- protocol v1 compatibility and protocol v2 capacity/session reporting.
