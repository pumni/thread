# Windows Worker install, update, and rollback runbook

The packaged Worker is a native Windows SCM service. The Control Plane owns durable Worker
status and WorkerJob leases; the operator owns the immutable release directories and SCM service
configuration. DRAINING never kills a process or interrupts a running handler.

## First install

1. Obtain the exact-head Windows package ZIP, SHA-256 file, and safe build manifest from the
   workflow artifact. Verify the digest and extract the package beneath
   `C:\Program Files\ThreadsWorker\releases\<version>-<sha>\`.
2. Run that release's `threads-worker.exe --package-check`. It exercises a temporary identity,
   current-user DPAPI, local journal, and bundled Chromium on `about:blank`; it makes no Control
   Plane or external network call.
3. From an elevated PowerShell session, register the service using only non-secret settings:

   ```powershell
   .\Install-ThreadsWorkerService.ps1 `
     -ReleaseDirectory 'C:\Program Files\ThreadsWorker\releases\<version>-<sha>' `
     -ControlPlaneUrl 'https://control.example' `
     -PromptForEnrollment
   ```

   `-PromptForEnrollment` prompts with `Read-Host -AsSecureString` and writes only the protected
   bootstrap file. Never pass the code in command-line arguments, service registry Environment,
   or ImagePath. The installer does not accept a token/password parameter.
4. The installer configures `NT AUTHORITY\LocalService`, ImagePath ending in
   `threads-worker.exe --windows-service`, and the persistent data root
   `%ProgramData%\ThreadsOperations`. It protects that root and its managed directories with
   LocalService Modify, SYSTEM FullControl, and Administrators FullControl, without broad
   Users/Everyone write access. It does not grant LocalService write access to the release tree.
5. Start the service and verify its SCM state and Control Plane Worker status:

   ```powershell
   sc.exe start ThreadsOperationsWorker
   sc.exe query ThreadsOperationsWorker
   ```

   After enrollment succeeds, the Worker removes `bootstrap\enrollment-code`. Unlinking is
   best-effort; Windows does not promise forensic secure erase.

## Update

1. Request an admin drain with a bounded reason code such as `UPDATE_REQUESTED`.
2. Confirm the Worker reports `DRAINING`.
3. Poll drain status until `quiescent` is true. The status reports worker ID, status, active
   browser session count, RUNNING WorkerJob count, and the derived quiescent flag. An expired
   RUNNING lease still blocks this step.
4. Let the WorkerAgent close managed sessions, flush STOPPED reports, report zero active
   sessions, and complete its worker-authenticated handshake.
5. Confirm the Worker reports `OFFLINE` and verify that it has no RUNNING WorkerJobs.
6. Request SCM stop and wait for `STOPPED`:

   ```powershell
   sc.exe stop ThreadsOperationsWorker
   sc.exe query ThreadsOperationsWorker
   ```

   SCM STOP uses the same authenticated self-drain path and is idempotent after an admin drain.
   Do not kill the process if it remains `STOP_PENDING`. When the Control Plane is unavailable,
   it cannot complete the durable handshake; it must reconnect and resume.
7. Verify the new package ZIP's SHA-256 against the exact workflow artifact record. Extract it to
   a new immutable release directory beneath `C:\Program Files\ThreadsWorker\releases\` and
   run its `threads-worker.exe --package-check`.
8. From elevated PowerShell, update only the release path. The installer requires the service
   already stopped and preserves the LocalService account, data root, and existing non-secret
   service environment:

   ```powershell
   .\Install-ThreadsWorkerService.ps1 `
     -ReleaseDirectory 'C:\Program Files\ThreadsWorker\releases\<new-version>-<sha>'
   ```
9. Start the service, allow it to authenticate and send hello, and verify it reaches `ONLINE`
   or `UPGRADE_REQUIRED` before assigning new work:

   ```powershell
   sc.exe start ThreadsOperationsWorker
   sc.exe query ThreadsOperationsWorker
   ```

## Abort or rollback

An admin may abort a drain only to `OFFLINE`. Abort is recovery, not proof of quiescence, and does
not rewrite browser sessions or WorkerJobs. A later normal hello or heartbeat may establish
`ONLINE` under the existing presence rules. Check drain status and verify no durable RUNNING jobs
remain before replacing software.

If the replacement package fails, stop it through SCM when possible, then run the installer with
the previous immutable release directory. Do not change or recreate the data root. Preserve the
same `worker_id`, LocalService account, DPAPI device identity, local journal, profiles, media,
and previous package. Current-user DPAPI is bound to the LocalService account; changing to
LocalSystem, an interactive user, or machine-scope DPAPI breaks identity continuity. Never
regenerate identity or copy an old profile/data snapshot over newer durable state.

`Uninstall-ThreadsWorkerService.ps1` requests graceful stop, waits for SCM `STOPPED`, removes only
the SCM registration, and leaves durable data and release directories in place. If its bounded
external wait expires, it leaves the service and data intact; it does not force termination.

## Service and package boundaries

```text
C:\Program Files\ThreadsWorker\releases\<version>-<sha>\
  threads-worker.exe
  _internal\
  BUILD-MANIFEST.json
%ProgramData%\ThreadsOperations\
  worker\
  profiles\
  journal\worker-state.sqlite3
  media\
  bootstrap\enrollment-code   (only during initial enrollment)
```

Durable Worker state is outside the release package and never lives in LocalService's implicit
`%LOCALAPPDATA%`. Persistent service Environment contains only explicitly allow-listed
non-secret settings; it never contains enrollment/session/OAuth/proxy credentials, passwords, or
private keys. Use only `sc.exe` and the supplied PowerShell/ACL scripts. This checkpoint does not
add WinSW, NSSM, a downloader, self-updater, installer service wrapper, Authenticode signing, or a
production release channel. The artifact is unsigned and internal/test only; its hash identifies
the bytes but does not authenticate the publisher. #78 DRAINING semantics remain unchanged.
