# Windows CurrentUser Root CA validation gate

**Status: NOT VERIFIED.** Issue [#123](https://github.com/pumni/thread/issues/123) remains open. Windows package smoke and CA file/directory tests do not satisfy this gate.

## Why this is a separate gate

The Worker Control Plane client uses HTTPX2 with system trust and normal hostname and certificate validation. The missing evidence is a real HTTPS handshake when a synthetic CA exists only in the Windows `CurrentUser\Root` store.

The bounded probe in PR #128 timed out while adding the generated CA on both a local Windows environment and a GitHub-hosted `windows-latest` runner. On the hosted Windows Server 2025 runner, CurrentUser and LocalMachine root-store enumeration each completed in about 0.2 seconds with 571 certificates. The runner reported `elevated=True interactive=True`, yet the add call did not return within 15 seconds and was terminated at `add_current_user_root_start`; cleanup confirmed the generated CA was absent and the captured existing roots remained. The hosted attempt is recorded in [PR #128's Windows CurrentUser root job](https://github.com/pumni/thread/actions/runs/36922619408/job/110572115791). The earlier PowerShell attempt has no retained script or linked log in the issue/PR history. Available evidence does not identify whether a store provider, UI/session requirement, or another wait caused either hang. Do not describe this as a proven permission or prompt issue.

## Release validation procedure

Run the existing end-to-end test on a dedicated, disposable Windows validation account/profile whose CurrentUser root store can be safely changed. Do not use production credentials, a production CA, the LocalMachine root store, or CA environment overrides. Reset or discard the runner profile after the run even when test cleanup succeeds.

At the exact candidate commit, run:

```powershell
uv sync --locked
uv run --locked pytest -s -q --tb=short `
  tests/unit/test_worker_control_client_httpx2.py `
  -k windows_current_user_root_ca
```

The test itself generates a nonproduction CA and local HTTPS server. It clears `SSL_CERT_FILE` and `SSL_CERT_DIR`, sets controlled proxy variables and `NO_PROXY`, then checks all of the following through `HttpWorkerControlClient`:

1. The request fails before the CA is installed.
2. The request succeeds while the CA is present in `CurrentUser\Root` and absent from `LocalMachine\Root`.
3. Wrong-hostname and expired leaves fail while that CA is trusted.
4. The CA is removed by its generated thumbprint, existing user roots remain present, and the request fails again.

The store helper uses Windows CryptoAPI with an explicit CurrentUser or LocalMachine scope. Each helper process has a 15-second timeout. The test logs only operation names, elapsed time, exit status and sanitized WinCrypt error codes; it does not log the certificate, thumbprint, private key or request credentials.

## Evidence required to close the gate

Attach the exact commit and Windows runner/OS context, the test result, operation timing/status lines, and the workflow or local validation log. Confirm the positive handshake and both fail-closed states, and confirm test cleanup restored the captured store state. A timeout, skipped test, SSL context inspection, CA file/directory override, or package smoke does not pass this gate. If the write still blocks, retain the sanitized last-stage diagnostics and keep #123 open for runner/policy investigation; do not extend the timeout or route trust through a file override to claim success.

The Win32 APIs used by the probe document system-store location flags and adding/removing certificate contexts: [CertOpenStore](https://learn.microsoft.com/en-us/windows/win32/api/wincrypt/nf-wincrypt-certopenstore), [CertAddEncodedCertificateToStore](https://learn.microsoft.com/en-us/windows/win32/api/wincrypt/nf-wincrypt-certaddencodedcertificatetostore), and [CertDeleteCertificateFromStore](https://learn.microsoft.com/en-us/windows/win32/api/wincrypt/nf-wincrypt-certdeletecertificatefromstore).
