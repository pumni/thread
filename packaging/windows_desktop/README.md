# Windows Desktop runtime packaging spike

This spike compares two PyInstaller onedir candidates that consume the same pinned Python dependency graph and EDB PostgreSQL 17.11 Windows x64 archive:

- `shared`: one `threads-runtime.exe` exposes `http`, `scheduler`, `migrate`, and `version` modes.
- `split`: `threads-http.exe` exposes HTTP and migration modes, while `threads-scheduler.exe` is a separate long-running process.

Both candidates carry the same PostgreSQL `bin`, `lib`, and `share` runtime tree, upstream PostgreSQL license files, generated Python dependency notices, and a manifest with source revision, `uv.lock` hash, archive hash, candidate size, and tree digest. Build output stays under ignored `build/windows-desktop/`; user data and credentials are excluded.

## Build on Windows x64

The build host needs Python 3.14 through uv, the repository's pinned `uv.lock`, PyInstaller 6.22.3 from the `packaging` dependency group, Git, and `curl.exe` (the script falls back to Python's standard library downloader). It downloads the archive in verified HTTP byte ranges and refuses any SHA-256 mismatch. This is build-time tooling; it is not a runtime prerequisite for the packaged candidate.

```powershell
uv sync --locked --no-dev --group packaging
uv run --locked --no-dev --group packaging python packaging/windows_desktop/build_runtime.py --layout all
```

Each candidate is then exercised against its bundled PostgreSQL runtime:

```powershell
powershell -NoProfile -File packaging/windows_desktop/smoke_runtime.ps1 `
  -BundleRoot build/windows-desktop/candidates/shared `
  -EvidencePath build/windows-desktop/evidence/shared-local-smoke.json
powershell -NoProfile -File packaging/windows_desktop/smoke_runtime.ps1 `
  -BundleRoot build/windows-desktop/candidates/split `
  -EvidencePath build/windows-desktop/evidence/split-local-smoke.json
```

The local smoke runs without administrator rights, restricts its temporary PostgreSQL data directory to the current Windows user, protects and round-trips a generated database credential using current-user DPAPI, removes developer runtime variables and tool directories from child-process `PATH`, runs migrations twice, checks refusal of an unknown migration revision, starts HTTP and scheduler processes, checks `/health`, and verifies PostgreSQL data survives a stop/start. `/ready` also includes worker fleet readiness, so an empty test cluster correctly reports not ready until a worker is enrolled. Runtime logs included in evidence are redacted, checked for raw secrets, and hashed. The smoke deletes its temporary cluster and credential after recording results.

`PR Acceptance` and `Main Verification` call the reusable `Desktop Diagnostic` component with an exact source SHA. Draft PRs do not start this Windows work; they run only Secret scan and PR Head Guard. A PR enters the shared acceptance path only when the coordinator marks the reviewed head Ready. The runtime job builds and verifies `shared`, then runs its non-admin package smoke. A Controller join job waits for native and runtime jobs, downloads their exact-SHA artifacts, and runs the non-admin Controller lifecycle smoke against that `shared` candidate. A full acceptance run still includes every frontend, Rust, Tauri, runtime, Controller, ACL, DPAPI, crash/WAL, migration, port/root-failure, and log-redaction gate. For a targeted hosted diagnostic, use `workflow_dispatch`, provide `source_sha`, and choose `runtime_layout`. See `docs/CI_AGENT_WORKFLOW.md` for the mandatory Draft/Ready and hosted-failure protocol.

The fallback `split` layout is a manual-only feasibility gate: use `workflow_dispatch` with an exact `source_sha` and select `runtime_layout: split`. That run builds, verifies, and smokes `split`; it does not run native Tauri or Controller acceptance. Final exact-head acceptance uses the `shared` path inside `PR Acceptance` or `Main Verification`.

For either runtime layout, if the hosted Actions process is an administrator, the wrapper creates one disposable standard local user, loads that user's profile, ACLs the staging root for that user, and runs smoke processes under it; otherwise it uses the existing non-administrator identity. The candidate smoke process receives only the bundled PostgreSQL `bin` directory and `Windows\System32` in `PATH`; the Windows root is excluded because it can contain `py.exe`. Inherited Python, uv, PostgreSQL, Docker, and Desktop database variables are cleared before packaged runtime commands. Candidate evidence records each of `python.exe`, `python3.exe`, `py.exe`, `uv.exe`, `docker.exe`, and `pg_ctl.exe` as resolved/unresolved and includes an absolute resolved path when present. Python, python3, py and uv must not resolve; Docker is informational even if the hosted image exposes it from System32; `pg_ctl.exe` must resolve from the candidate bundle. Evidence also records the Windows image/version, effective privilege, sanitized `PATH`, manifest/checksum, migration, loopback, HTTP, scheduler, persistence, DPAPI, ACL, and log-scrub results. Log scrub is `PASS` only when runtime logs exist and verification passes, `FAIL` when created logs fail verification, and `NOT_RUN` when failure precedes runtime log creation; `NOT_RUN` cannot satisfy acceptance. This avoids using Hyper-V or a developer VM.

Runtime outputs are retained for 14 days as `dx03-candidates-<source SHA>` and `dx03-runtime-evidence-<source SHA>`. The Controller join uploads `dx04-evidence-<source SHA>`, containing the downloaded runtime evidence plus Controller lifecycle checks, process evidence, and redacted stdout/stderr. No live database directory is uploaded. The unchanged Windows CurrentUser Root mutation assertion is a manual interactive release validation under the accepted PR #128 contract; follow [`docs/WINDOWS_CURRENTUSER_ROOT_CA_VALIDATION.md`](../../docs/WINDOWS_CURRENTUSER_ROOT_CA_VALIDATION.md). The Desktop Diagnostic component does not mutate a user's root store; signing/AV certification remains outside this spike.

The native job caches Cargo's build output and downloaded crate sources under a key containing the Windows runner, exact Rust compiler version, and Tauri `Cargo.toml`/`Cargo.lock` hashes. Cargo still evaluates cached artifacts normally, and every run still executes format, clippy with warnings denied, tests, the Tauri build, and lifecycle smoke. A cache miss only costs time; it does not change the required gates. No secrets or credentials belong in the cache.

The coordinator selected `shared` as the M1/DX-04 packaging baseline. Both `shared` and `split` passed exact-SHA hosted non-admin runtime acceptance. The hosted candidate sizes were 241,691,880 bytes for `shared` and 296,367,873 bytes for `split`, so `shared` is 54,675,993 bytes smaller (about 52.1 MiB, or 18.5% of `split`). `shared` uses one PyInstaller onedir and one `threads-runtime.exe` with `http`, `scheduler`, `migrate`, and `version` modes. DX-04 must still run HTTP and scheduler as separate OS processes; sharing a binary does not collapse their lifecycles. `split` remains an evidence-backed fallback and is not selected for M1. This selection applies only to the M1/DX-04 packaging baseline; it does not certify an installer, signing, AV readiness, or DX-12 production distribution. No build implementation changes follow from this selection.
