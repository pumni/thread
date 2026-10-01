# Backend dependency health checks

The Ubuntu quality gate and Windows Worker package workflow print the locked
production, development, and packaging dependency graph with
`uv tree --locked --all-groups`. This shows the versions in `uv.lock` for the
applicable platform and asserts the lockfile remains unchanged; it does not
check whether newer versions are available. Run the same command locally when
reviewing the locked graph.

`uv run --locked pytest` treats `DeprecationWarning`,
`PendingDeprecationWarning`, `FutureWarning`, and `PytestDeprecationWarning` as
errors. It also treats Starlette's custom `StarletteDeprecationWarning` as an
error; that warning derives from `UserWarning`, not `DeprecationWarning`. Other
warning categories remain visible in pytest's warning summary. Do not add
warning ignores to make the gate pass; update the affected dependency or code
and retain a focused regression check.

## Audit findings (2026-10-01)

The locked tree contains production, `dev`, and `packaging` groups. The
The Threads API and Worker Control Plane adapters now use `httpx2` at runtime.
The direct `httpx` dependency remains through the separate Slice C review and
cleanup; it is intentionally not removed as part of either transport slice.

Both HTTPX2 clients explicitly keep certificate verification enabled, use the
operating-system trust store by default instead of HTTPX's bundled `certifi`
store, and honor `SSL_CERT_FILE`, `SSL_CERT_DIR`, and the standard proxy
environment variables through `trust_env=True`. The Threads API keeps its
15-second timeout and redirect-disabled behavior; the Worker Control Plane
keeps its 10-second timeout and redirect-disabled behavior. HTTPX2 changes the
default User-Agent from `python-httpx/...` to `python-httpx2/...`; neither API
contract depends on that library-generated value. Slice C owns removal of the
direct `httpx` dependency after the remaining runtime and packaging tree is
reviewed.

The HTTPX2 migration guide describes it as a fork of HTTPX 0.28.1 with a
largely compatible API; its project metadata and release notes include Python
3.14 support. Package classes remain distinct, so each adapter and its
transport fixtures must use only its own package's classes.

Run `uv tree --outdated` separately to check for newer available package
versions. This is release freshness information, not evidence of deprecation,
end of support, or a security vulnerability, so this audit did not upgrade
packages based on freshness alone. No other supported upstream deprecation or
withdrawn locked release was identified in the dependency tree. Security
advisories must be assessed separately from deprecation notices.

The refreshed universal lock contains 68 registry packages across its platform
markers (69 entries including the project); the CI tree reports the applicable
graph on both Ubuntu and Windows. The 2026-10-01 PyPI release-metadata check
covered the previous 57-package lock and found no yanked distributions among
those releases. The current-lock freshness and yanked-release monitoring is
tracked separately in #115. Two Windows-packaging transitives have a slower
release cadence:
`pefile 2024.8.26` and `pywin32-ctypes 0.2.3` were last released in August 2024.
They remain the current upstream releases and PyInstaller dependencies; no
formal end-of-support notice or replacement was found. Treat the release gap
as a watch item, not a deprecation.

The `pip-audit` scan of the Windows-resolved tree and an OSV query covering the
previous 57 locked registry packages reported one moderate advisory,
GHSA-g6cj-pr64-35w5 (`PYSEC-2026-3552`), for `cryptography 49.0.0`. It concerns
PKCS#7 EnvelopedData decryption. The repository's reviewed call sites use
Ed25519 operations and private-key serialization, not PKCS#7 decryption. PR
#117 remediated the finding on `main` on 2026-10-01 by updating the constraint
to `cryptography>=50,<51` and locking `50.0.2`. A current
`uv audit --locked --python-version 3.14` scan of the combined 68-package lock
reports no known vulnerabilities or adverse project statuses. The GHSA is a
historical, remediated finding, not an unresolved current advisory. Ongoing
automated advisory and upstream-health monitoring remains tracked in #115.

Primary references:

- [Starlette TestClient documentation](https://starlette.dev/testclient/)
- [HTTPX2 migration guide](https://pydantic.dev/docs/httpx2/get-started/migration/)
- [HTTPX2 project metadata](https://github.com/pydantic/httpx2/blob/main/src/httpx2/pyproject.toml)
- [HTTPX2 changelog](https://github.com/pydantic/httpx2/blob/main/src/httpx2/CHANGELOG.md)
- [cryptography security advisory](https://github.com/pyca/cryptography/security/advisories/GHSA-g6cj-pr64-35w5)
- [pefile release history](https://pypi.org/project/pefile/)
- [pywin32-ctypes release history](https://pypi.org/project/pywin32-ctypes/)
