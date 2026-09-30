# Worker package update and rollback runbook

This procedure is service-manager neutral. The Control Plane owns durable
Worker status and WorkerJob leases. An update operator owns package replacement
and service stop/start. DRAINING is not a process-kill instruction.

## Update

1. Request an admin drain with a bounded reason code, for example
   `UPDATE_REQUESTED`.
2. Confirm the Worker reports `DRAINING`.
3. Poll drain status until `quiescent` is true. The status reports only worker
   ID, status, active browser session count, RUNNING WorkerJob count, and the
   derived quiescent flag. An expired RUNNING lease still blocks this step.
4. Let the WorkerAgent close managed sessions, flush STOPPED reports, report
   zero active sessions, and complete its worker-authenticated handshake.
5. Confirm the Worker reports `OFFLINE` and verify that it has no RUNNING
   WorkerJobs.
6. Stop the service/process through the external service manager.
7. Verify the new archive's SHA-256 against the exact workflow artifact record.
8. Extract it to a new immutable release directory, for example
   `C:\Program Files\ThreadsWorker\releases\<version>\`.
9. Run that release's `threads-worker.exe --package-check` before switching the
   external service configuration to it.
10. Preserve the data-root permissions and existing
    `%LOCALAPPDATA%\ThreadsOperations` contents. Durable Worker data must remain
    outside every release directory.
11. Switch the future service configuration to the new release directory and
    start it externally using the existing identity store.
12. Let the worker authenticate and send hello. Verify it reaches `ONLINE` or
    `UPGRADE_REQUIRED`.
13. Verify Control Plane health/readiness and worker assignments before
    assigning new work.

Do not stop or replace the package before the worker confirms OFFLINE. If
connectivity fails during finalization, leave it DRAINING and allow the Worker
Agent to reconnect, authenticate, observe durable DRAINING, and resume the
handshake. The server continues to reject new claims while local status may be
stale.

## Abort or rollback

An admin may abort a drain only to `OFFLINE`. Abort is recovery, not proof of
quiescence, and does not rewrite browser sessions or WorkerJobs. A later normal
hello or heartbeat may establish `ONLINE` under the existing presence rules.
Do not replace software solely because an aborted worker reports OFFLINE; check
drain status and verify no durable RUNNING jobs remain first.

If the replacement package fails, drain and stop it externally when possible,
then point the service configuration back to the previous intact release.
Preserve the same data root, `worker_id`, DPAPI identity store, private key, and
previous package. Start the restored release, authenticate, send hello, and
verify worker ID continuity and resulting status. Never regenerate identity or
copy an old profile/data snapshot over newer durable state. No downloader,
self-updater, installer, code signing, or service wrapper is provided by this
checkpoint.

## Package trust and layout

The expected application layout is:

```text
C:\Program Files\ThreadsWorker\releases\<version>\
  threads-worker.exe
  _internal\
  BUILD-MANIFEST.json
%LOCALAPPDATA%\ThreadsOperations\
  worker\
  profiles\
  journal\
  media\
```

Download or copy the ZIP only through the operator's approved channel. The
workflow ZIP and SHA-256 identify and detect changes to those exact bytes; the
hash does not verify who published them. This is an unsigned internal/test
artifact: no Authenticode signing, production auto-update channel, or service
installation is implemented. Service registration and publisher-trust policy
are separate later work. Do not configure a service until the package check
passes and the data root is confirmed to remain outside the release tree.
