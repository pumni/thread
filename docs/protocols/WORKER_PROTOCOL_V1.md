# Worker Protocol v1

## 1. Purpose

Define the Control Plane <-> Worker Agent contract for C1.

This protocol intentionally excludes browser-specific commands and DOM selectors.

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

Exact algorithms/key storage must be documented in the implementation PR and security-reviewed.

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

All state-mutating job requests require:
- authenticated worker_id;
- job_id;
- current lease_token;
- protocol version where applicable.

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
- prior attempt is closed as retryable/final according to policy;
- job may become reclaimable;
- new claim receives a different lease token.

Old token never becomes valid again.

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
