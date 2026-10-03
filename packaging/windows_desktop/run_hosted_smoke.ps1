[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$RepositoryRoot,
    [Parameter(Mandatory = $true)]
    [string]$ExpectedSourceRevision,
    [Parameter(Mandatory = $true)]
    [string]$EvidenceOutput,
    [Parameter()]
    [ValidateSet("shared", "split")]
    [string]$RuntimeLayout = "shared",
    [Parameter()]
    [switch]$ControllerOnly,
    [Parameter()]
    [string]$ControllerDesktopExecutable,
    [Parameter()]
    [string]$ControllerEvidenceOutput
)

$ErrorActionPreference = "Stop"
$RepositoryRoot = (Resolve-Path -LiteralPath $RepositoryRoot).Path
$actualSourceRevision = (& git -C $RepositoryRoot rev-parse HEAD).Trim()
$headExitCode = $LASTEXITCODE
$sourceStatus = @(& git -C $RepositoryRoot status --porcelain)
$sourceStatusExitCode = $LASTEXITCODE
$controllerSmokeRequested = -not [string]::IsNullOrWhiteSpace($ControllerDesktopExecutable)
$controllerEvidenceRequested = -not [string]::IsNullOrWhiteSpace($ControllerEvidenceOutput)
$controllerArgumentsValid = $controllerSmokeRequested -eq $controllerEvidenceRequested -and
    (-not $controllerSmokeRequested -or $RuntimeLayout -eq "shared") -and
    (-not $ControllerOnly -or $controllerSmokeRequested)
if (
    $env:GITHUB_ACTIONS -ne "true" -or
    $env:RUNNER_ENVIRONMENT -ne "github-hosted" -or
    $env:RUNNER_OS -ne "Windows" -or
    $env:RUNNER_ARCH -ne "X64" -or
    $env:DESKTOP_SOURCE_SHA -ne $ExpectedSourceRevision -or
    $actualSourceRevision -ne $ExpectedSourceRevision -or
    $ExpectedSourceRevision -notmatch "^[0-9a-f]{40}$" -or
    $headExitCode -ne 0 -or
    $sourceStatusExitCode -ne 0 -or
    $sourceStatus.Count -ne 0 -or
    -not $controllerArgumentsValid -or
    -not [Environment]::Is64BitOperatingSystem
) {
    throw "hosted_smoke_requires_exact_sha_github_hosted_windows_x64_runner"
}
$candidateRoot = Join-Path $RepositoryRoot "build\windows-desktop\candidates"
$smokeScript = Join-Path $RepositoryRoot "packaging\windows_desktop\smoke_runtime.ps1"
$verifierScript = Join-Path $RepositoryRoot "packaging\windows_desktop\verify_runtime_evidence.py"
$lockPath = Join-Path $RepositoryRoot "uv.lock"
$controllerSmokeScript = Join-Path $RepositoryRoot "packaging\windows_desktop\smoke_controller_lifecycle.ps1"
$downloadManifestPath = Join-Path $RepositoryRoot "packaging\windows_desktop\download-manifest.json"
$postgresArchivePath = Join-Path $RepositoryRoot "build\downloads\postgresql-17.11-4-windows-x64-binaries.zip"
$EvidenceOutput = [System.IO.Path]::GetFullPath($EvidenceOutput)
if ($ControllerEvidenceOutput) {
    $ControllerEvidenceOutput = [System.IO.Path]::GetFullPath($ControllerEvidenceOutput)
}
$runId = [guid]::NewGuid().ToString("N")
$parentIdentity = [System.Security.Principal.WindowsIdentity]::GetCurrent()
$parentPrincipal = [System.Security.Principal.WindowsPrincipal]::new($parentIdentity)
$parentIsAdministrator = $parentPrincipal.IsInRole([System.Security.Principal.WindowsBuiltInRole]::Administrator)
$smokeCredential = $null
$smokeUserName = $null
$smokeProfileRoot = [string]$env:USERPROFILE
$parentAdminProfilePath = $null
$smokeUserEnvironment = $null
$stageRoot = if ($parentIsAdministrator) {
    Join-Path $env:SystemDrive "ThreadsDx03HostedSmoke-$runId"
} else {
    Join-Path $env:TEMP "ThreadsDx03HostedSmoke-$runId"
}
$stageCandidates = Join-Path $stageRoot "candidates"
$stageEvidence = Join-Path $stageRoot "evidence"
$stageSmokeScript = Join-Path $stageRoot "smoke_runtime.ps1"
$stageControllerSmokeScript = Join-Path $stageRoot "smoke_controller_lifecycle.ps1"
$stageControllerDesktop = Join-Path $stageRoot "desktop\threads-desktop.exe"
$stageControllerEvidence = Join-Path $stageEvidence "controller-lifecycle.json"
$failures = [System.Collections.Generic.List[string]]::new()
$icacls = Join-Path $env:SystemRoot "System32\icacls.exe"
$verificationJsonPath = Join-Path $EvidenceOutput "runtime-evidence-verification.json"
$layoutsToStage = if ($ControllerOnly) { @("shared") } else { @($RuntimeLayout) }

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
        $processArgs.Environment = $smokeUserEnvironment
        if ($parentAdminProfilePath) {
            $arguments += @("-ParentAdminProfilePath", "`"$parentAdminProfilePath`"")
            $processArgs.ArgumentList = $arguments
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

function Invoke-ControllerSmokeProcess(
    [string]$SmokeScript,
    [string]$DesktopExecutable,
    [string]$RuntimeRoot,
    [string]$SmokeEvidencePath
) {
    $stdoutPath = Join-Path $stageRoot "controller-smoke.stdout.internal.log"
    $stderrPath = Join-Path $stageRoot "controller-smoke.stderr.internal.log"
    $controllerOutput = Join-Path $EvidenceOutput "controller"
    $stdoutEvidencePath = Join-Path $controllerOutput "stdout.redacted.log"
    $stderrEvidencePath = Join-Path $controllerOutput "stderr.redacted.log"
    $processEvidencePath = Join-Path $controllerOutput "smoke-process.json"
    New-Item -ItemType Directory -Path $controllerOutput -Force | Out-Null

    $arguments = @(
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        $SmokeScript,
        "-DesktopExecutable",
        $DesktopExecutable,
        "-RuntimeRoot",
        $RuntimeRoot,
        "-ExpectedSourceRevision",
        $ExpectedSourceRevision,
        "-EvidencePath",
        $SmokeEvidencePath,
        "-VerifiedSourceRevision",
        $ExpectedSourceRevision,
        "-VerifiedWorktreeClean"
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
    $processArgs.Environment = $smokeUserEnvironment
    $process = Start-Process @processArgs
    $process.Refresh()
    $childExitCode = if ($process.HasExited) { [int]$process.ExitCode } else { $null }
    $process.Dispose()

    $smokeEvidence = $null
    if (Test-Path -LiteralPath $SmokeEvidencePath -PathType Leaf) {
        New-Item -ItemType Directory -Path (Split-Path -Parent $ControllerEvidenceOutput) -Force | Out-Null
        Copy-Item -LiteralPath $SmokeEvidencePath -Destination $ControllerEvidenceOutput -Force
        $smokeEvidence = Get-Content -LiteralPath $SmokeEvidencePath -Raw | ConvertFrom-Json
    }
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

    $smokeStatus = if ($smokeEvidence) { [string]$smokeEvidence.result } else { "BLOCKER" }
    $failureCode = if ($smokeEvidence) { [string]$smokeEvidence.failure_code } else { "controller_lifecycle_evidence_missing" }
    $smokeRanAsStandardUser = $smokeEvidence -and -not [bool]$smokeEvidence.current_user_is_administrator
    $smokePassed = $childExitCode -eq 0 -and $smokeStatus -eq "PASS" -and $smokeRanAsStandardUser
    $processEvidence = [ordered]@{
        schema_version = 1
        source_revision = $ExpectedSourceRevision
        child_exit_code = $childExitCode
        current_user_is_administrator = if ($smokeEvidence) { $smokeEvidence.current_user_is_administrator } else { $null }
        smoke_status = $smokeStatus
        failure_code = $failureCode
        status = if ($smokePassed) { "PASS" } else { "BLOCKER" }
    }
    [System.IO.File]::WriteAllText(
        $processEvidencePath,
        ($processEvidence | ConvertTo-Json -Depth 6),
        [System.Text.UTF8Encoding]::new($false)
    )
    return [bool]$smokePassed
}

try {
    New-Item -ItemType Directory -Path $EvidenceOutput -Force | Out-Null
    New-Item -ItemType Directory -Path $stageRoot -Force | Out-Null
    $preflightName = if ($ControllerOnly) {
        "hosted-controller-preflight.json"
    } else {
        "hosted-runner-preflight.json"
    }
    $preflightPath = Join-Path $EvidenceOutput $preflightName
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
        $parentAdminProfilePath = [string]$env:USERPROFILE
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

        $profileBootstrap = Start-Process -FilePath (Join-Path $PSHOME "pwsh.exe") `
            -ArgumentList @("-NoProfile", "-Command", "exit 0") `
            -Credential $smokeCredential -LoadUserProfile -PassThru -Wait -WindowStyle Hidden
        $profileBootstrap.Refresh()
        $profileBootstrapExitCode = $profileBootstrap.ExitCode
        $profileBootstrap.Dispose()
        if ($profileBootstrapExitCode -ne 0) { throw "hosted_smoke_user_profile_bootstrap_failed" }

        try {
            $smokeUserSid = (Get-LocalUser -Name $smokeUserName -ErrorAction Stop).SID.Value
            $smokeProfiles = @(
                Get-CimInstance -ClassName Win32_UserProfile -Filter "SID = '$smokeUserSid'" -ErrorAction Stop |
                    Where-Object { $_.SID -eq $smokeUserSid }
            )
        } catch {
            throw "hosted_smoke_user_profile_unavailable"
        }
        if ($smokeProfiles.Count -ne 1 -or [string]::IsNullOrWhiteSpace([string]$smokeProfiles[0].LocalPath)) {
            throw "hosted_smoke_user_profile_unavailable"
        }
        $smokeProfileRoot = [System.IO.Path]::GetFullPath([string]$smokeProfiles[0].LocalPath)
        Set-HostedSmokeAcl $stageRoot "$env:COMPUTERNAME\$smokeUserName"
    }

    if ([string]::IsNullOrWhiteSpace($smokeProfileRoot)) { throw "hosted_smoke_user_profile_unavailable" }
    $smokeHomeDrive = [System.IO.Path]::GetPathRoot($smokeProfileRoot).TrimEnd([char[]]@("\", "/"))
    if ([string]::IsNullOrWhiteSpace($smokeHomeDrive) -or
        -not $smokeProfileRoot.StartsWith($smokeHomeDrive, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "hosted_smoke_user_profile_unavailable"
    }
    $smokeUserEnvironment = @{
        USERPROFILE = $smokeProfileRoot
        APPDATA = Join-Path $smokeProfileRoot "AppData\Roaming"
        LOCALAPPDATA = Join-Path $smokeProfileRoot "AppData\Local"
        HOMEDRIVE = $smokeHomeDrive
        HOMEPATH = $smokeProfileRoot.Substring($smokeHomeDrive.Length)
        TEMP = Join-Path $smokeProfileRoot "AppData\Local\Temp"
        TMP = Join-Path $smokeProfileRoot "AppData\Local\Temp"
    }

    New-Item -ItemType Directory -Path $stageCandidates -Force | Out-Null
    New-Item -ItemType Directory -Path $stageEvidence -Force | Out-Null
    foreach ($layout in $layoutsToStage) {
        try {
            $source = Join-Path $candidateRoot $layout
            if (-not (Test-Path -LiteralPath $source)) { throw "hosted_candidate_missing_$layout" }
            Copy-Item -LiteralPath $source -Destination (Join-Path $stageCandidates $layout) -Recurse
        } catch {
            $failures.Add("$layout candidate staging failed")
        }
    }
    if ($ControllerOnly) {
        $runtimeVerificationPath = Join-Path $EvidenceOutput "runtime-evidence-verification.json"
        $runtimeManifestPath = Join-Path $stageCandidates "shared\runtime-manifest.json"
        if (-not (Test-Path -LiteralPath $runtimeVerificationPath -PathType Leaf)) {
            throw "controller_runtime_verification_evidence_missing"
        }
        if (-not (Test-Path -LiteralPath $runtimeManifestPath -PathType Leaf)) {
            throw "controller_shared_runtime_manifest_missing"
        }
        $runtimeVerification = Get-Content -LiteralPath $runtimeVerificationPath -Raw | ConvertFrom-Json
        $sharedCandidateEvidence = @(
            $runtimeVerification.candidates | Where-Object { $_.layout -eq "shared" -and $_.status -eq "PASS" }
        )
        $sharedSmokeEvidence = @(
            $runtimeVerification.hosted_smokes | Where-Object { $_.layout -eq "shared" -and $_.status -eq "PASS" }
        )
        $runtimeManifest = Get-Content -LiteralPath $runtimeManifestPath -Raw | ConvertFrom-Json
        if (
            $runtimeVerification.status -ne "PASS" -or
            $runtimeVerification.source_revision -ne $ExpectedSourceRevision -or
            $sharedCandidateEvidence.Count -ne 1 -or
            $sharedSmokeEvidence.Count -ne 1 -or
            $runtimeManifest.source_revision -ne $ExpectedSourceRevision -or
            $runtimeManifest.layout -ne "shared"
        ) {
            throw "controller_shared_runtime_artifact_not_verified"
        }
    } else {
        Copy-Item -LiteralPath $smokeScript -Destination $stageSmokeScript
    }
    if ($controllerSmokeRequested) {
        $ControllerDesktopExecutable = (Resolve-Path -LiteralPath $ControllerDesktopExecutable).Path
        if (-not (Test-Path -LiteralPath $controllerSmokeScript -PathType Leaf)) {
            throw "hosted_controller_smoke_script_missing"
        }
        if (-not (Test-Path -LiteralPath $ControllerDesktopExecutable -PathType Leaf)) {
            throw "hosted_controller_desktop_executable_missing"
        }
        New-Item -ItemType Directory -Path (Split-Path -Parent $stageControllerDesktop) -Force | Out-Null
        Copy-Item -LiteralPath $ControllerDesktopExecutable -Destination $stageControllerDesktop
        Copy-Item -LiteralPath $controllerSmokeScript -Destination $stageControllerSmokeScript
    }

    if (-not $ControllerOnly) {
        $archiveArgs = @(
            "--candidate-root", $stageCandidates,
            "--layouts", $RuntimeLayout,
            "--expected-source-revision", $ExpectedSourceRevision,
            "--uv-lock", $lockPath,
            "--download-manifest", $downloadManifestPath,
            "--postgres-archive", $postgresArchivePath,
            "--result-path", (Join-Path $EvidenceOutput "staged-candidate-verification.json")
        )
        & uv run --locked --no-dev --group packaging python $verifierScript @archiveArgs
        if ($LASTEXITCODE -ne 0) { $failures.Add("hosted staged candidate checksum verification failed") }
    }

    if (-not $ControllerOnly) {
        foreach ($layout in $layoutsToStage) {
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
    }

    if ($controllerSmokeRequested) {
        try {
            $controllerSmokePassed = Invoke-ControllerSmokeProcess `
                $stageControllerSmokeScript `
                $stageControllerDesktop `
                (Join-Path $stageCandidates $RuntimeLayout) `
                $stageControllerEvidence
            if (-not $controllerSmokePassed) { $failures.Add("shared M1 Controller lifecycle smoke failed") }
        } catch {
            $safeFailure = ConvertTo-SafeChildOutput ([string]$_.Exception.Message)
            $failures.Add("Controller lifecycle smoke harness failed: $([regex]::Replace($safeFailure, '[^A-Za-z0-9_.-]', '_'))")
        }
    }

    if (-not $ControllerOnly) {
        $fullVerificationArgs = @(
            "--candidate-root", $candidateRoot,
            "--layouts", $RuntimeLayout,
            "--expected-source-revision", $ExpectedSourceRevision,
            "--uv-lock", $lockPath,
            "--download-manifest", $downloadManifestPath,
            "--postgres-archive", $postgresArchivePath,
            "--smoke-evidence-root", $EvidenceOutput,
            "--result-path", $verificationJsonPath
        )
        & uv run --locked --no-dev --group packaging python $verifierScript @fullVerificationArgs
        if ($LASTEXITCODE -ne 0) { $failures.Add("hosted runtime evidence verification failed") }
    }
} catch {
    $safeFailure = ConvertTo-SafeChildOutput ([string]$_.Exception.Message)
    $failures.Add(("hosted runtime setup failed: " + [regex]::Replace($safeFailure, "[^A-Za-z0-9_.-]", "_")))
} finally {
    try {
        if (-not $ControllerOnly -and -not (Test-Path -LiteralPath $verificationJsonPath)) {
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

if ($ControllerOnly) {
    Write-Output "DX-04 hosted Windows x64 non-admin Controller smoke PASS for $ExpectedSourceRevision"
} else {
    Write-Output "DX-03 hosted Windows x64 non-admin $RuntimeLayout runtime smoke and evidence verification PASS for $ExpectedSourceRevision"
}
