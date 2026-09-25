# Windows Worker Agent foundation

The C3-01 entrypoint is `python -m threads_platform.workers`. It runs a single local agent
process, authenticates through Worker Protocol v2 over HTTPS, reports presence/capacity, and
reconciles durable WorkerJobs after reconnect. C3-01 installs no browser engine and advertises
no business-action capabilities, so it does not claim or execute browser work.

By default local state lives below `%LOCALAPPDATA%/ThreadsOperations`:

- `worker/` stores the stable worker UUID, DPAPI-protected device key, and process lock;
- `profiles/` stores directories resolved from logical account profile references;
- `journal/worker-state.sqlite3` stores bounded recovery/session metadata only;
- `cache/`, `logs/`, and `updates/` are reserved local data areas.

Configure `THREADS_WORKER_CONTROL_PLANE_URL` with an HTTPS origin. The HTTP client keeps normal
certificate verification enabled and rejects URLs containing credentials. Set optional
`THREADS_WORKER_DISPLAY_NAME`, `THREADS_WORKER_AGENT_VERSION`,
`THREADS_WORKER_MAX_CONCURRENT_JOBS`, and `THREADS_WORKER_MAX_BROWSER_SESSIONS` values as needed.

For a first enrollment, provide the one-time C1 enrollment code through
`THREADS_WORKER_ENROLLMENT_CODE` using the deployment's protected configuration mechanism. The
agent consumes it once, records only a local enrollment-state marker, and never persists or
logs the code. The device key is generated locally, serialized as PKCS#8, protected with
current-user Windows DPAPI `CryptProtectData` using worker-specific optional entropy, and
stored with a versioned envelope. Existing unreadable key material fails closed.

`THREADS_WORKER_DATA_ROOT` can override the default root for managed deployments and tests.
The override is still treated as the security boundary: it must live outside Git worktrees, and
profile resolution rejects paths that escape it. Non-Windows test environments use an injected
fake data protector; the persistent entrypoint itself requires Windows DPAPI.
