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

`Desktop CI` provides the clean-machine acceptance run on a fresh GitHub-hosted `windows-2025` x64 runner. It checks out the exact workflow SHA, builds both candidates, records whether build tools are visible on the runner, copies the candidates to a temporary staging root, and verifies their complete tree hashes again. If the Actions process is an administrator, the smoke runs under a newly created standard local user; otherwise it uses the existing non-administrator identity. The smoke child receives only the bundled PostgreSQL `bin` directory and Windows system directories in `PATH`, and inherited Python, uv, PostgreSQL, Docker, and Desktop database variables are cleared before any packaged runtime command. It records the Windows image/version, effective privilege, sanitized `PATH`, manifest/checksum, migration, loopback, HTTP, scheduler, persistence, DPAPI, ACL, and log-scrub results. The runtime child cannot resolve a host Python, uv, Docker, or PostgreSQL executable. This avoids using Hyper-V or a developer VM.

The `dx03-windows-x64-<source SHA>` CI artifact includes both built candidates, their runtime manifests and checksums, clean-runner smoke JSON, hashed redacted logs, and the verification result. Artifacts are retained for 14 days. The unchanged Windows CurrentUser Root mutation assertion is a manual interactive release validation under the accepted PR #128 contract; follow [`docs/WINDOWS_CURRENTUSER_ROOT_CA_VALIDATION.md`](../../docs/WINDOWS_CURRENTUSER_ROOT_CA_VALIDATION.md). Desktop CI does not mutate a user's root store. CI does not select a packaging layout or certify signing/AV readiness; those remain DX-03 review inputs.

The two candidates are evidence inputs, not a selected production layout. Select a layout only after both hosted clean-runner smokes pass, license review, signature/AV review, and coordinator acceptance. This spike does not connect the packaged runtime to the Desktop UI or provision production credentials.
