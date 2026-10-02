# Windows Desktop runtime packaging spike

This spike compares two PyInstaller onedir candidates that consume the same pinned Python dependency graph and EDB PostgreSQL 17.11 Windows x64 archive:

- `shared`: one `threads-runtime.exe` exposes `http`, `scheduler`, `migrate`, and `version` modes.
- `split`: `threads-http.exe` exposes HTTP and migration modes, while `threads-scheduler.exe` is a separate long-running process.

Both candidates carry the same PostgreSQL `bin`, `lib`, and `share` runtime tree, upstream PostgreSQL license files, generated Python dependency notices, and a manifest with source revision, `uv.lock` hash, archive hash, candidate size, and tree digest. Build output stays under ignored `build/windows-desktop/`; user data and credentials are excluded.

## Build on Windows x64

The build host needs Python 3.14 through uv, the repository's pinned `uv.lock`, PyInstaller 6.22.3 from the `packaging` dependency group, Git, and `curl.exe` (the script falls back to Python's standard library downloader). It downloads the archive in verified HTTP byte ranges and refuses any SHA-256 mismatch.

```powershell
uv sync --locked --all-groups
uv run --locked --no-dev python packaging/windows_desktop/build_runtime.py --layout all
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

The smoke runs without administrator rights, restricts its temporary PostgreSQL data directory to the current Windows user, protects and round-trips a generated database credential using current-user DPAPI, clears developer tool directories from child-process `PATH`, runs migrations twice, checks refusal of an unknown migration revision, starts HTTP and scheduler processes, checks `/health`, and verifies PostgreSQL data survives a stop/start. `/ready` also includes worker fleet readiness, so an empty test cluster correctly reports not ready until a worker is enrolled. Runtime logs included in evidence are redacted and hashed. The smoke deletes its temporary cluster and credential after recording results.

The host used for this run already has Python/uv and Docker installed. There is no separate clean Windows x64 VM available, so local smoke evidence cannot satisfy the clean-VM acceptance criterion. The evidence records this as `clean_windows_vm_status: BLOCKER`; no user-installed runtime fallback is considered acceptable.

The two candidates are evidence inputs, not a selected production layout. Select a layout only after a successful local smoke, clean-VM install/startup evidence, license review, signature/AV review, and coordinator acceptance. This spike does not connect the packaged runtime to the Desktop UI or provision production credentials.
