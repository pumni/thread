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
7. Replace/update the package using the separately provided packaging
   procedure.
8. Start the Worker Agent using the existing worker identity store.
9. Let the worker authenticate and send hello. Verify it reaches `ONLINE` or
   `UPGRADE_REQUIRED`.
10. Verify Control Plane health/readiness and worker assignments before
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

If the replacement package fails, stop it externally and restore the previous
package. Preserve the existing `worker_id`, identity store, private key, and
previous package. Never regenerate the worker identity as an update recovery
step. Start the restored worker, authenticate, send hello, and verify its
resulting status. No downloader, self-updater, installer, or service wrapper is
provided by this checkpoint.
