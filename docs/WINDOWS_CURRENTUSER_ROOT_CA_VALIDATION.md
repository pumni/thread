# Windows CurrentUser Root CA validation gate

**Status: NOT VERIFIED.** Issue [#123](https://github.com/pumni/thread/issues/123) remains open. Windows package smoke and CA file/directory tests do not satisfy this gate.

## Why this is a separate gate

The Worker Control Plane client uses HTTPX2 with system trust and normal hostname and certificate validation. The missing evidence is a real HTTPS handshake when a synthetic CA exists only in the Windows `CurrentUser\Root` store.

The bounded probe in PR #128 timed out while adding the generated CA on both a local Windows environment and a GitHub-hosted `windows-latest` runner. On the hosted Windows Server 2025 runner, CurrentUser and LocalMachine root-store enumeration each completed in about 0.2 seconds with 571 certificates. The runner reported `elevated=True interactive=True`, yet the add call did not return within 15 seconds and was terminated at `add_current_user_root_start`; cleanup confirmed the generated CA was absent and the captured existing roots remained. The hosted attempt is recorded in [PR #128's Windows CurrentUser root job](https://github.com/pumni/thread/actions/runs/36922619408/job/110572115791). The earlier PowerShell attempt has no retained script or linked log in the issue/PR history. Available evidence does not identify whether a store provider, UI/session requirement, or another wait caused either hang. Do not describe this as a proven permission or prompt issue.

Microsoft documents a Security Warning with a user confirmation when an untrusted self-signed CA is first installed into Current User Trusted Root in its Visual Studio/IIS Express scenario. That makes a hidden confirmation a testable hypothesis, not an established cause for this Worker test. See [Microsoft's untrusted-certificate warning procedure](https://learn.microsoft.com/en-us/troubleshoot/developer/visualstudio/installation/warnings-untrusted-certificate).

## Release validation procedure

Run both phases on a dedicated, disposable Windows profile with a visible interactive desktop and a CurrentUser root store that can safely be changed. Do not use production credentials, a production CA, the LocalMachine root store, or CA environment overrides. Reset or discard the profile after the run even when test cleanup succeeds.

At the exact candidate commit, prepare a one-use CA fixture outside the repository:

```powershell
uv sync --locked
uv run --locked python tests/unit/prepare_windows_currentuser_root_ca.py
```

The script prints the fixture directory and certificate path. In the same visible PowerShell desktop, set the directory to the printed path and run the pre-import check:

```powershell
$fixtureDir = 'paste the printed fixture directory here'
if (-not (Test-Path (Join-Path $fixtureDir 'synthetic-worker-control-root.cer'))) {
  throw 'The prepared synthetic CA certificate was not found.'
}
if ((Split-Path -Leaf $fixtureDir) -notlike 'thread-currentuser-root-ca-*') {
  throw 'The fixture directory name is not the generated temporary directory.'
}
$env:THREADS_WINDOWS_ROOT_CA_FIXTURE_DIR = $fixtureDir
$testTemp = Join-Path $env:TEMP ("thread-currentuser-root-ca-tests-" + [guid]::NewGuid().ToString("N"))
uv run --locked pytest -s -q --tb=short `
  --basetemp $testTemp `
  tests/unit/test_worker_control_client_httpx2.py `
  -k rejects_untrusted_windows_current_user_root_ca_before_import
if ($LASTEXITCODE -ne 0) { throw 'The pre-import HTTPS rejection check failed.' }
```

The pre-import check must pass before installing the CA. Then, in the same visible PowerShell desktop, use the supported cmdlet:

```powershell
$installedCa = Import-Certificate `
  -FilePath (Join-Path $fixtureDir 'synthetic-worker-control-root.cer') `
  -CertStoreLocation Cert:\CurrentUser\Root
if ($installedCa.Subject -ne 'CN=worker-control-test-ca') {
  throw 'The imported certificate is not the synthetic test CA.'
}
```

Record whether the Windows Security Warning appears. If it does, verify it identifies the synthetic `worker-control-test-ca`, then confirm only that generated CA on this disposable profile. Do not use `-Confirm:$false`, force flags, or a hidden process for this step.

If `Import-Certificate` does not return, record the visible UI and last operation; do not run the handshake test until the CA's store state is checked.

After import returns, run the separate handshake-only test in the same profile. It never imports the CA; it requires the exact prepared CA already to exist only in `CurrentUser\Root`:

```powershell
uv run --locked pytest -s -q --tb=short --basetemp $testTemp `
  tests/unit/test_worker_control_client_httpx2.py `
  -k preimported_windows_current_user_root_ca
$pytestExitCode = $LASTEXITCODE
$env:THREADS_WINDOWS_ROOT_CA_FIXTURE_DIR = $null
if ($pytestExitCode -ne 0) { throw 'The CurrentUser root HTTPS checks failed.' }
Remove-Item -LiteralPath $testTemp -Recurse -Force
```

This test signs a local HTTPS server with the same fixture key and verifies through `HttpWorkerControlClient` that:

1. The CA is present in CurrentUser and absent from LocalMachine; root snapshots from before import are preserved.
2. HTTPS succeeds without `SSL_CERT_FILE` or `SSL_CERT_DIR` overrides.
3. Wrong-hostname and expired leaves fail while that CA is trusted.
4. The CA is removed by its generated thumbprint, existing roots remain, and the HTTPS request fails again.

On successful cleanup the test removes the fixture key and certificate files. Never commit or attach the private key. If the test fails, capture diagnostics and check whether the synthetic CA remains in CurrentUser Root. If it remains, remove only that certificate by its captured thumbprint:

```powershell
$installedCaPath = "Cert:\CurrentUser\Root\$($installedCa.Thumbprint)"
if (Test-Path -LiteralPath $installedCaPath) {
  Remove-Item -LiteralPath $installedCaPath -ErrorAction Stop
}
```

Then confirm it is absent, remove the printed fixture directory and `$testTemp`, and discard the profile.

The store helper uses Windows CryptoAPI with an explicit CurrentUser or LocalMachine scope. Each helper process has a 15-second timeout. The tests log only operation names, elapsed time, exit status and sanitized WinCrypt error codes; they do not log the certificate, thumbprint, private key or request credentials.

## Evidence required to close the gate

Attach the exact commit and Windows runner/OS context, whether the Security Warning appeared, the test result, operation timing/status lines, and the workflow or local validation log. Confirm the positive handshake and both fail-closed states, and confirm test cleanup restored the captured store state. A timeout, skipped test, SSL context inspection, CA file/directory override, or package smoke does not pass this gate. If the supported import still blocks on an accessible desktop, retain sanitized diagnostics and keep #123 open for the confirmation/store-write investigation; do not extend the timeout or route trust through a file override to claim success.

The Win32 APIs used by the probe document system-store location flags and adding/removing certificate contexts: [CertOpenStore](https://learn.microsoft.com/en-us/windows/win32/api/wincrypt/nf-wincrypt-certopenstore), [CertAddEncodedCertificateToStore](https://learn.microsoft.com/en-us/windows/win32/api/wincrypt/nf-wincrypt-certaddencodedcertificatetostore), and [CertDeleteCertificateFromStore](https://learn.microsoft.com/en-us/windows/win32/api/wincrypt/nf-wincrypt-certdeletecertificatefromstore).
