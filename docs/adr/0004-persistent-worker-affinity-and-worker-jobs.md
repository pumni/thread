# ADR-0004: Persistent Account Affinity and Remote WorkerJob

- Status: Accepted
- Date: 2026-09-25

## Context

Browser accounts use persistent local browser profiles. Moving those profiles automatically between machines creates profile-locking, consistency, versioning and recovery complexity.

Existing Command lifecycle represents business intent but is not sufficient to represent remote machine ownership and connectivity.

## Decision

1. Browser accounts use persistent AccountWorkerAssignment.
2. Automatic profile migration is not supported.
3. Remote execution is represented by WorkerJob, separate from Command.
4. WorkerJob has its own lease/fencing token, attempts and checkpoint.
5. Browser job may only be claimed by the eligible assigned worker.
6. If the worker is offline:
   - API fallback is allowed only by explicit capability/account policy;
   - otherwise Command/WorkerJob waits.
7. Worker presence and WorkerJob lease are separate signals.

## Consequences

- Remote worker loss does not destroy business state.
- Stale workers cannot finalize after reclaim.
- Browser identity stays stable.
- Manual reassignment can be added later as an explicit operation.
- Control Plane must maintain routing/assignment indexes and durable remote-job state.
