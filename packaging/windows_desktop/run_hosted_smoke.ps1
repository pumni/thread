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
$verificationJsonPath = Join-Path $EvidenceOutput "runtime-evidence-verification.json"

function ConvertTo-SafeChildOutput([string]$Content) {
    $Content = [regex]::Replace(
        $Content,
        '(?i)(password|database_url)\s*[:=]\s*(?!<REDACTED>)[^\s]+',
        '$1=<REDACTED>'
    )
    $Content = [regex]::Replace(
        $Content,
        '(?i)(authorization\s*[:=]\s*Bearer\s+)(?!<REDACTED>)[^\s]+',
        '$1<REDACTED>'
    )
    $Content = [regex]::Replace(
        $Content,
        '(?i)\bBearer\s+(?!<REDACTED>)[A-Za-z0-9._~+/-]{24,}',
        'Bearer <REDACTED>'
    )
    return [regex]::Replace(
        $Content,
        '(?i)(postgres(?:ql)?(?:\+\w+)?://)[^\s:@/]+:[^@\s/]+@',
        '$1<REDACTED>@'
    )
}

function Get-DiagnosticFileMetadata([string]$Path, [string]$RelativeName) {
    return [ordered]@{
        name = $RelativeName
        size_bytes = (Get-Item -LiteralPath $Path).Length
        sha256 = (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
    }
}

function Set-HostedSmokeAcl([string]$Path, [string]$UserPrincipal) {
    & $icacls $Path /inheritance:r /grant:r `
        "${UserPrincipal}:(OI)(CI)F" `
        "BUILTIN\Administrators:(OI)(CI)F" `
        "NT AUTHORITY\SYSTEM:(OI)(CI)F" | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "hosted_smoke_acl_setup_failed" }
}

function Invoke-SmokeProcess([string]$BundleRoot, [string]$SmokeEvidencePath, [string]$OutputLayout) {
    $layoutName = [IO.Path]::GetFileName($BundleRoot)
    $stdoutPath = Join-Path $stageRoot "$layoutName-smoke.stdout.internal.log"
    $stderrPath = Join-Path $stageRoot "$layoutName-smoke.stderr.internal.log"
    $diagnosticDirectory = Join-Path $OutputLayout "hosted-smoke-process"
    $stdoutEvidencePath = Join-Path $diagnosticDirectory "stdout.redacted.log"
    $stderrEvidencePath = Join-Path $diagnosticDirectory "stderr.redacted.log"
    $processEvidencePath = Join-Path $OutputLayout "smoke-process.json"
    $childExitCode = $null
    $spawnFailureCode = $null
    $smokeStatus = $null
    $primaryFailureCode = $null
    $logScrubStatus = $null
    $smokeJsonCopied = $false
    $smokePassed = $false
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
    New-Item -ItemType Directory -Path $diagnosticDirectory -Force | Out-Null
    try {
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
        $process.Refresh()
        if ($process.HasExited) { $childExitCode = [int]$process.ExitCode }
        $process.Dispose()

        if (Test-Path -LiteralPath $SmokeEvidencePath) {
            Copy-Item -LiteralPath $SmokeEvidencePath -Destination (Join-Path $OutputLayout "smoke.json") -Force
            $smokeJsonCopied = $true
            $evidence = Get-Content -LiteralPath $SmokeEvidencePath -Raw | ConvertFrom-Json
            $smokeStatus = [string]$evidence.status
            $primaryFailureCode = [string]$evidence.primary_failure_code
            $logScrubStatus = [string]$evidence.log_scrub_status
            $smokePassed = $childExitCode -eq 0 -and
                $evidence.status -eq "PASS" -and
                $evidence.clean_windows_runner_status -eq "PASS" -and
                $evidence.log_scrub_status -eq "PASS" -and
                [string]::IsNullOrEmpty([string]$evidence.primary_failure_code)
        }
    } catch {
        $safeFailure = ConvertTo-SafeChildOutput ([string]$_.Exception.Message)
        $spawnFailureCode = [regex]::Replace($safeFailure, "[^A-Za-z0-9_.-]", "_")
    } finally {
        foreach ($stream in @(
            @{ raw = $stdoutPath; safe = $stdoutEvidencePath },
            @{ raw = $stderrPath; safe = $stderrEvidencePath }
        )) {
            $rawText = if (Test-Path -LiteralPath $stream.raw) {
                [System.IO.File]::ReadAllText($stream.raw)
            } else { "" }
            $safeText = ConvertTo-SafeChildOutput $rawText
            [System.IO.File]::WriteAllText(
                $stream.safe,
                $safeText,
                [System.Text.UTF8Encoding]::new($false)
            )
        }
        $diagnosticRecords = @(
            Get-DiagnosticFileMetadata $stdoutEvidencePath "hosted-smoke-process/stdout.redacted.log"
            Get-DiagnosticFileMetadata $stderrEvidencePath "hosted-smoke-process/stderr.redacted.log"
        )
        $processEvidence = [ordered]@{
            schema_version = 1
            source_revision = $ExpectedSourceRevision
            layout = $layoutName
            child_exit_code = $childExitCode
            spawn_failure_code = $spawnFailureCode
            smoke_json_copied = $smokeJsonCopied
            smoke_status = $smokeStatus
            primary_failure_code = $primaryFailureCode
            log_scrub_status = $logScrubStatus
            status = if ($smokePassed) { "PASS" } else { "BLOCKER" }
            sanitized_diagnostics = $diagnosticRecords
        }
        [System.IO.File]::WriteAllText(
            $processEvidencePath,
            ($processEvidence | ConvertTo-Json -Depth 6),
            [System.Text.UTF8Encoding]::new($false)
        )
    }
    return [bool]$smokePassed
}

try {
    New-Item -ItemType Directory -Path $EvidenceOutput -Force | Out-Null
    New-Item -ItemType Directory -Path $stageRoot -Force | Out-Null
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
        try {
            $source = Join-Path $candidateRoot $layout
            if (-not (Test-Path -LiteralPath $source)) { throw "hosted_candidate_missing_$layout" }
            Copy-Item -LiteralPath $source -Destination (Join-Path $stageCandidates $layout) -Recurse
        } catch {
            $failures.Add("$layout candidate staging failed")
        }
    }
    Copy-Item -LiteralPath $smokeScript -Destination $stageSmokeScript

    $archiveArgs = @(
        "--candidate-root", $stageCandidates,
        "--expected-source-revision", $ExpectedSourceRevision,
        "--uv-lock", $lockPath,
        "--download-manifest", $downloadManifestPath,
        "--postgres-archive", $postgresArchivePath,
        "--result-path", (Join-Path $EvidenceOutput "staged-candidate-verification.json")
    )
    & uv run --locked --no-dev --group packaging python $verifierScript @archiveArgs
    if ($LASTEXITCODE -ne 0) { $failures.Add("hosted staged candidate checksum verification failed") }

    foreach ($layout in @("shared", "split")) {
        $layoutEvidence = Join-Path $stageEvidence $layout
        $bundleRoot = Join-Path $stageCandidates $layout
        $smokeEvidencePath = Join-Path $layoutEvidence "smoke.json"
        $outputLayout = Join-Path $EvidenceOutput $layout
        $smokePassed = $false
        try {
            New-Item -ItemType Directory -Path $layoutEvidence -Force | Out-Null
            New-Item -ItemType Directory -Path $outputLayout -Force | Out-Null
            if (-not (Test-Path -LiteralPath $bundleRoot)) { throw "hosted_candidate_missing_$layout" }
            $smokePassed = Invoke-SmokeProcess $bundleRoot $smokeEvidencePath $outputLayout
        } catch {
            $safeFailure = ConvertTo-SafeChildOutput ([string]$_.Exception.Message)
            $failures.Add("$layout runtime smoke harness failed: $([regex]::Replace($safeFailure, '[^A-Za-z0-9_.-]', '_'))")
        }
        try {
            if (Test-Path -LiteralPath $layoutEvidence) {
                Get-ChildItem -LiteralPath $layoutEvidence -File | ForEach-Object {
                    Copy-Item -LiteralPath $_.FullName -Destination $outputLayout -Force
                }
            }
        } catch {
            $failures.Add("$layout smoke evidence copy failed")
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
        "--result-path", $verificationJsonPath
    )
    & uv run --locked --no-dev --group packaging python $verifierScript @fullVerificationArgs
    if ($LASTEXITCODE -ne 0) { $failures.Add("hosted runtime evidence verification failed") }
} catch {
    $safeFailure = ConvertTo-SafeChildOutput ([string]$_.Exception.Message)
    $failures.Add(("hosted runtime setup failed: " + [regex]::Replace($safeFailure, "[^A-Za-z0-9_.-]", "_")))
} finally {
    try {
        if (-not (Test-Path -LiteralPath $verificationJsonPath)) {
            $fallbackVerification = [ordered]@{
                schema_version = 1
                status = "BLOCKER"
                source_revision = $ExpectedSourceRevision
                failures = @($failures)
            }
            [System.IO.File]::WriteAllText(
                $verificationJsonPath,
                ($fallbackVerification | ConvertTo-Json -Depth 5),
                [System.Text.UTF8Encoding]::new($false)
            )
        }
    } catch {
        $failures.Add("runtime verification evidence could not be written")
    }
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
