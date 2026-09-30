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

For a first enrollment, log in as the dedicated Worker user and provide the one-time C1
enrollment code only in the launching process environment as
`THREADS_WORKER_ENROLLMENT_CODE`. The agent consumes it once, records only a local
enrollment-state marker, and never persists or logs the code. Clear the environment variable
after enrollment. The device key is generated locally, serialized as PKCS#8, protected with
current-user Windows DPAPI `CryptProtectData` using worker-specific optional entropy, and
stored with a versioned envelope. Existing unreadable key material fails closed. Keep the same
Windows user across installs, updates, and rollback: current-user DPAPI identity cannot be moved
to a different Windows principal by this host.

`THREADS_WORKER_DATA_ROOT` can override the default root for managed deployments and tests.
The override is still treated as the security boundary: it must live outside Git worktrees, and
profile resolution rejects paths that escape it. Task registration resolves this override using
the same precedence as the Worker runtime and rejects effective roots in either Program Files
directory, including paths outside the release subtree. Non-Windows test environments use an
injected fake data protector; the persistent entrypoint itself requires Windows DPAPI.

## Windows x64 package and interactive host

The internal package is built as a PyInstaller `onedir` bundle from the locked project
dependencies. It contains the Worker executable, PyInstaller runtime, Playwright 1.63.0,
Playwright's matching Chromium, and `BUILD-MANIFEST.json`. Firefox and WebKit are not installed
or bundled. Use the exact-head Windows package workflow to obtain the ZIP, SHA-256 file, and
manifest; see `docs/WORKER_UPDATE_RUNBOOK.md` for installation and update steps.

The archive is unsigned and intended only as an internal/test artifact. Its SHA-256 identifies
the bytes but does not authenticate the publisher. It has no Authenticode signature, production
release channel, downloader, or self-updater.

The production browser Worker remains headed. Run it under a stable dedicated Windows user who
logs in interactively. The supported host is a Task Scheduler task named
`ThreadsPlatformWorker`, configured with that user's `Interactive` token and `Limited` run
level. It starts at that user's logon, ignores duplicate starts, has no execution time limit,
and has `AllowHardTerminate=false`. No password is stored. Do not run this headed browser Worker
as a Windows service: services run outside the interactive desktop, and a service account would
also change the current-user DPAPI security principal.

Use a strict non-secret `threads-worker-host-v1` JSON file and launch the release executable as
`threads-worker.exe --host-config <absolute-path>`. Its allowlisted fields are the existing
Control Plane URL, data root, display name, agent version, capacities, and capability flags.
Present host-config values take precedence over the matching ordinary environment settings;
omitted values fall back to those settings. `THREADS_WORKER_ENROLLMENT_CODE` remains
environment-only and is never accepted in the file or task definition. See the runbook for
first enrollment, task registration, update, and rollback.

The default durable root remains `%LOCALAPPDATA%\ThreadsOperations`, owned by the same user as
the task. Keep identity, DPAPI key, profiles, journal, and media outside every immutable release
directory. Abrupt logoff, shutdown, crash, or user termination is not a graceful drain; existing
WorkerJob lease recovery and presence expiry remain authoritative. Issue #3 remains open and
this unsigned artifact is not a production release claim; #62 remains separate.
