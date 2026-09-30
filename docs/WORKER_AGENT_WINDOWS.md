# Windows Worker Agent foundation

The Worker Agent entrypoint is `python -m threads_platform.workers`. It runs a single local agent
process, authenticates through Worker Protocol v2 over HTTPS, reports presence/capacity, and
reconciles durable WorkerJobs after reconnect. By default it advertises no business capabilities.
An explicitly provisioned worker can opt in to `threads.browser.feed.browse` v1 by setting
`THREADS_WORKER_FEED_BROWSE_ENABLED=true`; that enables only the reviewed read-only feed workflow.

The console command is `threads-worker`. `threads-worker --version` prints only the installed
project version and does not create Worker data or contact a service. The packaged
`threads-worker.exe --package-check` uses a temporary data root to exercise Worker identity,
current-user DPAPI, the SQLite journal, and the bundled Chromium launch on `about:blank`. It does
not read or change the configured durable data root, Control Plane configuration, or network.

The packaged executable also hosts the native Windows SCM service. The production service runs
as `NT AUTHORITY\LocalService` with ImagePath ending in `threads-worker.exe --windows-service`;
no WinSW, NSSM, pywin32 ServiceFramework, or wrapper binary is used. It stores durable state at
`%ProgramData%\ThreadsOperations`, never under LocalService's implicit `%LOCALAPPDATA%`. The
service installer protects the data root and child directories with LocalService Modify,
SYSTEM FullControl, and Administrators FullControl, without Users/Everyone write access. The
release tree remains outside that data root and is not writable by LocalService.

The service's DPAPI device key uses current-user DPAPI under the LocalService account. It is not
machine-scope DPAPI. Keep the service account and data root unchanged across upgrades and rollback;
changing either breaks identity continuity. `--windows-service-check` is a test-only SCM mode. It
requires an isolated `THREADS_WORKER_SERVICE_CHECK_ROOT` beneath ProgramData, loads or creates
LocalService DPAPI identity plus the local journal, makes no network call, and waits for SCM STOP.

SCM STOP and SHUTDOWN are graceful drain requests. The service reports STOP_PENDING, stops new
local claims, lets any awaited WorkerJob handler return normally, then posts authenticated
`POST /v1/workers/drain/request-self` with the fixed `SERVICE_STOP_REQUESTED` reason. It reuses
the durable DRAINING handshake: close browser sessions normally, flush STOPPED reports, report
zero active sessions, complete to OFFLINE, and only then report SCM STOPPED. It never cancels a
handler, kills a browser/process, revokes a lease, or forces a timeout. If the Control Plane is
unavailable, it stays STOP_PENDING, does not claim more work, reconnects, and resumes finalization.

By default local state lives below `%LOCALAPPDATA%/ThreadsOperations`:

- `worker/` stores the stable worker UUID, DPAPI-protected device key, and process lock;
- `profiles/` stores directories resolved from logical account profile references;
- `journal/worker-state.sqlite3` stores bounded recovery/session metadata only;
- `cache/`, `logs/`, and `updates/` are reserved local data areas.

Configure `THREADS_WORKER_CONTROL_PLANE_URL` with an HTTPS origin. The HTTP client keeps normal
certificate verification enabled and rejects URLs containing credentials. Set optional
`THREADS_WORKER_DISPLAY_NAME`, `THREADS_WORKER_AGENT_VERSION`,
`THREADS_WORKER_MAX_CONCURRENT_JOBS`, and `THREADS_WORKER_MAX_BROWSER_SESSIONS` values as needed.
Feed browsing also requires the pinned Playwright package and its matching Chromium browser
binary to be provisioned on the worker. Browser sessions use the assigned account's managed
profile and NetworkProfile. Routes that reference proxy credentials fail closed because this
entrypoint has no production proxy-secret provider.

For console enrollment, provide the one-time C1 enrollment code through
`THREADS_WORKER_ENROLLMENT_CODE` using the deployment's protected configuration mechanism. A
LocalService installation must not put enrollment codes or other credentials in process
environment, service registry Environment, CLI arguments, or ImagePath. Use the installer prompt
(`Read-Host -AsSecureString`) to write the code to the ACL-protected
`%ProgramData%\ThreadsOperations\bootstrap\enrollment-code` file. After successful enrollment,
the agent unlinks that file; Windows does not promise forensic secure erase. The device key is
generated locally, serialized as PKCS#8, protected with current-user Windows DPAPI
`CryptProtectData` using worker-specific optional entropy, and stored with a versioned envelope.
Existing unreadable key material fails closed.

`THREADS_WORKER_DATA_ROOT` can override the default root for managed deployments and tests.
The override is still treated as the security boundary: it must live outside Git worktrees, and
profile resolution rejects paths that escape it. Non-Windows test environments use an injected
fake data protector; the persistent entrypoint itself requires Windows DPAPI.

## Windows x64 package

The internal package is built as a PyInstaller `onedir` bundle from the locked project
dependencies. It contains the Worker executable, PyInstaller runtime, Playwright 1.63.0,
Playwright's matching Chromium, and `BUILD-MANIFEST.json`. Firefox and WebKit are not installed
or bundled. Use the exact-head Windows package workflow to obtain the ZIP, SHA-256 file, and
manifest; see `docs/WORKER_UPDATE_RUNBOOK.md` for installation and update steps.

The archive is unsigned and intended only as an internal/test artifact. Its SHA-256 identifies
the bytes but does not authenticate the publisher. There is no Authenticode signature, production
release channel, downloader, or self-updater. Use
`packaging/windows_worker/Install-ThreadsWorkerService.ps1` and
`Uninstall-ThreadsWorkerService.ps1`; they use built-in SCM and PowerShell ACL/registry APIs.
The update and rollback sequence is in `docs/WORKER_UPDATE_RUNBOOK.md`. Windows service support
does not implement signing, package download, or automatic update.
