# Backend dependency health checks

The Ubuntu quality gate and Windows Worker package workflow print the locked
production, development, and packaging dependency tree with
`uv tree --locked --all-groups`. This shows the packages applicable on both
platforms and the lock check prevents silently resolving newer versions. Run
the same command locally when reviewing dependency changes.

`uv run --locked pytest` treats `DeprecationWarning`,
`PendingDeprecationWarning`, `FutureWarning`, and `PytestDeprecationWarning` as
errors. It also treats Starlette's custom `StarletteDeprecationWarning` as an
error; that warning derives from `UserWarning`, not `DeprecationWarning`. Other
warning categories remain visible in pytest's warning summary. Do not add
warning ignores to make the gate pass; update the affected dependency or code
and retain a focused regression check.

## Audit findings (2026-10-01)

The locked tree contains production, `dev`, and `packaging` groups. The only
upstream deprecation found in current use is Starlette's `TestClient` fallback
to `httpx`: Starlette still supports that fallback, but recommends `httpx2`.
`httpx2` is therefore a dev-only dependency for FastAPI/Starlette tests. The
production `httpx` dependency remains because the Threads API and Worker
Control Plane adapters use it. The `httpx2` migration guide describes it as a
fork of HTTPX 0.28.1 with the same public API; its project metadata and release
notes include Python 3.14 support. This does not constitute a runtime-client
migration.

The dependency tree also reports packages for which newer releases are
available. That is release freshness information, not evidence of deprecation,
end of support, or a security vulnerability, so this audit did not upgrade
them. No other supported upstream deprecation or withdrawn locked release was
identified in the dependency tree. Security advisories must be assessed
separately from deprecation notices.

The universal lock contains 57 registry packages across its platform markers;
the CI tree reports the applicable graph on both Ubuntu and Windows. PyPI
release metadata showed no yanked distributions for any of the 57 locked
releases. Two Windows-packaging transitives have a slower release cadence:
`pefile 2024.8.26` and `pywin32-ctypes 0.2.3` were last released in August 2024.
They remain the current upstream releases and PyInstaller dependencies; no
formal end-of-support notice or replacement was found. Treat the release gap
as a watch item, not a deprecation.

The `pip-audit` scan of the Windows-resolved tree and an OSV query covering all
57 locked registry packages report one moderate advisory,
GHSA-g6cj-pr64-35w5 (`PYSEC-2026-3552`), for `cryptography 49.0.0`; the patched
version is 50.0.0. The advisory concerns PKCS#7 EnvelopedData decryption. The
repository currently uses Ed25519 operations and private-key serialization,
not PKCS#7 decryption, but the direct constraint `cryptography<50` blocks the
patched release. Review that version-bound change and its Windows packaging
impact in a separate security follow-up; it is not a deprecation remediation.

Primary references:

- [Starlette TestClient documentation](https://starlette.dev/testclient/)
- [HTTPX2 migration guide](https://pydantic.dev/docs/httpx2/get-started/migration/)
- [HTTPX2 project metadata](https://github.com/pydantic/httpx2/blob/main/src/httpx2/pyproject.toml)
- [HTTPX2 changelog](https://github.com/pydantic/httpx2/blob/main/src/httpx2/CHANGELOG.md)
- [cryptography security advisory](https://github.com/pyca/cryptography/security/advisories/GHSA-g6cj-pr64-35w5)
- [pefile release history](https://pypi.org/project/pefile/)
- [pywin32-ctypes release history](https://pypi.org/project/pywin32-ctypes/)
