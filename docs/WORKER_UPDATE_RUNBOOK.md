# Interactive Windows Worker install, update, and rollback

The current host runs the headed browser Worker under a stable dedicated Windows user who is
logged in interactively. It uses Task Scheduler, not a Windows service. The same Windows user
owns the Worker's current-user DPAPI identity across all releases. Keep Worker data outside
Program Files and preserve `%LOCALAPPDATA%\ThreadsOperations` through updates and rollback.

## First enrollment and task installation

1. Log in as the dedicated Worker user. Do not use LocalSystem, LocalService, or
   NetworkService.
2. Download the unsigned internal/test ZIP through the approved channel and verify its SHA-256
   against the exact Windows workflow artifact. Extract it into a new immutable release folder:
   `C:\Program Files\ThreadsWorker\releases\<version>-<git-sha>\`.
3. Run `threads-worker.exe --version` and `--package-check` from that release. Package check
   uses temporary identity, current-user DPAPI, journal, and bundled Chromium state; it does not
   contact the Control Plane or modify the durable Worker root.
4. Create a UTF-8 `threads-worker-host-v1` JSON file at
   `%LOCALAPPDATA%\ThreadsOperations\host\worker-host.json`. Its only allowed settings are
   existing non-secret deployment values: `control_plane_url`, `data_root`, `display_name`,
   `agent_version`, `max_concurrent_jobs`, `max_browser_sessions`, and explicit capability
   booleans. Paths are absolute and bounded. Unknown fields, including enrollment codes,
   tokens, credentials, account/profile identifiers, and private keys, are rejected. Present
   host-config values take precedence over matching ordinary environment settings; omitted
   fields fall back to those settings. Enrollment is always process-environment-only.
5. Obtain a one-time enrollment code. Enter it at a secure prompt and keep it only in the
   launching PowerShell process environment. A protected operator script can read a
   `SecureString`, convert it briefly for the process environment, launch the executable, then
   clear the environment and temporary unmanaged buffer in `finally`. Do not place the code in
   a command, argument, config, script, profile, task definition, registry environment, or log.
6. Launch `threads-worker.exe --host-config <absolute-config-path>` interactively. Verify the
   Worker reaches ONLINE. Request durable drain, wait for DRAINING and quiescence, and let the
   Worker finish the #78 session-close/report-flush/zero-capacity flow to OFFLINE. Clear the
   enrollment environment state and verify the local identity marker says ENROLLED without
   printing its UUID. Registration refuses a missing or PENDING identity.
7. Register the task from the same logged-in user:

   ```powershell
   .\packaging\windows_worker\Manage-ThreadsWorkerTask.ps1 `
     -Operation Install `
     -ReleaseDirectory 'C:\Program Files\ThreadsWorker\releases\<version>-<git-sha>' `
     -HostConfigPath "$env:LOCALAPPDATA\ThreadsOperations\host\worker-host.json" `
     -ExpectedProjectVersion '<version>' `
     -ExpectedGitSha '<40-character-git-sha>'
   ```

   The manager verifies the manifest and release tree, then registers the bounded task
   `ThreadsPlatformWorker`: current user, `Interactive` token, `Limited` run level, same-user
   logon trigger, `IgnoreNew`, unlimited duration, and `AllowHardTerminate=false`. It stores no
   password. Start it in the current session with
   `Start-ScheduledTask -TaskName ThreadsPlatformWorker`, or let the next logon trigger start
   it. `Inspect` reports a bounded task state only.

The production task action directly invokes the selected immutable release's
`threads-worker.exe --host-config <absolute-path>`. No credentials are accepted by the manager.
Durable identity, DPAPI key, profiles, journal, and media remain outside release directories.
Task registration requires an explicit absolute `data_root` in the host config and rejects roots
under either Program Files directory. It does not use the installer's process environment as the
persistent task's data-root source; the task reads the same saved host config at every logon.
Do not move the identity to another Windows user: current-user DPAPI keys are bound to their
original principal.

## Planned update

1. Request a Control Plane drain.
2. Wait for durable DRAINING, `quiescent=true`, and WorkerAgent completion to OFFLINE. Any
   RUNNING WorkerJob blocks quiescence even after its lease expires. Do not stop the task to
   force drain.
3. Confirm the task is no longer Running and verify no durable RUNNING jobs remain.
4. Verify the new workflow ZIP SHA-256, extract to a new immutable version/SHA release
   directory, then run `--version` and `--package-check` from that release.
5. From the same dedicated user, update the task action:

   ```powershell
   .\packaging\windows_worker\Manage-ThreadsWorkerTask.ps1 `
     -Operation Update `
     -ReleaseDirectory 'C:\Program Files\ThreadsWorker\releases\<new-version>-<new-git-sha>' `
     -ExpectedProjectVersion '<new-version>' `
     -ExpectedGitSha '<new-40-character-git-sha>' `
     -ConfirmDurableDrainOffline
   ```

   The command requires explicit operator confirmation, refuses a Running task, retains the
   existing host config, and never removes the previous release. Inspect settings/action, then
   start the task or allow the next logon trigger. Verify hello reports the expected version
   and the Worker reaches ONLINE or `UPGRADE_REQUIRED` as appropriate. Retain the previous
   release until the replacement is verified and rollback is no longer needed.

Do not call `Stop-ScheduledTask` as the planned update mechanism. The Worker must complete the
durable #78 DRAINING -> quiescent -> OFFLINE handshake itself. DRAINING blocks new claims while
already-running work reaches its existing safe terminal boundary.

## Rollback and uninstall

If a replacement release fails after it starts, drain it normally and wait for quiescence and
OFFLINE. Verify the task is no longer Running, then run `-Operation Update` with the previous
intact release and `-ConfirmDurableDrainOffline`. Keep the same config, data root, Windows user,
Worker identity, and DPAPI key. Start the old release and verify hello/identity continuity.
Never restore an old copy of profiles or journal over newer state, regenerate identity, or
delete the previous release during a switch.

To unregister the task, first complete durable drain to OFFLINE and verify it is not Running,
then call `-Operation Uninstall -ConfirmDurableDrainOffline`. Uninstall removes only the task;
release directories, config, identity, key, profiles, journal, and other Worker data remain.

## Abrupt logoff or host loss

Interactive user logoff, OS shutdown, crash, or user process termination can end the Worker
abruptly. That is not proof of DRAINING completion or quiescence. Task Scheduler is configured
not to hard-terminate the task during planned lifecycle operations, but it cannot guarantee
graceful work completion when Windows destroys the interactive session. Existing durable
WorkerJob lease recovery and scheduler-owned Worker presence expiry remain authoritative.
Reconnect the same user and identity; do not treat an abrupt exit as OFFLINE quiescence proof.

## Release trust and scope

`BUILD-MANIFEST.json` validates package identity and SHA-256 detects changes to exact bytes;
neither authenticates a publisher. This is an unsigned internal/test artifact without
Authenticode signing, a production release channel, downloader, or self-updater. The current
headed-browser/DPAPI architecture does not use a Windows service. A service wrapper can be
reconsidered only after a separately reviewed and validated headless/browser-host design.
Issue #3 remains open as the production release gate, metrics/tracing are not implemented, and
#62 remains separate.
