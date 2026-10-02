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

`Desktop CI` provides the clean-machine acceptance run on a fresh GitHub-hosted `windows-2025` x64 runner. It checks out the exact workflow SHA, builds both candidates, records whether build tools are visible on the runner, copies the candidates to a temporary staging root, and verifies their complete tree hashes again. If the Actions process is an administrator, the smoke runs under a newly created standard local user; otherwise it uses the existing non-administrator identity. The smoke child receives only the bundled PostgreSQL `bin` directory and `Windows\System32` in `PATH`; the Windows root is excluded because it can contain `py.exe`. Inherited Python, uv, PostgreSQL, Docker, and Desktop database variables are cleared before any packaged runtime command. Evidence records each of `python.exe`, `python3.exe`, `py.exe`, `uv.exe`, `docker.exe`, and `pg_ctl.exe` as resolved/unresolved and includes an absolute resolved path when present. Python, python3, py and uv must not resolve; Docker is informational even if the hosted image exposes it from System32; `pg_ctl.exe` must resolve from the candidate bundle. Docker is not a runtime prerequisite. It also records the Windows image/version, effective privilege, sanitized `PATH`, manifest/checksum, migration, loopback, HTTP, scheduler, persistence, DPAPI, ACL, and log-scrub results. Log scrub is `PASS` only when runtime logs exist and verification passes, `FAIL` when created logs fail verification, and `NOT_RUN` when failure precedes runtime log creation; `NOT_RUN` cannot satisfy acceptance. This avoids using Hyper-V or a developer VM.

Desktop CI uploads `dx03-candidates-<source SHA>` separately from the compact `dx03-evidence-<source SHA>` artifact. The evidence artifact records the hosted preflight, candidate verification, independent shared/split smoke JSON, each child PowerShell exit code, sanitized stdout/stderr, and final verification JSON even when either smoke fails. This preserves the first runtime failure code separately from log-scrub status. Both artifacts are retained for 14 days. The unchanged Windows CurrentUser Root mutation assertion is a manual interactive release validation under the accepted PR #128 contract; follow [`docs/WINDOWS_CURRENTUSER_ROOT_CA_VALIDATION.md`](../../docs/WINDOWS_CURRENTUSER_ROOT_CA_VALIDATION.md). Desktop CI does not mutate a user's root store or itself select a packaging layout. Its evidence informed the coordinator's selection below; signing/AV certification remains outside this spike.

The coordinator selected `shared` as the M1/DX-04 packaging baseline. Both `shared` and `split` passed exact-SHA hosted non-admin runtime acceptance. The hosted candidate sizes were 241,691,880 bytes for `shared` and 296,367,873 bytes for `split`, so `shared` is 54,675,993 bytes smaller (about 52.1 MiB, or 18.5% of `split`). `shared` uses one PyInstaller onedir and one `threads-runtime.exe` with `http`, `scheduler`, `migrate`, and `version` modes. DX-04 must still run HTTP and scheduler as separate OS processes; sharing a binary does not collapse their lifecycles. `split` remains an evidence-backed fallback and is not selected for M1. This selection applies only to the M1/DX-04 packaging baseline; it does not certify an installer, signing, AV readiness, or DX-12 production distribution. No build implementation changes follow from this selection.
