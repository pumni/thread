[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$RepositoryRoot,
    [Parameter(Mandatory = $true)]
    [string]$ExpectedSourceRevision,
    [Parameter(Mandatory = $true)]
    [string]$EvidenceOutput
)

$ErrorActionPreference = "Stop"
$RepositoryRoot = (Resolve-Path -LiteralPath $RepositoryRoot).Path
$actualSourceRevision = (& git -C $RepositoryRoot rev-parse HEAD).Trim()
if (
    $env:GITHUB_ACTIONS -ne "true" -or
    $env:RUNNER_ENVIRONMENT -ne "github-hosted" -or
    $env:RUNNER_OS -ne "Windows" -or
    $env:RUNNER_ARCH -ne "X64" -or
    $env:GITHUB_SHA -ne $ExpectedSourceRevision -or
    $actualSourceRevision -ne $ExpectedSourceRevision -or
    $ExpectedSourceRevision -notmatch "^[0-9a-f]{40}$" -or
    -not [Environment]::Is64BitOperatingSystem
) {
    throw "hosted_smoke_requires_exact_sha_github_hosted_windows_x64_runner"
}
$candidateRoot = Join-Path $RepositoryRoot "build\windows-desktop\candidates"
$smokeScript = Join-Path $RepositoryRoot "packaging\windows_desktop\smoke_runtime.ps1"
$verifierScript = Join-Path $RepositoryRoot "packaging\windows_desktop\verify_runtime_evidence.py"
$lockPath = Join-Path $RepositoryRoot "uv.lock"
$downloadManifestPath = Join-Path $RepositoryRoot "packaging\windows_desktop\download-manifest.json"
$postgresArchivePath = Join-Path $RepositoryRoot "build\downloads\postgresql-17.11-4-windows-x64-binaries.zip"
$EvidenceOutput = [System.IO.Path]::GetFullPath($EvidenceOutput)
$runId = [guid]::NewGuid().ToString("N")
$parentIdentity = [System.Security.Principal.WindowsIdentity]::GetCurrent()
$parentPrincipal = [System.Security.Principal.WindowsPrincipal]::new($parentIdentity)
$parentIsAdministrator = $parentPrincipal.IsInRole([System.Security.Principal.WindowsBuiltInRole]::Administrator)
$smokeCredential = $null
$smokeUserName = $null
$stageRoot = if ($parentIsAdministrator) {
    Join-Path $env:SystemDrive "ThreadsDx03HostedSmoke-$runId"
} else {
    Join-Path $env:TEMP "ThreadsDx03HostedSmoke-$runId"
}
$stageCandidates = Join-Path $stageRoot "candidates"
$stageEvidence = Join-Path $stageRoot "evidence"
$stageSmokeScript = Join-Path $stageRoot "smoke_runtime.ps1"
$failures = [System.Collections.Generic.List[string]]::new()
$icacls = Join-Path $env:SystemRoot "System32\icacls.exe"

function Set-HostedSmokeAcl([string]$Path, [string]$UserPrincipal) {
    & $icacls $Path /inheritance:r /grant:r `
        "${UserPrincipal}:(OI)(CI)F" `
        "BUILTIN\Administrators:(OI)(CI)F" `
        "NT AUTHORITY\SYSTEM:(OI)(CI)F" | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "hosted_smoke_acl_setup_failed" }
}

function Invoke-SmokeProcess([string]$BundleRoot, [string]$SmokeEvidencePath) {
    $layoutName = [IO.Path]::GetFileName($BundleRoot)
    $stdoutPath = Join-Path $stageRoot "$layoutName-smoke.stdout.internal.log"
    $stderrPath = Join-Path $stageRoot "$layoutName-smoke.stderr.internal.log"
    $arguments = @(
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        $stageSmokeScript,
        "-BundleRoot",
        $BundleRoot,
        "-EvidencePath",
        $SmokeEvidencePath,
        "-ExpectedSourceRevision",
        $ExpectedSourceRevision,
        "-RunKind",
        "github_hosted_windows_x64_isolated",
        "-CleanWindowsEvidence"
    )
    $processArgs = @{
        FilePath = (Join-Path $PSHOME "pwsh.exe")
        ArgumentList = $arguments
        PassThru = $true
        Wait = $true
        WindowStyle = "Hidden"
        RedirectStandardOutput = $stdoutPath
        RedirectStandardError = $stderrPath
    }
    if ($smokeCredential) {
        $processArgs.Credential = $smokeCredential
        $processArgs.LoadUserProfile = $true
    }
    $process = Start-Process @processArgs
    Write-Output "HOSTED_SMOKE_CHILD layout=$([IO.Path]::GetFileName($BundleRoot)) exit_code=$($process.ExitCode)"
    if ($process.ExitCode -ne 0) { return $false }
    if (-not (Test-Path -LiteralPath $SmokeEvidencePath)) { return $false }
    $evidence = Get-Content -LiteralPath $SmokeEvidencePath -Raw | ConvertFrom-Json
    return ($evidence.status -eq "PASS" -and $evidence.clean_windows_runner_status -eq "PASS")
}

try {
    New-Item -ItemType Directory -Path $EvidenceOutput -Force | Out-Null
    New-Item -ItemType Directory -Path $stageRoot -Force | Out-Null
    if ($parentIsAdministrator) {
        if (-not (Get-Command -Name New-LocalUser -ErrorAction SilentlyContinue)) {
            throw "hosted_runner_cannot_create_non_admin_smoke_user"
        }
        $smokeUserName = "dx03_$($runId.Substring(0, 10))"
        $passwordBytes = [byte[]]::new(32)
        $randomGenerator = [System.Security.Cryptography.RandomNumberGenerator]::Create()
        try { $randomGenerator.GetBytes($passwordBytes) }
        finally { $randomGenerator.Dispose() }
        $passwordText = [Convert]::ToBase64String($passwordBytes) + "aA7!"
        $securePassword = ConvertTo-SecureString -String $passwordText -AsPlainText -Force
        [Array]::Clear($passwordBytes, 0, $passwordBytes.Length)
        $passwordText = $null
        New-LocalUser -Name $smokeUserName -Password $securePassword `
            -AccountNeverExpires -PasswordNeverExpires -UserMayNotChangePassword | Out-Null
        $smokeCredential = [System.Management.Automation.PSCredential]::new(
            "$env:COMPUTERNAME\$smokeUserName",
            $securePassword
        )
        $securePassword = $null
        Set-HostedSmokeAcl $stageRoot "$env:COMPUTERNAME\$smokeUserName"
    }

    New-Item -ItemType Directory -Path $stageCandidates -Force | Out-Null
    New-Item -ItemType Directory -Path $stageEvidence -Force | Out-Null
    foreach ($layout in @("shared", "split")) {
        $source = Join-Path $candidateRoot $layout
        if (-not (Test-Path -LiteralPath $source)) { throw "hosted_candidate_missing_$layout" }
        Copy-Item -LiteralPath $source -Destination (Join-Path $stageCandidates $layout) -Recurse
    }
    Copy-Item -LiteralPath $smokeScript -Destination $stageSmokeScript

    $preflightPath = Join-Path $EvidenceOutput "hosted-runner-preflight.json"
    $preflight = [ordered]@{
        schema_version = 1
        source_revision = $ExpectedSourceRevision
        github_actions = $env:GITHUB_ACTIONS -eq "true"
        runner_environment = $env:RUNNER_ENVIRONMENT
        runner_os = $env:RUNNER_OS
        runner_arch = $env:RUNNER_ARCH
        runner_image = $env:ImageOS
        runner_image_version = $env:ImageVersion
        workflow_runner_label = "windows-2025"
        windows_version = [Environment]::OSVersion.Version.ToString()
        architecture_x64 = [Environment]::Is64BitOperatingSystem
        runner_process_is_administrator = $parentIsAdministrator
        runner_ambient_path_prerequisites = [ordered]@{
            python = $null -ne (Get-Command -Name python.exe -CommandType Application -ErrorAction SilentlyContinue) -or
                $null -ne (Get-Command -Name python3.exe -CommandType Application -ErrorAction SilentlyContinue)
            python_launcher = $null -ne (Get-Command -Name py.exe -CommandType Application -ErrorAction SilentlyContinue)
            uv = $null -ne (Get-Command -Name uv.exe -CommandType Application -ErrorAction SilentlyContinue)
            docker = $null -ne (Get-Command -Name docker.exe -CommandType Application -ErrorAction SilentlyContinue)
            postgresql = $null -ne (Get-Command -Name pg_ctl.exe -CommandType Application -ErrorAction SilentlyContinue)
        }
        smoke_process = "non-administrator user; PATH restricted to bundled PostgreSQL and Windows system binaries"
        source_revision_is_manifest_checked = $true
    }
    [System.IO.File]::WriteAllText(
        $preflightPath,
        ($preflight | ConvertTo-Json -Depth 8),
        [System.Text.UTF8Encoding]::new($false)
    )

    $archiveArgs = @(
        "--candidate-root", $stageCandidates,
        "--expected-source-revision", $ExpectedSourceRevision,
        "--uv-lock", $lockPath,
        "--download-manifest", $downloadManifestPath,
        "--postgres-archive", $postgresArchivePath,
        "--result-path", (Join-Path $EvidenceOutput "staged-candidate-verification.json")
    )
    & uv run --locked --no-dev --group packaging python $verifierScript @archiveArgs
    if ($LASTEXITCODE -ne 0) { throw "hosted_staged_candidate_checksum_failed" }

    foreach ($layout in @("shared", "split")) {
        $layoutEvidence = Join-Path $stageEvidence $layout
        New-Item -ItemType Directory -Path $layoutEvidence -Force | Out-Null
        $bundleRoot = Join-Path $stageCandidates $layout
        $smokeEvidencePath = Join-Path $layoutEvidence "smoke.json"
        $smokePassed = Invoke-SmokeProcess $bundleRoot $smokeEvidencePath
        $outputLayout = Join-Path $EvidenceOutput $layout
        New-Item -ItemType Directory -Path $outputLayout -Force | Out-Null
        if (Test-Path -LiteralPath $layoutEvidence) {
            Get-ChildItem -LiteralPath $layoutEvidence -File | ForEach-Object {
                Copy-Item -LiteralPath $_.FullName -Destination $outputLayout -Force
            }
        }
        if (-not $smokePassed) { $failures.Add("$layout runtime smoke failed") }
    }

    $fullVerificationArgs = @(
        "--candidate-root", $candidateRoot,
        "--expected-source-revision", $ExpectedSourceRevision,
        "--uv-lock", $lockPath,
        "--download-manifest", $downloadManifestPath,
        "--postgres-archive", $postgresArchivePath,
        "--smoke-evidence-root", $EvidenceOutput,
        "--result-path", (Join-Path $EvidenceOutput "runtime-evidence-verification.json")
    )
    & uv run --locked --no-dev --group packaging python $verifierScript @fullVerificationArgs
    if ($LASTEXITCODE -ne 0) { $failures.Add("hosted runtime evidence verification failed") }
} catch {
    $failures.Add(("hosted runtime setup failed: " + $_.Exception.Message))
} finally {
    if ($smokeUserName) {
        Remove-LocalUser -Name $smokeUserName -ErrorAction SilentlyContinue
    }
    if (Test-Path -LiteralPath $stageRoot) {
        Remove-Item -LiteralPath $stageRoot -Recurse -Force -ErrorAction SilentlyContinue
    }
}

if ($failures.Count -gt 0) {
    $failures | ForEach-Object { Write-Error $_ }
    exit 1
}

Write-Output "DX-03 hosted Windows x64 non-admin smoke and evidence verification PASS for $ExpectedSourceRevision"
