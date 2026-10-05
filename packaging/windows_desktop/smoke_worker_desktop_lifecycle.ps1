[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$RepositoryRoot,
    [Parameter(Mandatory = $true)]
    [string]$DesktopArtifactRoot,
    [Parameter(Mandatory = $true)]
    [string]$WorkerReleaseDirectory,
    [Parameter(Mandatory = $true)]
    [string]$RuntimeRoot,
    [Parameter(Mandatory = $true)]
    [ValidatePattern("^[0-9a-f]{40}$")]
    [string]$ExpectedSourceRevision,
    [Parameter(Mandatory = $true)]
    [ValidateSet(
        "legacy_running_cutover",
        "legacy_ready_cutover",
        "legacy_disabled_desktop_start",
        "no_task_fail_closed",
        "identity_key_fail_closed",
        "duplicate_process_lock",
        "graceful_busy_drain_quit",
        "drain_failure_force_interrupt",
        "desktop_crash_logout_recovery",
        "rollback_legacy_task",
        "headed_chromium_profile_continuity"
    )]
    [string]$Scenario,
    [Parameter(Mandatory = $true)]
    [string]$EvidencePath,
    [Parameter()]
    [switch]$ChildMode,
    [Parameter()]
    [string]$WrongUserDataRoot
)

$ErrorActionPreference = "Stop"
$PSNativeCommandUseErrorActionPreference = $false
$RepositoryRoot = [IO.Path]::GetFullPath($RepositoryRoot)
$DesktopArtifactRoot = [IO.Path]::GetFullPath($DesktopArtifactRoot)
$WorkerReleaseDirectory = [IO.Path]::GetFullPath($WorkerReleaseDirectory)
$RuntimeRoot = [IO.Path]::GetFullPath($RuntimeRoot)
$EvidencePath = [IO.Path]::GetFullPath($EvidencePath)
$WorkerDesktopFixture = Join-Path $RepositoryRoot "packaging\windows_desktop\worker_desktop_fixture.py"
$WorkerDesktopAggregator = Join-Path $RepositoryRoot "packaging\windows_desktop\aggregate_worker_desktop_scenarios.py"
$TaskHelper = Join-Path $DesktopArtifactRoot "Manage-ThreadsWorkerTask.ps1"
$DesktopExecutable = Join-Path $DesktopArtifactRoot "threads-desktop.exe"
$WorkerExecutable = Join-Path $WorkerReleaseDirectory "threads-worker.exe"
$Python = Join-Path $RepositoryRoot ".venv\Scripts\python.exe"

function Write-SetupBlocker([string]$Code) {
    New-Item -ItemType Directory -Path (Split-Path -Parent $EvidencePath) -Force | Out-Null
    & $Python $WorkerDesktopAggregator `
        --write-blocker-evidence $EvidencePath `
        --source-sha $ExpectedSourceRevision `
        --scenario $Scenario `
        --failure-code $Code | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "worker_desktop_evidence_write_failed" }
}

function ConvertTo-StartProcessArgument([string]$Value) {
    return '"' + $Value.Replace('"', '\"') + '"'
}

function Set-HostedUserAcl([string]$Path, [string]$Principal) {
    $icacls = Join-Path $env:SystemRoot "System32\icacls.exe"
    & $icacls $Path /inheritance:r /grant:r `
        "${Principal}:(OI)(CI)F" `
        "BUILTIN\Administrators:(OI)(CI)F" `
        "NT AUTHORITY\SYSTEM:(OI)(CI)F" | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "worker_desktop_user_profile_acl_failed" }
}

function Invoke-HostedStandardUser {
    $sourceRevision = (& git -C $RepositoryRoot rev-parse HEAD).Trim()
    if (
        $env:GITHUB_ACTIONS -ne "true" -or
        $env:RUNNER_ENVIRONMENT -ne "github-hosted" -or
        $env:RUNNER_OS -ne "Windows" -or
        $env:RUNNER_ARCH -ne "X64" -or
        -not [Environment]::Is64BitOperatingSystem -or
        $sourceRevision -ne $ExpectedSourceRevision -or
        -not (Test-Path -LiteralPath $DesktopExecutable -PathType Leaf) -or
        -not (Test-Path -LiteralPath $TaskHelper -PathType Leaf) -or
        -not (Test-Path -LiteralPath $WorkerExecutable -PathType Leaf) -or
        -not (Test-Path -LiteralPath (Join-Path $RuntimeRoot "postgresql\bin\initdb.exe") -PathType Leaf)
    ) {
        Write-SetupBlocker "worker_desktop_harness_input_invalid"
        throw "worker_desktop_harness_input_invalid"
    }

    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [Security.Principal.WindowsPrincipal]::new($identity)
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        Write-SetupBlocker "worker_desktop_standard_user_setup_unavailable"
        throw "worker_desktop_standard_user_setup_unavailable"
    }

    $runId = [guid]::NewGuid().ToString("N")
    $userName = "dx07_$($runId.Substring(0, 12))"
    $profilePath = $null
    $wrongUserRoot = $WrongUserDataRoot
    $createdWrongUserRoot = $false
    $savedHosts = $null
    $hostsPath = Join-Path $env:SystemRoot "System32\drivers\etc\hosts"
    $hostsMarker = "# dx07-worker-desktop-$runId"
    $operatorPassword = "DX07Owner" + [guid]::NewGuid().ToString("N")
    $userPassword = $null
    $credential = $null
    $childProcess = $null
    $stageRoot = Join-Path $env:TEMP "ThreadsDx07WorkerDesktop-$runId"
    $internalEvidence = Join-Path $stageRoot "evidence\worker-desktop.json"
    $userSid = $null
    try {
        if (-not (Get-Command -Name New-LocalUser -ErrorAction SilentlyContinue)) {
            throw "worker_desktop_standard_user_setup_unavailable"
        }
        $randomBytes = [byte[]]::new(32)
        $random = [Security.Cryptography.RandomNumberGenerator]::Create()
        try { $random.GetBytes($randomBytes) }
        finally { $random.Dispose() }
        $userPassword = [Convert]::ToBase64String($randomBytes) + "aA7!"
        [Array]::Clear($randomBytes, 0, $randomBytes.Length)
        $securePassword = ConvertTo-SecureString -String $userPassword -AsPlainText -Force
        New-LocalUser -Name $userName -Password $securePassword `
            -AccountNeverExpires -PasswordNeverExpires -UserMayNotChangePassword | Out-Null
        $credential = [Management.Automation.PSCredential]::new(
            "$env:COMPUTERNAME\$userName", $securePassword
        )
        $securePassword = $null
        $userPassword = $null

        $profileBootstrap = Start-Process -FilePath (Join-Path $PSHOME "pwsh.exe") `
            -ArgumentList @("-NoProfile", "-Command", "exit 0") `
            -Credential $credential -LoadUserProfile -PassThru -Wait -WindowStyle Hidden
        $profileBootstrap.Refresh()
        $profileBootstrapExitCode = $profileBootstrap.ExitCode
        $profileBootstrap.Dispose()
        if ($profileBootstrapExitCode -ne 0) {
            throw "worker_desktop_standard_user_profile_failed"
        }
        $userSid = (Get-LocalUser -Name $userName -ErrorAction Stop).SID.Value
        $profiles = @(
            Get-CimInstance -ClassName Win32_UserProfile -Filter "SID = '$userSid'" |
                Where-Object { $_.SID -eq $userSid }
        )
        if ($profiles.Count -ne 1 -or [string]::IsNullOrWhiteSpace([string]$profiles[0].LocalPath)) {
            throw "worker_desktop_standard_user_profile_failed"
        }
        $profilePath = [IO.Path]::GetFullPath([string]$profiles[0].LocalPath)
        New-Item -ItemType Directory -Path $stageRoot -Force | Out-Null
        Set-HostedUserAcl $stageRoot "$env:COMPUTERNAME\$userName"

        if ([string]::IsNullOrWhiteSpace($wrongUserRoot)) {
            $wrongUserRoot = Join-Path $env:TEMP "DX07-WrongUser-$runId\worker-data"
            $wrongUserRoot = [IO.Path]::GetFullPath($wrongUserRoot)
            $wrongUserJson = & $Python $WorkerDesktopFixture `
                seed-identity-only --data-root $wrongUserRoot
            if ($LASTEXITCODE -ne 0) { throw "worker_desktop_wrong_user_fixture_failed" }
            $createdWrongUserRoot = $true
            $wrongUserParent = Split-Path -Parent $wrongUserRoot
            & (Join-Path $env:SystemRoot "System32\icacls.exe") $wrongUserParent `
                /grant "${env:COMPUTERNAME}\${userName}:(OI)(CI)RX" /T | Out-Null
            if ($LASTEXITCODE -ne 0) { throw "worker_desktop_wrong_user_fixture_failed" }
        }

        $hostBytes = [IO.File]::ReadAllBytes($hostsPath)
        $hostText = [Text.Encoding]::UTF8.GetString($hostBytes)
        if ($hostText -match '(?im)^\s*[^#\r\n]+\s+www\.threads\.com(?:\s|$)') {
            throw "worker_desktop_fixture_hostname_conflict"
        }
        $savedHosts = $hostBytes
        [IO.File]::AppendAllText(
            $hostsPath,
            [Environment]::NewLine + "127.0.0.1 www.threads.com $hostsMarker" + [Environment]::NewLine,
            [Text.Encoding]::ASCII
        )
        & (Join-Path $env:SystemRoot "System32\ipconfig.exe") /flushdns | Out-Null

        $homeDrive = [IO.Path]::GetPathRoot($profilePath).TrimEnd([char[]]@("\", "/"))
        $childEnvironment = @{
            GITHUB_ACTIONS = $env:GITHUB_ACTIONS
            RUNNER_ENVIRONMENT = $env:RUNNER_ENVIRONMENT
            RUNNER_OS = $env:RUNNER_OS
            RUNNER_ARCH = $env:RUNNER_ARCH
            ImageOS = $env:ImageOS
            ImageVersion = $env:ImageVersion
            USERPROFILE = $profilePath
            APPDATA = Join-Path $profilePath "AppData\Roaming"
            LOCALAPPDATA = Join-Path $profilePath "AppData\Local"
            HOMEDRIVE = $homeDrive
            HOMEPATH = $profilePath.Substring($homeDrive.Length)
            TEMP = Join-Path $profilePath "AppData\Local\Temp"
            TMP = Join-Path $profilePath "AppData\Local\Temp"
            PATH = "$env:SystemRoot\System32;$env:SystemRoot;$PSHOME"
            THREADS_DX07_OPERATOR_PASSWORD = $operatorPassword
        }
        foreach ($directory in @($childEnvironment.APPDATA, $childEnvironment.LOCALAPPDATA, $childEnvironment.TEMP)) {
            New-Item -ItemType Directory -Path $directory -Force | Out-Null
        }
        $childArguments = @(
            "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
            (ConvertTo-StartProcessArgument $PSCommandPath),
            "-RepositoryRoot", (ConvertTo-StartProcessArgument $RepositoryRoot),
            "-DesktopArtifactRoot", (ConvertTo-StartProcessArgument $DesktopArtifactRoot),
            "-WorkerReleaseDirectory", (ConvertTo-StartProcessArgument $WorkerReleaseDirectory),
            "-RuntimeRoot", (ConvertTo-StartProcessArgument $RuntimeRoot),
            "-ExpectedSourceRevision", $ExpectedSourceRevision,
            "-Scenario", $Scenario,
            "-EvidencePath", (ConvertTo-StartProcessArgument $internalEvidence),
            "-WrongUserDataRoot", (ConvertTo-StartProcessArgument $wrongUserRoot),
            "-ChildMode"
        )
        $stdoutPath = Join-Path $stageRoot "child.stdout.internal.log"
        $stderrPath = Join-Path $stageRoot "child.stderr.internal.log"
        $childProcess = Start-Process -FilePath (Join-Path $PSHOME "pwsh.exe") `
            -ArgumentList $childArguments -WorkingDirectory $RepositoryRoot `
            -Credential $credential -LoadUserProfile -Environment $childEnvironment `
            -WindowStyle Hidden -PassThru -Wait `
            -RedirectStandardOutput $stdoutPath -RedirectStandardError $stderrPath
        $childProcess.Refresh()
        $childExitCode = [int]$childProcess.ExitCode
        $childProcess.Dispose()
        $childProcess = $null

        if (Test-Path -LiteralPath $internalEvidence -PathType Leaf) {
            New-Item -ItemType Directory -Path (Split-Path -Parent $EvidencePath) -Force | Out-Null
            Copy-Item -LiteralPath $internalEvidence -Destination $EvidencePath -Force
        } else {
            Write-SetupBlocker "worker_desktop_harness_evidence_missing"
            $childExitCode = 1
        }
        if ($childExitCode -ne 0) { throw "worker_desktop_scenario_blocked" }
    } catch {
        if (-not (Test-Path -LiteralPath $EvidencePath -PathType Leaf)) {
            $failure = if ($_.Exception.Message -match '^worker_[a-z0-9_]+$') {
                $_.Exception.Message
            } else { "worker_desktop_hosted_setup_failed" }
            Write-SetupBlocker $failure
        }
        throw "worker_desktop_scenario_blocked"
    } finally {
        if ($childProcess) { $childProcess.Dispose() }
        if ($savedHosts) {
            [IO.File]::WriteAllBytes($hostsPath, $savedHosts)
            & (Join-Path $env:SystemRoot "System32\ipconfig.exe") /flushdns | Out-Null
        }
        if ($createdWrongUserRoot -and (Test-Path -LiteralPath $wrongUserRoot)) {
            Remove-Item -LiteralPath (Split-Path -Parent $wrongUserRoot) -Recurse -Force -ErrorAction SilentlyContinue
        }
        if ($userName) { Remove-LocalUser -Name $userName -ErrorAction SilentlyContinue }
        if (Test-Path -LiteralPath $stageRoot) {
            Remove-Item -LiteralPath $stageRoot -Recurse -Force -ErrorAction SilentlyContinue
        }
        $operatorPassword = $null
        $userPassword = $null
        $credential = $null
    }
}

if (-not $ChildMode) {
    try {
        Invoke-HostedStandardUser
        Write-Output "Worker Desktop hosted scenario PASS: $Scenario"
        exit 0
    } catch {
        [Console]::Error.WriteLine("worker_desktop_scenario_blocked")
        exit 1
    }
}

Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
Add-Type -AssemblyName System.Windows.Forms
if (-not ("ThreadsWorkerDesktopSmoke.NativeMethods" -as [type])) {
    Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;
using Microsoft.Win32.SafeHandles;

namespace ThreadsWorkerDesktopSmoke {
    public static class NativeMethods {
        [StructLayout(LayoutKind.Sequential)]
        private struct FileTime { public uint Low; public uint High; }
        [StructLayout(LayoutKind.Sequential)]
        private struct ByHandleFileInformation {
            public uint FileAttributes;
            public FileTime CreationTime;
            public FileTime LastAccessTime;
            public FileTime LastWriteTime;
            public uint VolumeSerialNumber;
            public uint FileSizeHigh;
            public uint FileSizeLow;
            public uint NumberOfLinks;
            public uint FileIndexHigh;
            public uint FileIndexLow;
        }

        [DllImport("user32.dll", SetLastError = true)]
        public static extern bool PostMessage(IntPtr window, uint message, IntPtr wParam, IntPtr lParam);
        [DllImport("user32.dll", SetLastError = true)]
        public static extern bool IsWindowVisible(IntPtr window);
        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern SafeFileHandle CreateFileW(
            string path, uint access, uint share, IntPtr security, uint creation, uint flags, IntPtr template
        );
        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool GetFileInformationByHandle(
            SafeFileHandle file, out ByHandleFileInformation information
        );
        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool SetFilePointerEx(SafeFileHandle file, long distance, out long position, uint method);
        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool ReadFile(SafeFileHandle file, byte[] buffer, uint count, out uint read, IntPtr overlapped);
        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern uint GetFileType(SafeFileHandle file);

        public static int ObserveLockByte(string path) {
            const uint GENERIC_READ = 0x80000000;
            const uint SHARE_ALL = 0x00000001 | 0x00000002 | 0x00000004;
            const uint OPEN_EXISTING = 3;
            const uint OPEN_REPARSE_POINT = 0x00200000;
            const uint ATTRIBUTE_REPARSE_POINT = 0x00000400;
            const int ERROR_FILE_NOT_FOUND = 2;
            const int ERROR_PATH_NOT_FOUND = 3;
            const int ERROR_LOCK_VIOLATION = 33;
            using (SafeFileHandle file = CreateFileW(path, GENERIC_READ, SHARE_ALL, IntPtr.Zero,
                    OPEN_EXISTING, OPEN_REPARSE_POINT, IntPtr.Zero)) {
                if (file.IsInvalid) {
                    int error = Marshal.GetLastWin32Error();
                    return error == ERROR_FILE_NOT_FOUND || error == ERROR_PATH_NOT_FOUND ? 0 : 2;
                }
                ByHandleFileInformation info;
                if (!GetFileInformationByHandle(file, out info) ||
                    (info.FileAttributes & ATTRIBUTE_REPARSE_POINT) != 0 || GetFileType(file) != 1) return 2;
                long position;
                if (!SetFilePointerEx(file, 0, out position, 0)) return 2;
                byte[] first = new byte[1];
                uint read;
                if (ReadFile(file, first, 1, out read, IntPtr.Zero)) return 0;
                return Marshal.GetLastWin32Error() == ERROR_LOCK_VIOLATION ? 1 : 2;
            }
        }
    }
}
"@
}

$script:OperatorPassword = $env:THREADS_DX07_OPERATOR_PASSWORD
if ([string]::IsNullOrWhiteSpace($script:OperatorPassword)) { throw "worker_desktop_operator_fixture_secret_missing" }
Remove-Item Env:\THREADS_DX07_OPERATOR_PASSWORD -ErrorAction SilentlyContinue
$script:WorkerOverrideNames = @(
    "THREADS_WORKER_ENROLLMENT_CODE",
    "THREADS_WORKER_CONTROL_PLANE_URL",
    "THREADS_WORKER_DATA_ROOT",
    "THREADS_WORKER_DISPLAY_NAME",
    "THREADS_WORKER_AGENT_VERSION",
    "THREADS_WORKER_MAX_CONCURRENT_JOBS",
    "THREADS_WORKER_MAX_BROWSER_SESSIONS",
    "THREADS_WORKER_FEED_BROWSE_ENABLED",
    "THREADS_WORKER_THREAD_OPEN_ENABLED",
    "THREADS_WORKER_PROFILE_OPEN_ENABLED",
    "THREADS_WORKER_MEDIA_LOCAL_UPLOAD_ENABLED"
)
foreach ($name in $script:WorkerOverrideNames) {
    Remove-Item -LiteralPath "Env:\$name" -ErrorAction SilentlyContinue
}
Get-ChildItem Env: | Where-Object Name -Like "THREADS_WORKER_*" | ForEach-Object {
    Remove-Item -LiteralPath "Env:\$($_.Name)" -ErrorAction SilentlyContinue
}

$script:ScenarioChecks = @{
    legacy_running_cutover = @(
        "legacy_running_observed", "drain_post_once", "authoritative_offline", "legacy_natural_exit",
        "process_lock_released", "task_disabled_after_offline", "exact_desktop_worker_started",
        "same_identity_and_data_root", "desktop_online", "single_owner"
    )
    legacy_ready_cutover = @(
        "legacy_ready_observed", "legacy_task_started_first", "drain_post_once", "authoritative_offline",
        "legacy_natural_exit", "process_lock_released", "task_disabled_after_offline",
        "exact_desktop_worker_started", "same_identity_and_data_root", "desktop_online", "single_owner"
    )
    legacy_disabled_desktop_start = @(
        "task_disabled_observed", "enrolled_identity_unchanged", "process_lock_free_before_start",
        "exact_package_and_host_config", "desktop_online", "task_remains_disabled", "single_owner"
    )
    no_task_fail_closed = @(
        "not_registered_diagnostic", "no_executable_discovery", "identity_unchanged", "no_task_created",
        "no_worker_spawned"
    )
    identity_key_fail_closed = @(
        "missing_key_rejected", "corrupt_key_rejected", "mismatched_identity_rejected",
        "wrong_user_unprotect_rejected", "no_replacement_identity_or_key", "durable_data_unchanged",
        "no_worker_spawned"
    )
    duplicate_process_lock = @(
        "existing_worker_lock_held", "desktop_start_blocked", "one_worker_process_only",
        "journal_unchanged", "no_second_owner"
    )
    graceful_busy_drain_quit = @(
        "operator_session_retained_until_offline", "drain_post_once", "status_get_only_polling",
        "busy_draining_observed", "counts_and_quiescence_observed", "authoritative_offline",
        "worker_natural_exit", "chromium_natural_exit", "process_lock_released", "no_job_object_termination"
    )
    drain_failure_force_interrupt = @(
        "graceful_failure_intervention", "no_automatic_force_fallback", "exact_force_phrase_required",
        "explicit_force_operation_invoked", "offline_not_fabricated", "drain_complete_not_fabricated",
        "legacy_task_remains_disabled", "identity_and_data_preserved", "process_tree_terminated",
        "forced_interruption_diagnostic", "operator_api_live_after_drain_failure",
        "force_authorization_rechecked"
    )
    desktop_crash_logout_recovery = @(
        "desktop_parent_abnormal_exit", "captured_descendants_alive_at_crash",
        "job_object_reaped_worker_descendants", "disabled_task_binding_unchanged",
        "identity_key_root_unchanged", "journal_profile_preserved", "worker_reconciliation_observed",
        "no_duplicate_identity_tree", "not_reported_as_graceful_offline"
    )
    rollback_legacy_task = @(
        "desktop_drain_post_once", "authoritative_offline", "desktop_natural_exit", "process_lock_released",
        "task_enabled_after_release", "legacy_started_after_enable", "same_worker_identity", "single_owner"
    )
    headed_chromium_profile_continuity = @(
        "packaged_worker_browser_capability_executed", "chromium_started_headed", "not_session_zero_or_service",
        "worker_and_chromium_interactive_user", "desktop_hide_did_not_stop_worker",
        "worker_restart_requested", "restart_drain_post_once", "restart_authoritative_offline",
        "old_worker_natural_exit", "restart_process_lock_released", "legacy_task_disabled_through_restart",
        "new_worker_pid_after_restart", "restart_reused_identity_and_root", "restarted_worker_online",
        "second_profile_capability_executed", "profile_sentinel_survived_boundary",
        "profile_not_copied_or_reinitialized", "same_identity_across_boundary"
    )
}
$script:Checks = [ordered]@{}
foreach ($check in $script:ScenarioChecks[$Scenario]) { $script:Checks[$check] = $false }
$script:Facts = [ordered]@{
    worker_uuid = $null
    identity_marker_sha256 = $null
    protected_key_sha256 = $null
    journal_sha256 = $null
    profile_tree_sha256 = $null
    profile_sentinel_sha256 = $null
    task_state_timeline = [System.Collections.Generic.List[string]]::new()
    ownership_timeline = [System.Collections.Generic.List[string]]::new()
    worker_status_timeline = [System.Collections.Generic.List[string]]::new()
    lock_observation_timeline = [System.Collections.Generic.List[string]]::new()
    process_ids = [ordered]@{ desktop = $null; worker = $null; chromium = [System.Collections.Generic.List[int]]::new() }
    drain_post_count = 0
    drain_status_get_count = 0
    operator_me_get_count = 0
    worker_pid_timeline = [System.Collections.Generic.List[int]]::new()
    status_counts_timeline = [System.Collections.Generic.List[object]]::new()
}
$script:Processes = [System.Collections.Generic.List[System.Diagnostics.Process]]::new()
$script:DesktopProcess = $null
$script:Fixture = $null
$script:DatabaseUrl = $null
$script:DataRoot = $null
$script:TaskInstalled = $false
$script:ApiProcess = $null
$script:ExternalWorkerPid = 0
$script:PgStarted = $false
$script:RootCertificate = $null
$script:CertificateStore = $null
$script:PageReleaseFile = $null
$script:PageProcess = $null
$script:DrainFaultFile = $null
$script:WorkerStatusBeforeCleanup = $null

function Assert-ScenarioCheck([string]$Name, [bool]$Condition, [string]$FailureCode) {
    if (-not $script:Checks.Contains($Name)) { throw "worker_desktop_harness_check_unexpected" }
    $script:Checks[$Name] = $Condition
    if (-not $Condition) { throw $FailureCode }
}

function Invoke-Fixture([string[]]$Arguments, [switch]$SeedOwner) {
    $previousDatabaseUrl = $env:THREADS_PLATFORM_DATABASE_URL
    $previousPassword = $env:THREADS_DX07_OPERATOR_PASSWORD
    try {
        $env:THREADS_PLATFORM_DATABASE_URL = $script:DatabaseUrl
        if ($SeedOwner) { $env:THREADS_DX07_OPERATOR_PASSWORD = $script:OperatorPassword }
        $output = @(& $Python $WorkerDesktopFixture @Arguments 2>$null)
        if ($LASTEXITCODE -ne 0) { throw "worker_desktop_fixture_operation_failed" }
        $json = $output -join "`n"
        return ($json | ConvertFrom-Json -ErrorAction Stop)
    } catch {
        throw "worker_desktop_fixture_operation_failed"
    } finally {
        if ($null -eq $previousDatabaseUrl) { Remove-Item Env:\THREADS_PLATFORM_DATABASE_URL -ErrorAction SilentlyContinue }
        else { $env:THREADS_PLATFORM_DATABASE_URL = $previousDatabaseUrl }
        if ($null -eq $previousPassword) { Remove-Item Env:\THREADS_DX07_OPERATOR_PASSWORD -ErrorAction SilentlyContinue }
        else { $env:THREADS_DX07_OPERATOR_PASSWORD = $previousPassword }
    }
}

function Start-FixtureProcess([string]$Name, [string[]]$Arguments) {
    $stdout = Join-Path $script:WorkRoot "$Name.stdout.internal.log"
    $stderr = Join-Path $script:WorkRoot "$Name.stderr.internal.log"
    $arguments = @($WorkerDesktopFixture) + $Arguments
    $start = @{
        FilePath = $Python
        ArgumentList = @($arguments | ForEach-Object { ConvertTo-StartProcessArgument $_ })
        WorkingDirectory = $RepositoryRoot
        WindowStyle = "Hidden"
        PassThru = $true
        RedirectStandardOutput = $stdout
        RedirectStandardError = $stderr
    }
    if ($script:DatabaseUrl) { $start.Environment = @{ THREADS_PLATFORM_DATABASE_URL = $script:DatabaseUrl } }
    $process = Start-Process @start
    $script:Processes.Add($process) | Out-Null
    return $process
}

function Invoke-TaskHelper([string]$Operation, [switch]$ConfirmDrainOffline) {
    $arguments = @("-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", $TaskHelper, $Operation)
    if ($Operation -eq "Install") {
        $arguments += @("-ReleaseDirectory", $WorkerReleaseDirectory, "-HostConfigPath", $script:Fixture.host_config_path)
    }
    if ($ConfirmDrainOffline) { $arguments += "-ConfirmDurableDrainOffline" }
    $output = @(& powershell.exe @arguments 2>$null)
    if ($LASTEXITCODE -ne 0) { throw "worker_legacy_task_operation_failed" }
    return $output -join "`n"
}

function Observe-Task {
    $raw = Invoke-TaskHelper "InspectJson"
    try { $inspection = $raw | ConvertFrom-Json -ErrorAction Stop }
    catch { throw "worker_legacy_task_inspection_invalid" }
    $state = [string]$inspection.state
    if ($state -notin @("NOT_REGISTERED", "INVALID", "RUNNING", "READY", "DISABLED")) {
        throw "worker_legacy_task_inspection_invalid"
    }
    $timeline = $script:Facts.task_state_timeline
    if ($timeline.Count -eq 0 -or $timeline[$timeline.Count - 1] -ne $state) { $timeline.Add($state) }
    return $inspection
}

function Observe-Lock([string]$Root = $script:DataRoot) {
    if ([string]::IsNullOrWhiteSpace($Root)) { return "UNAVAILABLE" }
    $path = Join-Path $Root "worker\agent.lock"
    $result = [ThreadsWorkerDesktopSmoke.NativeMethods]::ObserveLockByte($path)
    $state = switch ($result) { 0 { "NOT_HELD" } 1 { "HELD" } default { "UNAVAILABLE" } }
    $timeline = $script:Facts.lock_observation_timeline
    if ($timeline.Count -eq 0 -or $timeline[$timeline.Count - 1] -ne $state) { $timeline.Add($state) }
    return $state
}

function Get-Sha256([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $null }
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

function Get-TreeSha256([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path -PathType Container)) { return $null }
    $lines = [System.Collections.Generic.List[string]]::new()
    foreach ($item in (Get-ChildItem -LiteralPath $Path -File -Recurse -Force | Sort-Object FullName)) {
        $relative = [IO.Path]::GetRelativePath($Path, $item.FullName).Replace("\", "/")
        $lines.Add("$relative`:$((Get-Sha256 $item.FullName))")
    }
    $bytes = [Text.Encoding]::UTF8.GetBytes(($lines -join "`n"))
    $digest = [Security.Cryptography.SHA256]::HashData($bytes)
    return [Convert]::ToHexString($digest).ToLowerInvariant()
}

function Get-WorkerProcesses {
    return @(
        Get-CimInstance -ClassName Win32_Process -Filter "Name = 'threads-worker.exe'" |
            Where-Object {
                $_.ExecutablePath -and
                [IO.Path]::GetFullPath([string]$_.ExecutablePath).Equals(
                    [IO.Path]::GetFullPath($WorkerExecutable), [StringComparison]::OrdinalIgnoreCase
                )
            }
    )
}

function Get-AnyWorkerProcesses {
    return @(
        Get-CimInstance -ClassName Win32_Process -Filter "Name = 'threads-worker.exe'"
    )
}

function Get-ProcessById([int]$ProcessId) {
    if ($ProcessId -le 0) { return $null }
    return Get-CimInstance -ClassName Win32_Process -Filter "ProcessId = $ProcessId" -ErrorAction SilentlyContinue
}

function Test-ProcessIdAlive([int]$ProcessId) {
    return $null -ne (Get-ProcessById $ProcessId)
}

function Test-InteractiveProcessOwner([object[]]$Processes, [string]$UserSid, [int]$SessionId) {
    if ($Processes.Count -eq 0 -or $SessionId -le 0) { return $false }
    foreach ($process in $Processes) {
        $owner = Invoke-CimMethod -InputObject $process -MethodName GetOwnerSid -ErrorAction SilentlyContinue
        if (-not $owner -or $owner.Sid -ne $UserSid -or [int]$process.SessionId -ne $SessionId) {
            return $false
        }
    }
    return $true
}

function Get-ChromiumProcesses {
    if (-not $script:Fixture -or -not $script:Fixture.profile_directory) { return @() }
    $profile = [string]$script:Fixture.profile_directory
    return @(
        Get-CimInstance -ClassName Win32_Process |
            Where-Object {
                $_.Name -in @("chrome.exe", "chrome-headless-shell.exe") -and
                $_.CommandLine -and
                $_.CommandLine.IndexOf($profile, [StringComparison]::OrdinalIgnoreCase) -ge 0
            }
    )
}

function Get-DesktopProcess([int]$ProcessId = 0) {
    if ($ProcessId -gt 0) {
        $candidate = Get-CimInstance -ClassName Win32_Process -Filter "ProcessId = $ProcessId" -ErrorAction SilentlyContinue
        if ($candidate -and [IO.Path]::GetFullPath([string]$candidate.ExecutablePath).Equals(
                [IO.Path]::GetFullPath($DesktopExecutable), [StringComparison]::OrdinalIgnoreCase)) {
            return $candidate
        }
        return $null
    }
    return Get-CimInstance -ClassName Win32_Process -Filter "Name = 'threads-desktop.exe'" |
        Where-Object {
            $_.ExecutablePath -and
            [IO.Path]::GetFullPath([string]$_.ExecutablePath).Equals(
                [IO.Path]::GetFullPath($DesktopExecutable), [StringComparison]::OrdinalIgnoreCase
            )
        } | Select-Object -First 1
}

function Get-DesktopWindow([int]$ProcessId) {
    $process = $null
    try {
        $process = [Diagnostics.Process]::GetProcessById($ProcessId)
        $process.Refresh()
        if ($process.HasExited -or $process.MainWindowHandle -eq [IntPtr]::Zero) { return $null }
        return [System.Windows.Automation.AutomationElement]::FromHandle($process.MainWindowHandle)
    } catch { return $null }
    finally { if ($process) { $process.Dispose() } }
}

function Find-UiElement(
    [System.Windows.Automation.AutomationElement]$Root,
    [string]$Name,
    [System.Windows.Automation.ControlType]$Type
) {
    if (-not $Root) { return $null }
    $conditions = @(
        [System.Windows.Automation.PropertyCondition]::new(
            [System.Windows.Automation.AutomationElement]::NameProperty, $Name
        ),
        [System.Windows.Automation.PropertyCondition]::new(
            [System.Windows.Automation.AutomationElement]::ControlTypeProperty, $Type
        )
    )
    try {
        return $Root.FindFirst(
            [System.Windows.Automation.TreeScope]::Descendants,
            [System.Windows.Automation.AndCondition]::new($conditions)
        )
    } catch { return $null }
}

function Get-UiNames([int]$ProcessId) {
    $window = Get-DesktopWindow $ProcessId
    if (-not $window) { return @() }
    try {
        return @($window.FindAll(
            [System.Windows.Automation.TreeScope]::Descendants,
            [System.Windows.Automation.Condition]::TrueCondition
        ) | ForEach-Object { try { $_.Current.Name } catch { "" } } | Where-Object { $_ })
    } catch { return @() }
}

function Wait-UiText([int]$ProcessId, [string]$Text, [int]$TimeoutSeconds = 20) {
    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    do {
        if ((Get-UiNames $ProcessId | Where-Object { $_.Contains($Text, [StringComparison]::OrdinalIgnoreCase) }).Count -gt 0) {
            return $true
        }
        Start-Sleep -Milliseconds 250
    } while ([DateTime]::UtcNow -lt $deadline)
    return $false
}

function Invoke-UiButton([int]$ProcessId, [string]$Name, [int]$TimeoutSeconds = 25) {
    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    do {
        $window = Get-DesktopWindow $ProcessId
        $button = Find-UiElement $window $Name ([System.Windows.Automation.ControlType]::Button)
        if ($button) {
            try {
                if ($button.Current.IsEnabled) {
                    $pattern = $button.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern)
                    $pattern.Invoke()
                    return
                }
            } catch { }
        }
        Start-Sleep -Milliseconds 250
    } while ([DateTime]::UtcNow -lt $deadline)
    throw "worker_desktop_ui_button_unavailable"
}

function Set-UiField([int]$ProcessId, [string]$Name, [string]$Value, [switch]$KeyboardOnly) {
    $window = Get-DesktopWindow $ProcessId
    $field = Find-UiElement $window $Name ([System.Windows.Automation.ControlType]::Edit)
    if (-not $field) { throw "worker_desktop_ui_field_unavailable" }
    try { $field.SetFocus() } catch { throw "worker_desktop_ui_field_unavailable" }
    if (-not $KeyboardOnly) {
        try {
            $pattern = $field.GetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern)
            $pattern.SetValue($Value)
            return
        } catch { }
    }
    [System.Windows.Forms.SendKeys]::SendWait("^a")
    [System.Windows.Forms.SendKeys]::SendWait("{BACKSPACE}")
    [System.Windows.Forms.SendKeys]::SendWait($Value)
}

function Set-LastUiEdit([int]$ProcessId, [string]$Value) {
    $window = Get-DesktopWindow $ProcessId
    if (-not $window) { throw "worker_desktop_ui_dialog_unavailable" }
    try {
        $edits = @($window.FindAll(
            [System.Windows.Automation.TreeScope]::Descendants,
            [System.Windows.Automation.PropertyCondition]::new(
                [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
                [System.Windows.Automation.ControlType]::Edit
            )
        ))
    } catch { $edits = @() }
    if ($edits.Count -eq 0) { throw "worker_desktop_ui_dialog_unavailable" }
    try { $edits[-1].SetFocus() } catch { throw "worker_desktop_ui_dialog_unavailable" }
    [System.Windows.Forms.SendKeys]::SendWait("^a")
    [System.Windows.Forms.SendKeys]::SendWait("{BACKSPACE}")
    [System.Windows.Forms.SendKeys]::SendWait($Value)
}

function Get-WorkerStatus([switch]$Record) {
    $status = Invoke-Fixture @("status", "--worker-id", [string]$script:Fixture.worker_id)
    if ($status.worker_id -ne $script:Fixture.worker_id) { throw "worker_desktop_fixture_worker_id_mismatch" }
    if ($Record) {
        $timeline = $script:Facts.worker_status_timeline
        if ($timeline.Count -eq 0 -or $timeline[$timeline.Count - 1] -ne [string]$status.status) {
            $timeline.Add([string]$status.status)
        }
        $script:Facts.status_counts_timeline.Add([ordered]@{
            status = [string]$status.status
            active_browser_sessions = [int]$status.active_browser_sessions
            running_worker_jobs = [int]$status.running_worker_jobs
            quiescent = [bool]$status.quiescent
        })
    }
    return $status
}

function Wait-WorkerStatus([string[]]$Expected, [int]$TimeoutSeconds = 45) {
    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    do {
        try {
            $status = Get-WorkerStatus -Record
            if ($status.status -in $Expected) { return $status }
        } catch { }
        Start-Sleep -Milliseconds 350
    } while ([DateTime]::UtcNow -lt $deadline)
    throw "worker_desktop_worker_status_timeout"
}

function Wait-ForNoWorker([int]$TimeoutSeconds = 20) {
    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    do {
        if ((Get-WorkerProcesses).Count -eq 0) { return $true }
        Start-Sleep -Milliseconds 200
    } while ([DateTime]::UtcNow -lt $deadline)
    return $false
}

function Wait-ForNoChromium([int]$TimeoutSeconds = 20) {
    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    do {
        if ((Get-ChromiumProcesses).Count -eq 0) { return $true }
        Start-Sleep -Milliseconds 200
    } while ([DateTime]::UtcNow -lt $deadline)
    return $false
}

function Get-FreeTcpPort([string]$Address = "127.0.0.1") {
    $listener = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Parse($Address), 0)
    try {
        $listener.Start()
        return [int]([Net.IPEndPoint]$listener.LocalEndpoint).Port
    } finally { $listener.Stop() }
}

function Wait-TcpPort([int]$Port, [int]$TimeoutSeconds = 20) {
    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    do {
        $client = [Net.Sockets.TcpClient]::new()
        try {
            $task = $client.ConnectAsync("127.0.0.1", $Port)
            if ($task.Wait(250) -and $client.Connected) { return $true }
        } catch { }
        finally { $client.Dispose() }
        Start-Sleep -Milliseconds 150
    } while ([DateTime]::UtcNow -lt $deadline)
    return $false
}

function Start-PostgresFixture {
    $postgres = Join-Path $RuntimeRoot "postgresql\bin"
    $initdb = Join-Path $postgres "initdb.exe"
    $pgCtl = Join-Path $postgres "pg_ctl.exe"
    $port = Get-FreeTcpPort
    $script:PgData = Join-Path $script:WorkRoot "postgres-data"
    $logPath = Join-Path $script:WorkRoot "postgres.internal.log"
    & $initdb -D $script:PgData -U $env:USERNAME --auth=trust --encoding=UTF8 --no-locale *> $null
    if ($LASTEXITCODE -ne 0) { throw "worker_desktop_fixture_database_init_failed" }
    & $pgCtl -D $script:PgData -l $logPath -o "-h 127.0.0.1 -p $port" start -w *> $null
    if ($LASTEXITCODE -ne 0) { throw "worker_desktop_fixture_database_start_failed" }
    $script:PgStarted = $true
    $script:DatabaseUrl = "postgresql+asyncpg://127.0.0.1:$port/postgres"
    $env:THREADS_PLATFORM_DATABASE_URL = $script:DatabaseUrl
    $env:PGUSER = $env:USERNAME
    $env:PGHOST = "127.0.0.1"
    $env:PGPORT = [string]$port
    if (-not (Wait-TcpPort $port 30)) { throw "worker_desktop_fixture_database_unavailable" }
}

function Start-ControlPlaneFixture {
    $script:ControlPort = Get-FreeTcpPort
    $script:FixtureRoot = Join-Path $script:WorkRoot "fixture"
    $script:DataRoot = Join-Path $script:WorkRoot "worker-data"
    New-Item -ItemType Directory -Path $script:FixtureRoot | Out-Null
    $script:Fixture = Invoke-Fixture @(
        "seed", "--fixture-root", $script:FixtureRoot, "--data-root", $script:DataRoot,
        "--control-port", [string]$script:ControlPort, "--operator-username", "dx07owner"
    ) -SeedOwner
    if ($script:Fixture.fixture_root -ne $script:FixtureRoot -or
        $script:Fixture.data_root -ne $script:DataRoot -or
        $script:Fixture.control_port -ne $script:ControlPort) {
        throw "worker_desktop_fixture_binding_invalid"
    }
    $script:Facts.worker_uuid = [string]$script:Fixture.worker_id
    $script:Facts.identity_marker_sha256 = [string]$script:Fixture.identity_marker_sha256
    $script:Facts.protected_key_sha256 = [string]$script:Fixture.protected_key_sha256
    $script:Facts.profile_sentinel_sha256 = [string]$script:Fixture.profile_sentinel_sha256
    $script:Facts.journal_sha256 = Get-Sha256 ([string]$script:Fixture.journal_path)
    $script:Facts.profile_tree_sha256 = Get-TreeSha256 ([string]$script:Fixture.profile_directory)
    $script:InitialDataTreeHash = Get-TreeSha256 $script:DataRoot
    $script:PageReleaseFile = Join-Path $script:WorkRoot "release-page.request"
    $script:DrainFaultFile = Join-Path $script:FixtureRoot "reject-drain-post.request"
    $script:StatusTimelineFile = Join-Path $script:FixtureRoot "status-observations.json"
    $null = Start-FixtureProcess "worker-status-observer" @(
        "watch-status", "--worker-id", [string]$script:Fixture.worker_id,
        "--output-path", $script:StatusTimelineFile, "--duration-seconds", "300"
    )

    $certificateBytes = [IO.File]::ReadAllBytes([string]$script:Fixture.tls.root_der)
    $script:RootCertificate = [Security.Cryptography.X509Certificates.X509Certificate2]::new($certificateBytes)
    $script:CertificateStore = [Security.Cryptography.X509Certificates.X509Store]::new(
        [Security.Cryptography.X509Certificates.StoreName]::Root,
        [Security.Cryptography.X509Certificates.StoreLocation]::CurrentUser
    )
    $script:CertificateStore.Open([Security.Cryptography.X509Certificates.OpenFlags]::ReadWrite)
    $script:CertificateStore.Add($script:RootCertificate)
    $script:CertificateStore.Close()

    $counterPath = Join-Path $script:FixtureRoot "request-counts.json"
    $script:ApiProcess = Start-FixtureProcess "controller-api" @(
        "serve-api", "--host", "127.0.0.1", "--port", [string]$script:ControlPort,
        "--certificate", [string]$script:Fixture.tls.leaf_certificate,
        "--private-key", [string]$script:Fixture.tls.leaf_private_key,
        "--request-counts", $counterPath, "--drain-fault-file", $script:DrainFaultFile
    )
    if (-not (Wait-TcpPort $script:ControlPort)) { throw "worker_desktop_fixture_control_plane_unavailable" }

    $appConfigDirectory = Join-Path $env:APPDATA "com.pumni.threads-desktop"
    $appLocalDirectory = Join-Path $env:LOCALAPPDATA "com.pumni.threads-desktop"
    if (Test-Path -LiteralPath $appConfigDirectory -or Test-Path -LiteralPath $appLocalDirectory) {
        throw "worker_desktop_profile_not_fresh"
    }
    New-Item -ItemType Directory -Path $appConfigDirectory -Force | Out-Null
    $deviceConfig = [ordered]@{
        schema_version = 1
        role = "WORKER"
        theme = "SYSTEM"
        autostart_enabled = $false
    }
    [IO.File]::WriteAllText(
        (Join-Path $appConfigDirectory "device-config.json"),
        ($deviceConfig | ConvertTo-Json -Compress),
        [Text.UTF8Encoding]::new($false)
    )
}

function Start-PageFixture {
    if (Wait-TcpPort 443 1) { throw "worker_desktop_fixture_profile_page_port_unavailable" }
    $script:PagePort = 443
    $script:PageProcess = Start-FixtureProcess "synthetic-threads-page" @(
        "serve-page", "--host", "127.0.0.1", "--port", [string]$script:PagePort,
        "--certificate", [string]$script:Fixture.tls.leaf_certificate,
        "--private-key", [string]$script:Fixture.tls.leaf_private_key,
        "--release-file", $script:PageReleaseFile, "--maximum-wait-seconds", "90"
    )
    if (-not (Wait-TcpPort $script:PagePort 5)) { throw "worker_desktop_fixture_profile_page_unavailable" }
}

function Restart-PageFixture {
    if ($script:PageProcess) {
        $script:PageProcess.Refresh()
        if (-not $script:PageProcess.HasExited) {
            Stop-Process -Id $script:PageProcess.Id -Force -ErrorAction SilentlyContinue
            if (-not $script:PageProcess.WaitForExit(10000)) {
                throw "worker_desktop_fixture_profile_page_stop_timeout"
            }
        }
        $script:PageProcess.Dispose()
        $script:PageProcess = $null
    }
    if (Test-Path -LiteralPath $script:PageReleaseFile) {
        Remove-Item -LiteralPath $script:PageReleaseFile -Force
    }
    Start-PageFixture
}

function Set-HostConfigRoot([string]$Root) {
    $path = [string]$script:Fixture.host_config_path
    $configuration = Get-Content -LiteralPath $path -Raw | ConvertFrom-Json -ErrorAction Stop
    $configuration.data_root = [IO.Path]::GetFullPath($Root)
    [IO.File]::WriteAllText(
        $path, ($configuration | ConvertTo-Json -Compress), [Text.UTF8Encoding]::new($false)
    )
}

function Get-TaskBinding {
    $inspection = Observe-Task
    if ($inspection.state -in @("INVALID", "NOT_REGISTERED") -or
        -not $inspection.same_user -or
        [IO.Path]::GetFullPath([string]$inspection.executable_path) -ne [IO.Path]::GetFullPath($WorkerExecutable) -or
        [IO.Path]::GetFullPath([string]$inspection.host_config_path) -ne [IO.Path]::GetFullPath([string]$script:Fixture.host_config_path) -or
        [IO.Path]::GetFullPath([string]$inspection.working_directory) -ne [IO.Path]::GetFullPath($WorkerReleaseDirectory)) {
        throw "worker_desktop_task_binding_invalid"
    }
    return $inspection
}

function Install-DisabledTask([switch]$TaskRunning) {
    $null = Invoke-TaskHelper "Install"
    $script:TaskInstalled = $true
    $inspection = Get-TaskBinding
    if ($inspection.state -ne "READY") { throw "worker_desktop_task_ready_expected" }
    if ($TaskRunning) {
        $null = Invoke-TaskHelper "Start"
        $inspection = Get-TaskBinding
        if ($inspection.state -ne "RUNNING") { throw "worker_desktop_task_start_failed" }
        $null = Wait-WorkerStatus @("ONLINE")
        return $inspection
    }
    $null = Invoke-TaskHelper "Disable" -ConfirmDrainOffline
    $inspection = Get-TaskBinding
    if ($inspection.state -ne "DISABLED") { throw "worker_desktop_task_disable_failed" }
    return $inspection
}

function Open-Desktop([switch]$ExpectStartupWorker) {
    $process = Start-Process -FilePath $DesktopExecutable -PassThru
    $deadline = [DateTime]::UtcNow.AddSeconds(30)
    do {
        $desktop = Get-DesktopProcess ([int]$process.Id)
        if ($desktop) {
            $script:Facts.process_ids.desktop = [int]$desktop.ProcessId
            $script:DesktopProcess = [Diagnostics.Process]::GetProcessById([int]$desktop.ProcessId)
            if (Get-DesktopWindow ([int]$desktop.ProcessId)) { break }
        }
        Start-Sleep -Milliseconds 250
    } while ([DateTime]::UtcNow -lt $deadline)
    if (-not $script:DesktopProcess -or -not (Get-DesktopWindow $script:DesktopProcess.Id)) {
        throw "worker_desktop_window_unavailable"
    }
    if ($ExpectStartupWorker) {
        $null = Wait-WorkerStatus @("ONLINE")
        $workers = Get-WorkerProcesses
        if ($workers.Count -eq 1) { $script:Facts.process_ids.worker = [int]$workers[0].ProcessId }
    }
    return $script:DesktopProcess.Id
}

function Wait-DesktopDiagnostic([string[]]$Codes, [int]$TimeoutSeconds = 15) {
    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    do {
        $names = Get-UiNames $script:DesktopProcess.Id
        foreach ($code in $Codes) {
            if ($names -contains $code) { return $code }
            if (@($names | Where-Object { $_.Contains($code, [StringComparison]::OrdinalIgnoreCase) }).Count -gt 0) {
                return $code
            }
        }
        Start-Sleep -Milliseconds 250
    } while ([DateTime]::UtcNow -lt $deadline)
    return $null
}

function Sign-InOperator {
    $processId = $script:DesktopProcess.Id
    if (-not (Wait-UiText $processId "Controller address" 20)) { throw "worker_desktop_operator_login_unavailable" }
    Set-UiField $processId "Controller address" ([string]$script:Fixture.control_plane_url)
    Invoke-UiButton $processId "Probe Controller identity"
    $fingerprint = [string]$script:Fixture.tls.root_fingerprint_sha256
    if (-not (Wait-UiText $processId "Confirm matching fingerprint" 20) -or
        -not (@(Get-UiNames $processId | Where-Object { $_.Contains($fingerprint, [StringComparison]::OrdinalIgnoreCase) }).Count -gt 0)) {
        throw "worker_desktop_controller_trust_probe_invalid"
    }
    Invoke-UiButton $processId "Confirm matching fingerprint"
    if (-not (Wait-UiText $processId "Sign in" 10)) { throw "worker_desktop_operator_login_unavailable" }
    Set-UiField $processId "Username" ([string]$script:Fixture.operator_username)
    Set-UiField $processId "Password" $script:OperatorPassword -KeyboardOnly
    Invoke-UiButton $processId "Sign in"
    $deadline = [DateTime]::UtcNow.AddSeconds(25)
    do {
        $sessions = Invoke-Fixture @("operator-session-count", "--operator-username", [string]$script:Fixture.operator_username)
        if ([int]$sessions.active_operator_sessions -gt 0) { return }
        Start-Sleep -Milliseconds 300
    } while ([DateTime]::UtcNow -lt $deadline)
    throw "worker_desktop_operator_session_not_authoritative"
}

function Get-RequestCounts {
    return Invoke-Fixture @("request-counts", "--counter-file", (Join-Path $script:FixtureRoot "request-counts.json"))
}

function Get-DrainAuditCounts {
    return Invoke-Fixture @("drain-audit-counts", "--worker-id", [string]$script:Fixture.worker_id)
}

function Read-StatusTimeline {
    if (-not (Test-Path -LiteralPath $script:StatusTimelineFile -PathType Leaf)) {
        throw "worker_desktop_authoritative_status_observer_missing"
    }
    try { $observations = @(Get-Content -LiteralPath $script:StatusTimelineFile -Raw | ConvertFrom-Json -ErrorAction Stop) }
    catch { throw "worker_desktop_authoritative_status_observer_invalid" }
    $script:Facts.worker_status_timeline.Clear()
    $script:Facts.status_counts_timeline.Clear()
    foreach ($item in $observations) {
        if ($item.status -notin @("REGISTERING", "ONLINE", "DEGRADED", "DRAINING", "OFFLINE", "DISABLED", "UPGRADE_REQUIRED")) {
            throw "worker_desktop_authoritative_status_observer_invalid"
        }
        $statusName = [string]$item.status
        if ($script:Facts.worker_status_timeline.Count -eq 0 -or
            $script:Facts.worker_status_timeline[$script:Facts.worker_status_timeline.Count - 1] -ne $statusName) {
            $script:Facts.worker_status_timeline.Add($statusName)
        }
        $script:Facts.status_counts_timeline.Add([ordered]@{
            status = $statusName
            active_browser_sessions = [int]$item.active_browser_sessions
            running_worker_jobs = [int]$item.running_worker_jobs
            quiescent = [bool]$item.quiescent
        })
    }
}

function Wait-ForJobStatus([string]$JobId, [string[]]$Expected, [int]$TimeoutSeconds = 25) {
    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    do {
        $status = Invoke-Fixture @("job-status", "--worker-job-id", $JobId)
        if ($status.worker_job_status -in $Expected) { return [string]$status.worker_job_status }
        Start-Sleep -Milliseconds 250
    } while ([DateTime]::UtcNow -lt $deadline)
    throw "worker_desktop_fixture_job_status_timeout"
}

function Queue-BlockedProfileJob {
    $job = Invoke-Fixture @(
        "queue-profile-job", "--worker-id", [string]$script:Fixture.worker_id,
        "--account-id", [string]$script:Fixture.account_id
    )
    if ([string]::IsNullOrWhiteSpace([string]$job.worker_job_id)) {
        throw "worker_desktop_fixture_job_enqueue_failed"
    }
    $script:ActiveJobId = [string]$job.worker_job_id
    $null = Wait-ForJobStatus $script:ActiveJobId @("RUNNING")
    $deadline = [DateTime]::UtcNow.AddSeconds(20)
    do {
        $status = Get-WorkerStatus -Record
        $chromium = Get-ChromiumProcesses
        if ($status.active_browser_sessions -gt 0 -and $chromium.Count -gt 0) {
            $script:Facts.process_ids.chromium.Clear()
            foreach ($process in $chromium) { $script:Facts.process_ids.chromium.Add([int]$process.ProcessId) }
            return $status
        }
        Start-Sleep -Milliseconds 200
    } while ([DateTime]::UtcNow -lt $deadline)
    throw "worker_desktop_fixture_headed_browser_unavailable"
}

function Release-BlockedPage {
    [IO.File]::WriteAllText($script:PageReleaseFile, "release", [Text.UTF8Encoding]::new($false))
}

function Add-OwnershipObservation([int]$ProcessId) {
    $names = Get-UiNames $ProcessId
    foreach ($state in @("LEGACY", "TAKEOVER_REQUIRED", "DESKTOP", "BLOCKED")) {
        if ($names -contains $state) {
            $timeline = $script:Facts.ownership_timeline
            if ($timeline.Count -eq 0 -or $timeline[$timeline.Count - 1] -ne $state) {
                $timeline.Add($state)
            }
            return $state
        }
    }
    return $null
}

function Get-OperatorSessionCount {
    $result = Invoke-Fixture @("operator-session-count", "--operator-username", [string]$script:Fixture.operator_username)
    return [int]$result.active_operator_sessions
}

function Get-IdentityEvidence {
    $markerPath = Join-Path $script:DataRoot "worker\worker_id"
    $markerText = Get-Content -LiteralPath $markerPath -Raw -ErrorAction Stop
    $workerId = ($markerText -split "`r?`n")[0]
    $keyPath = Join-Path (Join-Path $script:DataRoot "worker") "$workerId.device-key.dpapi"
    $hostConfig = Get-Content -LiteralPath ([string]$script:Fixture.host_config_path) -Raw | ConvertFrom-Json -ErrorAction Stop
    return [ordered]@{
        worker_id = $workerId.ToLowerInvariant()
        data_root = [IO.Path]::GetFullPath([string]$hostConfig.data_root)
        marker_hash = Get-Sha256 $markerPath
        key_hash = Get-Sha256 $keyPath
        data_tree_hash = Get-TreeSha256 $script:DataRoot
        journal_hash = Get-Sha256 ([string]$script:Fixture.journal_path)
        profile_tree_hash = Get-TreeSha256 ([string]$script:Fixture.profile_directory)
        profile_sentinel_hash = Get-Sha256 (Join-Path ([string]$script:Fixture.profile_directory) "dx07-profile-continuity.sentinel")
        host_config_hash = Get-Sha256 ([string]$script:Fixture.host_config_path)
    }
}

function Set-EvidenceIdentity([object]$Identity) {
    $script:Facts.worker_uuid = [string]$Identity.worker_id
    $script:Facts.identity_marker_sha256 = [string]$Identity.marker_hash
    $script:Facts.protected_key_sha256 = [string]$Identity.key_hash
    $script:Facts.journal_sha256 = $Identity.journal_hash
    $script:Facts.profile_tree_sha256 = $Identity.profile_tree_hash
    $script:Facts.profile_sentinel_sha256 = $Identity.profile_sentinel_hash
}

function Wait-DesktopExit([int]$ProcessId, [int]$TimeoutSeconds = 25) {
    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    do {
        if (-not (Get-DesktopProcess $ProcessId)) { return $true }
        Start-Sleep -Milliseconds 200
    } while ([DateTime]::UtcNow -lt $deadline)
    return $false
}

function Stop-DesktopAfterNoWorker {
    if (-not $script:DesktopProcess) { return }
    $workers = Get-WorkerProcesses
    if ($workers.Count -gt 0) { return }
    $desktopId = $script:DesktopProcess.Id
    if (Get-DesktopProcess $desktopId) {
        Stop-Process -Id $desktopId -Force -ErrorAction SilentlyContinue
        $null = Wait-DesktopExit $desktopId 10
    }
    $script:DesktopProcess.Dispose()
    $script:DesktopProcess = $null
}

function Submit-GracefulQuit([switch]$ExpectDesktopExit) {
    if (-not $script:DesktopProcess -or -not (Get-DesktopProcess $script:DesktopProcess.Id)) { return }
    Invoke-UiButton $script:DesktopProcess.Id "Quit…"
    Invoke-UiButton $script:DesktopProcess.Id "Gracefully drain and quit"
    if ($ExpectDesktopExit -and -not (Wait-DesktopExit $script:DesktopProcess.Id 45)) {
        throw "worker_desktop_quit_exit_timeout"
    }
}

function Complete-ScenarioDrainForCleanup {
    if (-not $script:DesktopProcess -or -not (Get-DesktopProcess $script:DesktopProcess.Id)) { return }
    $owners = Get-WorkerProcesses
    if ($owners.Count -eq 0) { return }
    $state = Add-OwnershipObservation $script:DesktopProcess.Id
    if ($state -eq "LEGACY" -or $state -eq "TAKEOVER_REQUIRED") {
        Invoke-UiButton $script:DesktopProcess.Id "Take over existing Worker" 8
        $null = Wait-WorkerStatus @("ONLINE")
    }
    if ($script:DesktopProcess) {
        $null = Sign-InOperator
        Submit-GracefulQuit -ExpectDesktopExit
    }
}

function Watch-LegacyCutover([string]$InitialTaskState, [int]$OriginalWorkerPid = 0) {
    $deadline = [DateTime]::UtcNow.AddSeconds(120)
    $seenLegacyOnline = $InitialTaskState -eq "RUNNING"
    $seenDraining = $false
    $seenOffline = $false
    $seenOldExit = $false
    $seenLockFree = $false
    $seenDisabled = $false
    $newWorkerPid = 0
    $lastTaskCheck = [DateTime]::MinValue
    do {
        $status = $null
        try { $status = Get-WorkerStatus -Record } catch { }
        if ($status) {
            if ($status.status -eq "ONLINE" -and -not $seenDraining) { $seenLegacyOnline = $true }
            if ($status.status -eq "DRAINING") { $seenDraining = $true }
            if ($status.status -eq "OFFLINE") {
                $seenOffline = $true
                $audits = Get-DrainAuditCounts
                if ([int]$audits.drain_completion_audit_count -lt 1) { $seenOffline = $false }
            }
        }
        $workers = Get-WorkerProcesses
        if ($OriginalWorkerPid -eq 0 -and $workers.Count -eq 1 -and -not $seenDraining) {
            $OriginalWorkerPid = [int]$workers[0].ProcessId
        }
        if ($OriginalWorkerPid -gt 0 -and -not ($workers | Where-Object { [int]$_.ProcessId -eq $OriginalWorkerPid })) {
            if ($seenOffline) { $seenOldExit = $true }
        }
        $lockState = Observe-Lock
        if ($seenOffline -and $seenOldExit -and $lockState -eq "NOT_HELD") { $seenLockFree = $true }

        if (([DateTime]::UtcNow - $lastTaskCheck).TotalMilliseconds -ge 500) {
            $task = Get-TaskBinding
            $lastTaskCheck = [DateTime]::UtcNow
            if ($task.state -eq "DISABLED") {
                if (-not ($seenOffline -and $seenOldExit -and $seenLockFree)) {
                    throw "worker_desktop_task_disabled_before_offline_exit_lock_release"
                }
                $seenDisabled = $true
            }
        }
        $workers = Get-WorkerProcesses
        if ($workers.Count -gt 1) { throw "worker_desktop_duplicate_worker_process" }
        if ($workers.Count -eq 1 -and $seenDisabled -and [int]$workers[0].ProcessId -ne $OriginalWorkerPid) {
            $newWorkerPid = [int]$workers[0].ProcessId
            $script:Facts.process_ids.worker = $newWorkerPid
            try { $status = Get-WorkerStatus -Record } catch { $status = $null }
            if ($status -and $status.status -eq "ONLINE") { break }
        }
        $null = Add-OwnershipObservation $script:DesktopProcess.Id
        $diagnostic = Wait-DesktopDiagnostic @(
            "worker_cutover_rollback_required", "worker_drain_unavailable", "worker_drain_request_failed",
            "worker_process_exited", "worker_process_lock_not_acquired", "worker_identity_corrupt"
        ) 1
        if ($diagnostic) { throw "worker_desktop_cutover_diagnostic_failure" }
        Start-Sleep -Milliseconds 150
    } while ([DateTime]::UtcNow -lt $deadline)

    $counts = Get-RequestCounts
    $script:Facts.drain_post_count = [int]$counts.drain_post_count
    $script:Facts.drain_status_get_count = [int]$counts.drain_status_get_count
    Assert-ScenarioCheck "drain_post_once" ($script:Facts.drain_post_count -eq 1) "worker_desktop_cutover_post_count_invalid"
    Assert-ScenarioCheck "authoritative_offline" $seenOffline "worker_desktop_cutover_offline_not_observed"
    Assert-ScenarioCheck "legacy_natural_exit" $seenOldExit "worker_desktop_legacy_exit_not_observed"
    Assert-ScenarioCheck "process_lock_released" $seenLockFree "worker_desktop_process_lock_not_released"
    Assert-ScenarioCheck "task_disabled_after_offline" $seenDisabled "worker_desktop_task_disable_order_invalid"
    Assert-ScenarioCheck "exact_desktop_worker_started" ($newWorkerPid -gt 0) "worker_desktop_process_not_started"
    Assert-ScenarioCheck "desktop_online" ($null -ne $status -and $status.status -eq "ONLINE") "worker_desktop_online_not_observed"
    Assert-ScenarioCheck "single_owner" ((Get-WorkerProcesses).Count -eq 1 -and (Observe-Lock) -eq "HELD") "worker_desktop_single_owner_invalid"
    if ($InitialTaskState -eq "RUNNING") {
        Assert-ScenarioCheck "legacy_running_observed" $true "worker_desktop_legacy_running_not_observed"
    } else {
        Assert-ScenarioCheck "legacy_ready_observed" ($InitialTaskState -eq "READY") "worker_desktop_legacy_ready_not_observed"
        Assert-ScenarioCheck "legacy_task_started_first" ($seenLegacyOnline -and $OriginalWorkerPid -gt 0) "worker_desktop_legacy_not_started_first"
    }
}

function Test-ExactWorkerLaunch([object]$Process) {
    $inspection = Get-TaskBinding
    $executable = [IO.Path]::GetFullPath([string]$inspection.executable_path)
    $configuration = [IO.Path]::GetFullPath([string]$inspection.host_config_path)
    $escapedConfiguration = [regex]::Escape($configuration)
    $expectedCommandLine = '^"' + [regex]::Escape($executable) + '"\s+--host-config\s+(?:"' +
        $escapedConfiguration + '"|' + $escapedConfiguration + ')$'
    return $executable.Equals([IO.Path]::GetFullPath($WorkerExecutable), [StringComparison]::OrdinalIgnoreCase) -and
        $configuration.Equals([IO.Path]::GetFullPath([string]$script:Fixture.host_config_path), [StringComparison]::OrdinalIgnoreCase) -and
        ([string]$Process.CommandLine).Trim() -match $expectedCommandLine -and
        $inspection.same_user -eq $true
}

function Test-LegacyCutover([ValidateSet("RUNNING", "READY")][string]$InitialTaskState) {
    $identityBefore = Get-IdentityEvidence
    $task = if ($InitialTaskState -eq "RUNNING") {
        Install-DisabledTask -TaskRunning
    } else {
        $null = Invoke-TaskHelper "Install"
        $script:TaskInstalled = $true
        Get-TaskBinding
    }
    if ($task.state -ne $InitialTaskState) { throw "worker_desktop_legacy_task_state_mismatch" }
    $legacyProcesses = Get-WorkerProcesses
    if ($InitialTaskState -eq "RUNNING" -and $legacyProcesses.Count -ne 1) {
        throw "worker_desktop_legacy_worker_process_missing"
    }
    if ($InitialTaskState -eq "READY" -and $legacyProcesses.Count -ne 0) {
        throw "worker_desktop_ready_task_started_without_takeover"
    }

    $desktopId = Open-Desktop
    $null = Add-OwnershipObservation $desktopId
    Sign-InOperator
    if ($InitialTaskState -eq "RUNNING") {
        $originalPid = [int]$legacyProcesses[0].ProcessId
        Assert-ScenarioCheck "legacy_running_observed" ($task.state -eq "RUNNING" -and $originalPid -gt 0) "worker_desktop_legacy_running_not_observed"
    } else {
        $originalPid = 0
        Assert-ScenarioCheck "legacy_ready_observed" ($task.state -eq "READY") "worker_desktop_legacy_ready_not_observed"
    }

    Invoke-UiButton $desktopId "Take over existing Worker"
    Watch-LegacyCutover $InitialTaskState $originalPid

    $identityAfter = Get-IdentityEvidence
    Assert-ScenarioCheck "same_identity_and_data_root" (
        $identityBefore.worker_id -eq $identityAfter.worker_id -and
        $identityBefore.marker_hash -eq $identityAfter.marker_hash -and
        $identityBefore.key_hash -eq $identityAfter.key_hash -and
        $identityBefore.host_config_hash -eq $identityAfter.host_config_hash -and
        [IO.Path]::GetFullPath([string]$task.host_config_path) -eq [IO.Path]::GetFullPath([string]$script:Fixture.host_config_path)
    ) "worker_desktop_identity_changed_during_cutover"
    $desktopWorkers = Get-WorkerProcesses
    Assert-ScenarioCheck "exact_desktop_worker_started" ($desktopWorkers.Count -eq 1 -and (Test-ExactWorkerLaunch $desktopWorkers[0])) "worker_desktop_launch_binding_invalid"
    $script:Facts.journal_sha256 = $identityAfter.journal_hash
    $script:Facts.profile_tree_sha256 = $identityAfter.profile_tree_hash
    $script:Facts.profile_sentinel_sha256 = $identityAfter.profile_sentinel_hash
}

function Test-DisabledDesktopStart {
    $identityBefore = Get-IdentityEvidence
    $task = Install-DisabledTask
    $lockBefore = Observe-Lock
    Assert-ScenarioCheck "process_lock_free_before_start" ($lockBefore -eq "NOT_HELD") "worker_desktop_process_lock_unavailable_before_start"
    $desktopId = Open-Desktop -ExpectStartupWorker
    $taskAfter = Get-TaskBinding
    $workers = Get-AnyWorkerProcesses
    $identityAfter = Get-IdentityEvidence
    Assert-ScenarioCheck "task_disabled_observed" ($task.state -eq "DISABLED") "worker_desktop_task_not_disabled"
    Assert-ScenarioCheck "enrolled_identity_unchanged" (
        $identityBefore.worker_id -eq $identityAfter.worker_id -and
        $identityBefore.marker_hash -eq $identityAfter.marker_hash -and
        $identityBefore.key_hash -eq $identityAfter.key_hash
    ) "worker_desktop_identity_changed_during_start"
    Assert-ScenarioCheck "exact_package_and_host_config" ($workers.Count -eq 1 -and (Test-ExactWorkerLaunch $workers[0])) "worker_desktop_launch_binding_invalid"
    Assert-ScenarioCheck "desktop_online" ((Wait-WorkerStatus @("ONLINE")).status -eq "ONLINE") "worker_desktop_online_not_observed"
    Assert-ScenarioCheck "task_remains_disabled" ($taskAfter.state -eq "DISABLED") "worker_desktop_task_state_changed"
    Assert-ScenarioCheck "single_owner" ($workers.Count -eq 1 -and (Observe-Lock) -eq "HELD") "worker_desktop_single_owner_invalid"
    $null = Add-OwnershipObservation $desktopId
    Set-EvidenceIdentity $identityAfter
}

function Test-NoTaskFailClosed {
    $identityBefore = Get-IdentityEvidence
    $desktopId = Open-Desktop
    $null = Add-OwnershipObservation $desktopId
    $diagnostic = Wait-DesktopDiagnostic @("worker_legacy_task_not_registered") 20
    $task = Observe-Task
    $workers = Get-AnyWorkerProcesses
    $identityAfter = Get-IdentityEvidence
    Assert-ScenarioCheck "not_registered_diagnostic" ($diagnostic -eq "worker_legacy_task_not_registered") "worker_desktop_not_registered_diagnostic_missing"
    Assert-ScenarioCheck "no_executable_discovery" ($workers.Count -eq 0) "worker_desktop_unexpected_worker_spawn"
    Assert-ScenarioCheck "identity_unchanged" (
        $identityBefore.worker_id -eq $identityAfter.worker_id -and
        $identityBefore.marker_hash -eq $identityAfter.marker_hash -and
        $identityBefore.key_hash -eq $identityAfter.key_hash -and
        $identityBefore.data_tree_hash -eq $identityAfter.data_tree_hash
    ) "worker_desktop_identity_or_data_mutated"
    Assert-ScenarioCheck "no_task_created" ($task.state -eq "NOT_REGISTERED") "worker_desktop_task_was_created"
    Assert-ScenarioCheck "no_worker_spawned" ($workers.Count -eq 0) "worker_desktop_unexpected_worker_spawn"
}

function Stop-DesktopWithoutWorker {
    Stop-DesktopAfterNoWorker
}

function Test-IdentityFailureCase([string]$Kind, [string]$ExpectedDiagnostic) {
    $workerDirectory = Join-Path $script:DataRoot "worker"
    $identityBefore = Get-IdentityEvidence
    $markerPath = Join-Path $workerDirectory "worker_id"
    $originalMarker = [IO.File]::ReadAllBytes($markerPath)
    $originalKeyPath = Join-Path $workerDirectory "$($script:Fixture.worker_id).device-key.dpapi"
    $originalKey = [IO.File]::ReadAllBytes($originalKeyPath)
    try {
        switch ($Kind) {
            "missing" {
                Remove-Item -LiteralPath $originalKeyPath -Force
            }
            "corrupt" {
                $corrupt = [byte[]]$originalKey.Clone()
                $corrupt[$corrupt.Length - 1] = $corrupt[$corrupt.Length - 1] -bxor 0x5A
                [IO.File]::WriteAllBytes($originalKeyPath, $corrupt)
            }
            "mismatched" {
                $newId = [guid]::NewGuid().ToString()
                [IO.File]::WriteAllText($markerPath, "$newId`nENROLLED`n", [Text.UTF8Encoding]::new($false))
            }
            "wrong_user" {
                Set-HostConfigRoot $WrongUserDataRoot
                $script:DataRoot = [IO.Path]::GetFullPath($WrongUserDataRoot)
            }
        }
        $mutatedRoot = $script:DataRoot
        $beforeFailureHash = Get-TreeSha256 $mutatedRoot
        $desktopId = Open-Desktop
        $diagnostic = Wait-DesktopDiagnostic @($ExpectedDiagnostic, "worker_identity_missing", "worker_identity_corrupt", "worker_device_key_unprotect_failed")
        $workers = Get-WorkerProcesses
        $afterFailureHash = Get-TreeSha256 $mutatedRoot
        $actualExpected = $diagnostic -eq $ExpectedDiagnostic -or
            ($Kind -eq "mismatched" -and $diagnostic -eq "worker_identity_missing") -or
            ($Kind -eq "corrupt" -and $diagnostic -in @("worker_identity_corrupt", "worker_device_key_unprotect_failed"))
        Assert-ScenarioCheck "no_worker_spawned" ($workers.Count -eq 0) "worker_desktop_identity_preflight_spawned_worker"
        if (-not $actualExpected) { throw "worker_desktop_identity_preflight_diagnostic_invalid" }
        if ($beforeFailureHash -ne $afterFailureHash) { throw "worker_desktop_identity_preflight_mutated_data" }
        Stop-DesktopWithoutWorker
    } finally {
        if ($Kind -eq "missing" -and -not (Test-Path -LiteralPath $originalKeyPath)) {
            [IO.File]::WriteAllBytes($originalKeyPath, $originalKey)
        }
        if ($Kind -eq "corrupt") { [IO.File]::WriteAllBytes($originalKeyPath, $originalKey) }
        if ($Kind -eq "mismatched") { [IO.File]::WriteAllBytes($markerPath, $originalMarker) }
        if ($Kind -eq "wrong_user") {
            Set-HostConfigRoot $script:Fixture.data_root
            $script:DataRoot = [IO.Path]::GetFullPath([string]$script:Fixture.data_root)
        }
        $null = $identityBefore
        $null = $originalKey
    }
}

function Test-IdentityKeyFailClosed {
    $null = Install-DisabledTask
    Test-IdentityFailureCase "missing" "worker_identity_missing"
    $script:Checks.missing_key_rejected = $true
    Test-IdentityFailureCase "corrupt" "worker_device_key_unprotect_failed"
    $script:Checks.corrupt_key_rejected = $true
    Test-IdentityFailureCase "mismatched" "worker_identity_missing"
    $script:Checks.mismatched_identity_rejected = $true
    Test-IdentityFailureCase "wrong_user" "worker_device_key_unprotect_failed"
    $script:Checks.wrong_user_unprotect_rejected = $true
    $final = Get-IdentityEvidence
    Assert-ScenarioCheck "no_replacement_identity_or_key" (
        $final.worker_id -eq $script:Fixture.worker_id -and
        $final.marker_hash -eq $script:Fixture.identity_marker_sha256 -and
        $final.key_hash -eq $script:Fixture.protected_key_sha256
    ) "worker_desktop_identity_or_key_replaced"
    Assert-ScenarioCheck "durable_data_unchanged" ($final.data_tree_hash -eq $script:InitialDataTreeHash) "worker_desktop_durable_data_changed"
    $workers = Get-WorkerProcesses
    Assert-ScenarioCheck "no_worker_spawned" ($workers.Count -eq 0) "worker_desktop_identity_preflight_spawned_worker"
    Set-EvidenceIdentity $final
}

function Test-DuplicateProcessLock {
    $identityBefore = Get-IdentityEvidence
    $taskDisabled = Install-DisabledTask
    if ($taskDisabled.state -ne "DISABLED") { throw "worker_desktop_task_disable_failed" }
    $external = Start-Process -FilePath $WorkerExecutable `
        -ArgumentList @("--host-config", (ConvertTo-StartProcessArgument ([string]$script:Fixture.host_config_path))) `
        -PassThru -WindowStyle Hidden
    $script:ExternalWorkerPid = [int]$external.Id
    $null = Wait-WorkerStatus @("ONLINE")
    $externalProcesses = Get-WorkerProcesses
    if ($externalProcesses.Count -ne 1 -or [int]$externalProcesses[0].ProcessId -ne $script:ExternalWorkerPid) {
        throw "worker_desktop_fixture_existing_worker_missing"
    }
    $lockHeld = Observe-Lock
    $journalBeforeDesktop = Get-Sha256 ([string]$script:Fixture.journal_path)
    $desktopId = Open-Desktop
    $null = Add-OwnershipObservation $desktopId
    $diagnostic = Wait-DesktopDiagnostic @("worker_process_lock_held") 20
    Start-Sleep -Milliseconds 750
    $workers = Get-WorkerProcesses
    $journalAfterDesktop = Get-Sha256 ([string]$script:Fixture.journal_path)
    $lockAfter = Observe-Lock
    $taskAfterDesktop = Get-TaskBinding
    Assert-ScenarioCheck "existing_worker_lock_held" ($lockHeld -eq "HELD" -and $lockAfter -eq "HELD") "worker_desktop_lock_observation_invalid"
    Assert-ScenarioCheck "desktop_start_blocked" ($diagnostic -eq "worker_process_lock_held") "worker_desktop_lock_did_not_block_start"
    Assert-ScenarioCheck "one_worker_process_only" ($workers.Count -eq 1 -and [int]$workers[0].ProcessId -eq $script:ExternalWorkerPid) "worker_desktop_duplicate_worker_spawned"
    Assert-ScenarioCheck "journal_unchanged" ($journalBeforeDesktop -and $journalBeforeDesktop -eq $journalAfterDesktop) "worker_desktop_journal_changed_on_duplicate_start"
    Assert-ScenarioCheck "no_second_owner" (
        $workers.Count -eq 1 -and $lockAfter -eq "HELD" -and
        $taskDisabled.state -eq "DISABLED" -and $taskAfterDesktop.state -eq "DISABLED"
    ) "worker_desktop_duplicate_owner_observed"
    Set-EvidenceIdentity $identityBefore
    $script:Facts.process_ids.worker = $script:ExternalWorkerPid
}

function Test-GracefulBusyDrainQuit {
    $null = Install-DisabledTask
    $null = Start-PageFixture
    $desktopId = Open-Desktop -ExpectStartupWorker
    $script:Facts.process_ids.worker = [int](Get-WorkerProcesses)[0].ProcessId
    Sign-InOperator
    $busy = Queue-BlockedProfileJob
    $chromiumBefore = Get-ChromiumProcesses
    $script:Facts.process_ids.chromium.Clear()
    foreach ($process in $chromiumBefore) { $script:Facts.process_ids.chromium.Add([int]$process.ProcessId) }
    if ($busy.active_browser_sessions -lt 1 -or $busy.running_worker_jobs -lt 1 -or
        $busy.quiescent -or $chromiumBefore.Count -eq 0) {
        throw "worker_desktop_busy_browser_fixture_not_established"
    }
    $workerHandle = [Diagnostics.Process]::GetProcessById([int]$script:Facts.process_ids.worker)
    Invoke-UiButton $desktopId "Quit…"
    Invoke-UiButton $desktopId "Gracefully drain and quit"
    $busyDrain = $null
    $sessionsDuringDrain = 0
    $deadline = [DateTime]::UtcNow.AddSeconds(20)
    do {
        try {
            $candidate = Get-WorkerStatus -Record
            if ($candidate.status -eq "DRAINING") {
                $busyDrain = $candidate
                $sessionsDuringDrain = Get-OperatorSessionCount
                break
            }
        } catch { }
        Start-Sleep -Milliseconds 150
    } while ([DateTime]::UtcNow -lt $deadline)
    $requestCounts = Get-RequestCounts
    Assert-ScenarioCheck "operator_session_retained_until_offline" ($busyDrain -and $sessionsDuringDrain -gt 0) "worker_desktop_operator_logged_out_before_offline"
    Assert-ScenarioCheck "busy_draining_observed" (
        $busyDrain -and $busyDrain.active_browser_sessions -gt 0 -and
        $busyDrain.running_worker_jobs -gt 0 -and -not $busyDrain.quiescent
    ) "worker_desktop_busy_drain_not_observed"
    Release-BlockedPage
    $offline = Wait-WorkerStatus @("OFFLINE") 45
    $deadline = [DateTime]::UtcNow.AddSeconds(45)
    do {
        $workerGone = (Get-WorkerProcesses).Count -eq 0
        $lockState = Observe-Lock
        $chromiumGone = (Get-ChromiumProcesses).Count -eq 0
        $audit = Get-DrainAuditCounts
        if ($workerGone -and $lockState -eq "NOT_HELD" -and $chromiumGone -and
            [int]$audit.drain_completion_audit_count -eq 1) { break }
        Start-Sleep -Milliseconds 200
    } while ([DateTime]::UtcNow -lt $deadline)
    $script:Facts.drain_post_count = [int]$requestCounts.drain_post_count
    $script:Facts.drain_status_get_count = [int]$requestCounts.drain_status_get_count
    $script:Facts.process_ids.worker = [int]$workerHandle.Id
    Assert-ScenarioCheck "drain_post_once" ($requestCounts.drain_post_count -eq 1) "worker_desktop_drain_post_count_invalid"
    Assert-ScenarioCheck "status_get_only_polling" ($requestCounts.drain_status_get_count -gt 0 -and $requestCounts.drain_post_count -eq 1) "worker_desktop_drain_polling_invalid"
    Assert-ScenarioCheck "counts_and_quiescence_observed" (
        $busyDrain.active_browser_sessions -gt 0 -and $busyDrain.running_worker_jobs -gt 0 -and
        -not $busyDrain.quiescent -and $offline.active_browser_sessions -eq 0 -and
        $offline.running_worker_jobs -eq 0 -and -not $offline.quiescent
    ) "worker_desktop_drain_counts_invalid"
    $audit = Get-DrainAuditCounts
    Assert-ScenarioCheck "authoritative_offline" (
        $offline.status -eq "OFFLINE" -and [int]$audit.drain_request_audit_count -eq 1 -and
        [int]$audit.drain_completion_audit_count -eq 1
    ) "worker_desktop_offline_not_authoritative"
    Assert-ScenarioCheck "worker_natural_exit" (
        (Get-WorkerProcesses).Count -eq 0 -and $workerGone -and $workerHandle.HasExited -and
        $workerHandle.ExitCode -eq 0
    ) "worker_desktop_worker_did_not_exit_naturally"
    Assert-ScenarioCheck "chromium_natural_exit" ($chromiumGone -and (Get-ChromiumProcesses).Count -eq 0) "worker_desktop_chromium_did_not_exit"
    Assert-ScenarioCheck "process_lock_released" ((Observe-Lock) -eq "NOT_HELD") "worker_desktop_process_lock_not_released"
    Assert-ScenarioCheck "no_job_object_termination" (
        [int]$audit.drain_completion_audit_count -eq 1 -and $workerHandle.HasExited -and $workerHandle.ExitCode -eq 0
    ) "worker_desktop_graceful_stop_used_abnormal_termination"
    if (-not (Wait-DesktopExit $desktopId 45)) { throw "worker_desktop_quit_exit_timeout" }
    $sessionAfterExit = Get-OperatorSessionCount
    if ($sessionAfterExit -ne 0) { throw "worker_desktop_operator_logout_after_exit_invalid" }
    $workerHandle.Dispose()
}

function Test-DrainFailureForceInterrupt {
    $identityBefore = Get-IdentityEvidence
    $task = Install-DisabledTask
    $desktopId = Open-Desktop -ExpectStartupWorker
    Sign-InOperator
    $workerPid = [int](Get-WorkerProcesses)[0].ProcessId
    $script:Facts.process_ids.worker = $workerPid
    $countsBeforeDrain = Get-RequestCounts
    New-Item -ItemType File -Path $script:DrainFaultFile | Out-Null
    Invoke-UiButton $desktopId "Quit…"
    Invoke-UiButton $desktopId "Gracefully drain and quit"
    $drainFailure = Wait-DesktopDiagnostic @("worker_drain_request_failed") 20
    $workerStillAlive = (Get-WorkerProcesses).Count -eq 1
    $desktopStillAlive = $null -ne (Get-DesktopProcess $desktopId)
    $taskStillDisabled = (Get-TaskBinding).state -eq "DISABLED"
    $countsBeforeForce = Get-RequestCounts
    $statusBeforeForce = Get-WorkerStatus -Record
    $initialAudit = Get-DrainAuditCounts
    Assert-ScenarioCheck "graceful_failure_intervention" ($null -ne $drainFailure -and $desktopStillAlive) "worker_desktop_graceful_failure_not_reported"
    Assert-ScenarioCheck "operator_api_live_after_drain_failure" (
        [int]$countsBeforeForce.operator_me_get_count -gt [int]$countsBeforeDrain.operator_me_get_count -and
        $statusBeforeForce.status -eq "ONLINE" -and
        [int]$initialAudit.drain_completion_audit_count -eq 0
    ) "worker_desktop_operator_api_or_worker_state_unavailable_after_drain_fault"
    Assert-ScenarioCheck "no_automatic_force_fallback" (
        $workerStillAlive -and [int]$countsBeforeForce.drain_post_count -eq 1 -and
        [int]$initialAudit.drain_completion_audit_count -eq 0
    ) "worker_desktop_automatic_force_or_post_observed"
    if (-not $desktopStillAlive -or -not $workerStillAlive -or -not $taskStillDisabled) {
        throw "worker_desktop_force_precondition_invalid"
    }
    Invoke-UiButton $desktopId "Cancel"
    $forceButtonReady = Wait-UiText $desktopId "Force stop Worker (abnormal)…" 15
    if (-not $forceButtonReady) { throw "worker_desktop_force_intervention_unavailable" }
    Invoke-UiButton $desktopId "Force stop Worker (abnormal)…"
    $dialogNames = Get-UiNames $desktopId
    $phraseRequired = @($dialogNames | Where-Object { $_.Contains("FORCE STOP WORKER", [StringComparison]::Ordinal) }).Count -gt 0
    $forceButton = Find-UiElement (Get-DesktopWindow $desktopId) "Force stop Worker" ([System.Windows.Automation.ControlType]::Button)
    $phraseInitiallyDisabled = $forceButton -and -not $forceButton.Current.IsEnabled
    Set-LastUiEdit $desktopId "FORCE"
    $forceButton = Find-UiElement (Get-DesktopWindow $desktopId) "Force stop Worker" ([System.Windows.Automation.ControlType]::Button)
    $wrongPhraseDisabled = $forceButton -and -not $forceButton.Current.IsEnabled
    Set-LastUiEdit $desktopId "FORCE STOP WORKER"
    $meCountBeforeForce = [int](Get-RequestCounts).operator_me_get_count
    Invoke-UiButton $desktopId "Force stop Worker"
    $deadline = [DateTime]::UtcNow.AddSeconds(15)
    do {
        if ((Get-WorkerProcesses).Count -eq 0 -and (Get-ChromiumProcesses).Count -eq 0) { break }
        Start-Sleep -Milliseconds 200
    } while ([DateTime]::UtcNow -lt $deadline)
    $afterForce = Get-IdentityEvidence
    Start-Sleep -Milliseconds 500
    $statusAfterForce = Get-WorkerStatus -Record
    $finalAudit = Get-DrainAuditCounts
    $taskAfterForce = Get-TaskBinding
    $diagnostic = Wait-DesktopDiagnostic @("worker_forced_interruption") 10
    $requestCounts = Get-RequestCounts
    $script:Facts.drain_post_count = [int]$requestCounts.drain_post_count
    $script:Facts.drain_status_get_count = [int]$requestCounts.drain_status_get_count
    $script:Facts.operator_me_get_count = [int]$requestCounts.operator_me_get_count
    Assert-ScenarioCheck "exact_force_phrase_required" ($phraseRequired -and $phraseInitiallyDisabled -and $wrongPhraseDisabled) "worker_desktop_force_confirmation_not_strong"
    Assert-ScenarioCheck "explicit_force_operation_invoked" ((Get-WorkerProcesses).Count -eq 0 -and (Get-DesktopProcess $desktopId)) "worker_desktop_force_operation_not_observed"
    Assert-ScenarioCheck "offline_not_fabricated" ($statusAfterForce.status -ne "OFFLINE") "worker_desktop_force_fabricated_offline"
    Assert-ScenarioCheck "drain_complete_not_fabricated" ([int]$finalAudit.drain_completion_audit_count -eq [int]$initialAudit.drain_completion_audit_count) "worker_desktop_force_completed_drain"
    Assert-ScenarioCheck "legacy_task_remains_disabled" ($task.state -eq "DISABLED" -and $taskAfterForce.state -eq "DISABLED") "worker_desktop_force_changed_legacy_task"
    Assert-ScenarioCheck "identity_and_data_preserved" (
        $identityBefore.worker_id -eq $afterForce.worker_id -and
        $identityBefore.marker_hash -eq $afterForce.marker_hash -and
        $identityBefore.key_hash -eq $afterForce.key_hash
    ) "worker_desktop_force_changed_identity_or_data"
    Assert-ScenarioCheck "process_tree_terminated" ((Get-WorkerProcesses).Count -eq 0 -and (Get-ChromiumProcesses).Count -eq 0) "worker_desktop_force_left_process_tree"
    Assert-ScenarioCheck "forced_interruption_diagnostic" ($diagnostic -eq "worker_forced_interruption") "worker_desktop_force_diagnostic_missing"
    Assert-ScenarioCheck "force_authorization_rechecked" (
        [int]$requestCounts.operator_me_get_count -gt $meCountBeforeForce -and
        (Get-DesktopProcess $desktopId) -and
        (Get-WorkerProcesses).Count -eq 0
    ) "worker_desktop_force_authorization_not_rechecked"
}

function Watch-DesktopRollback([int]$DesktopWorkerPid) {
    $deadline = [DateTime]::UtcNow.AddSeconds(120)
    $seenOffline = $false
    $seenDesktopExit = $false
    $seenLockFree = $false
    $seenReadyAfterRelease = $false
    $seenRunningAfterReady = $false
    $legacyPid = 0
    $lastTaskCheck = [DateTime]::MinValue
    $status = $null
    do {
        try { $status = Get-WorkerStatus -Record } catch { }
        if ($status -and $status.status -eq "OFFLINE") {
            $audits = Get-DrainAuditCounts
            if ([int]$audits.drain_completion_audit_count -gt 0) { $seenOffline = $true }
        }
        $workers = Get-WorkerProcesses
        if ($DesktopWorkerPid -gt 0 -and -not ($workers | Where-Object { [int]$_.ProcessId -eq $DesktopWorkerPid })) {
            if ($seenOffline) { $seenDesktopExit = $true }
        }
        $lock = Observe-Lock
        if ($seenOffline -and $seenDesktopExit -and $lock -eq "NOT_HELD") { $seenLockFree = $true }

        if (([DateTime]::UtcNow - $lastTaskCheck).TotalMilliseconds -ge 500) {
            $task = Get-TaskBinding
            $lastTaskCheck = [DateTime]::UtcNow
            if ($task.state -in @("READY", "RUNNING")) {
                if (-not ($seenOffline -and $seenDesktopExit -and $seenLockFree)) {
                    throw "worker_desktop_task_enabled_before_offline_exit_lock_release"
                }
                $seenReadyAfterRelease = $true
            }
            if ($task.state -eq "RUNNING" -and $seenReadyAfterRelease) { $seenRunningAfterReady = $true }
        }
        $workers = Get-WorkerProcesses
        if ($workers.Count -gt 1) { throw "worker_desktop_duplicate_worker_process" }
        if ($workers.Count -eq 1 -and [int]$workers[0].ProcessId -ne $DesktopWorkerPid) {
            if (-not $seenRunningAfterReady) { throw "worker_desktop_legacy_started_before_enable" }
            $legacyPid = [int]$workers[0].ProcessId
            $script:Facts.process_ids.worker = $legacyPid
            try { $status = Get-WorkerStatus -Record } catch { }
            if ($status -and $status.status -in @("ONLINE", "DEGRADED")) { break }
        }
        $null = Add-OwnershipObservation $script:DesktopProcess.Id
        Start-Sleep -Milliseconds 150
    } while ([DateTime]::UtcNow -lt $deadline)

    $counts = Get-RequestCounts
    $script:Facts.drain_post_count = [int]$counts.drain_post_count
    $script:Facts.drain_status_get_count = [int]$counts.drain_status_get_count
    $audits = Get-DrainAuditCounts
    Assert-ScenarioCheck "desktop_drain_post_once" ($script:Facts.drain_post_count -eq 1) "worker_desktop_rollback_post_count_invalid"
    Assert-ScenarioCheck "authoritative_offline" $seenOffline "worker_desktop_rollback_offline_not_observed"
    Assert-ScenarioCheck "desktop_natural_exit" $seenDesktopExit "worker_desktop_rollback_process_not_exited"
    Assert-ScenarioCheck "process_lock_released" $seenLockFree "worker_desktop_rollback_lock_not_released"
    Assert-ScenarioCheck "task_enabled_after_release" $seenReadyAfterRelease "worker_desktop_rollback_task_enable_order_invalid"
    Assert-ScenarioCheck "legacy_started_after_enable" ($seenRunningAfterReady -and $legacyPid -gt 0) "worker_desktop_rollback_start_order_invalid"
    Assert-ScenarioCheck "same_worker_identity" (
        $status -and $status.worker_id -eq $script:Fixture.worker_id -and
        [int]$audits.drain_completion_audit_count -ge 1
    ) "worker_desktop_rollback_identity_mismatch"
    Assert-ScenarioCheck "single_owner" ((Get-WorkerProcesses).Count -eq 1 -and (Observe-Lock) -eq "HELD") "worker_desktop_rollback_multiple_owners"
}

function Test-RollbackLegacyTask {
    $identityBefore = Get-IdentityEvidence
    $null = Install-DisabledTask
    $desktopId = Open-Desktop -ExpectStartupWorker
    $desktopWorkerPid = [int](Get-WorkerProcesses)[0].ProcessId
    $script:Facts.process_ids.worker = $desktopWorkerPid
    Sign-InOperator
    $null = Add-OwnershipObservation $desktopId
    Invoke-UiButton $desktopId "Restore legacy Worker host"
    Watch-DesktopRollback $desktopWorkerPid
    $identityAfter = Get-IdentityEvidence
    Assert-ScenarioCheck "same_worker_identity" (
        $identityBefore.worker_id -eq $identityAfter.worker_id -and
        $identityBefore.marker_hash -eq $identityAfter.marker_hash -and
        $identityBefore.key_hash -eq $identityAfter.key_hash -and
        $identityBefore.host_config_hash -eq $identityAfter.host_config_hash
    ) "worker_desktop_rollback_changed_identity"
    $taskAfter = Get-TaskBinding
    $workers = Get-WorkerProcesses
    Assert-ScenarioCheck "single_owner" ($taskAfter.state -eq "RUNNING" -and $workers.Count -eq 1) "worker_desktop_rollback_owner_invalid"
    $script:Facts.journal_sha256 = $identityAfter.journal_hash
    $script:Facts.profile_tree_sha256 = $identityAfter.profile_tree_hash
    $script:Facts.profile_sentinel_sha256 = $identityAfter.profile_sentinel_hash
}

function Test-WorkerHide([int]$DesktopId, [int]$WorkerPid, [int[]]$ChromiumPids) {
    $process = [Diagnostics.Process]::GetProcessById($DesktopId)
    try {
        $process.Refresh()
        if (-not [ThreadsWorkerDesktopSmoke.NativeMethods]::PostMessage(
                $process.MainWindowHandle, 0x0010, [IntPtr]::Zero, [IntPtr]::Zero
            )) {
            throw "worker_desktop_window_hide_failed"
        }
    } finally { $process.Dispose() }
    $deadline = [DateTime]::UtcNow.AddSeconds(10)
    $hidden = $false
    do {
        $window = Get-DesktopWindow $DesktopId
        if (-not $window) { return $false }
        try {
            $desktop = [Diagnostics.Process]::GetProcessById($DesktopId)
            $desktop.Refresh()
            if (-not [ThreadsWorkerDesktopSmoke.NativeMethods]::IsWindowVisible($desktop.MainWindowHandle)) {
                $hidden = $true
                break
            }
        } finally { if ($desktop) { $desktop.Dispose() } }
        Start-Sleep -Milliseconds 150
    } while ([DateTime]::UtcNow -lt $deadline)
    $status = Get-WorkerStatus
    $workers = Get-WorkerProcesses
    $chromium = Get-ChromiumProcesses
    return $hidden -and $status.status -eq "ONLINE" -and
        $workers.Count -eq 1 -and [int]$workers[0].ProcessId -eq $WorkerPid -and
        $chromium.Count -eq $ChromiumPids.Count -and
        (@($ChromiumPids | Where-Object { $_ -notin @($chromium | ForEach-Object { [int]$_.ProcessId }) }).Count -eq 0)
}

function Test-DesktopCrashLogoutRecovery {
    $identityBefore = Get-IdentityEvidence
    $null = Install-DisabledTask
    $null = Start-PageFixture
    $desktopId = Open-Desktop -ExpectStartupWorker
    $workerPid = [int](Get-WorkerProcesses)[0].ProcessId
    $script:Facts.process_ids.worker = $workerPid
    Sign-InOperator
    $null = Queue-BlockedProfileJob
    $chromiumBefore = Get-ChromiumProcesses
    if ($chromiumBefore.Count -eq 0) { throw "worker_desktop_crash_browser_fixture_not_established" }
    $chromiumPids = @($chromiumBefore | ForEach-Object { [int]$_.ProcessId })
    $script:Facts.process_ids.chromium.Clear()
    foreach ($processId in $chromiumPids) { $script:Facts.process_ids.chromium.Add($processId) }

    Invoke-UiButton $desktopId "Sign out"
    $deadline = [DateTime]::UtcNow.AddSeconds(15)
    do {
        if ((Get-OperatorSessionCount) -eq 0) { break }
        Start-Sleep -Milliseconds 200
    } while ([DateTime]::UtcNow -lt $deadline)
    $logoutDidNotStop = (Get-OperatorSessionCount) -eq 0 -and
        (Get-WorkerStatus).status -eq "ONLINE" -and
        (Get-WorkerProcesses).Count -eq 1 -and (Get-ChromiumProcesses).Count -eq $chromiumPids.Count
    $hideDidNotStop = Test-WorkerHide $desktopId $workerPid $chromiumPids
    $singleDesktopBeforeCrash = @(Get-CimInstance -ClassName Win32_Process -Filter "Name = 'threads-desktop.exe'" |
        Where-Object { $_.ExecutablePath -and [IO.Path]::GetFullPath([string]$_.ExecutablePath).Equals(
            [IO.Path]::GetFullPath($DesktopExecutable), [StringComparison]::OrdinalIgnoreCase
        ) }).Count -eq 1

    $taskBeforeCrash = Get-TaskBinding
    $workerBeforeCrash = @(Get-WorkerProcesses | Where-Object { [int]$_.ProcessId -eq $workerPid })
    $chromiumBeforeCrash = @(Get-ChromiumProcesses | Where-Object { [int]$_.ProcessId -in $chromiumPids })
    $crashWorkerStatus = Get-WorkerStatus
    $blockedJobStatus = Invoke-Fixture @("job-status", "--worker-job-id", $script:ActiveJobId)
    $crashSession = [Diagnostics.Process]::GetCurrentProcess().SessionId
    $crashSid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    $capturedDescendantsAlive = (
        $taskBeforeCrash.state -eq "DISABLED" -and
        $workerBeforeCrash.Count -eq 1 -and
        $chromiumBeforeCrash.Count -eq $chromiumPids.Count -and
        (Test-ProcessIdAlive $workerPid) -and
        (@($chromiumPids | Where-Object { -not (Test-ProcessIdAlive $_) }).Count -eq 0) -and
        $crashWorkerStatus.status -eq "ONLINE" -and
        [int]$crashWorkerStatus.active_browser_sessions -gt 0 -and
        $blockedJobStatus.worker_job_status -eq "RUNNING" -and
        (Test-InteractiveProcessOwner (@($workerBeforeCrash) + @($chromiumBeforeCrash)) $crashSid $crashSession)
    )
    Assert-ScenarioCheck "captured_descendants_alive_at_crash" $capturedDescendantsAlive "worker_desktop_crash_descendants_not_alive_at_parent_exit"
    if (-not $capturedDescendantsAlive) { throw "worker_desktop_crash_descendants_not_alive_at_parent_exit" }

    Stop-Process -Id $desktopId -Force -ErrorAction SilentlyContinue
    if (-not (Wait-DesktopExit $desktopId 15)) { throw "worker_desktop_parent_crash_not_observed" }
    $deadline = [DateTime]::UtcNow.AddSeconds(20)
    do {
        $capturedWorkerAlive = Test-ProcessIdAlive $workerPid
        $capturedChromiumAlive = @($chromiumPids | Where-Object { Test-ProcessIdAlive $_ }).Count -gt 0
        if (-not $capturedWorkerAlive -and -not $capturedChromiumAlive -and
            (Get-AnyWorkerProcesses).Count -eq 0 -and (Get-ChromiumProcesses).Count -eq 0 -and
            (Observe-Lock) -eq "NOT_HELD") { break }
        Start-Sleep -Milliseconds 200
    } while ([DateTime]::UtcNow -lt $deadline)
    $capturedWorkerGone = -not (Test-ProcessIdAlive $workerPid)
    $capturedChromiumGone = @($chromiumPids | Where-Object { Test-ProcessIdAlive $_ }).Count -eq 0
    $reaped = $capturedWorkerGone -and $capturedChromiumGone -and
        (Get-AnyWorkerProcesses).Count -eq 0 -and (Get-ChromiumProcesses).Count -eq 0 -and
        (Observe-Lock) -eq "NOT_HELD"
    $taskAfterCrash = Get-TaskBinding
    $auditAfterCrash = Get-DrainAuditCounts
    $statusAfterCrash = Get-WorkerStatus -Record
    if ($statusAfterCrash.status -eq "OFFLINE") { throw "worker_desktop_crash_claimed_offline_without_drain" }
    if ($taskAfterCrash.state -ne "DISABLED") { throw "worker_desktop_crash_changed_legacy_task" }
    if ([int]$auditAfterCrash.drain_request_audit_count -ne 0 -or
        [int]$auditAfterCrash.drain_completion_audit_count -ne 0) {
        throw "worker_desktop_crash_fabricated_drain"
    }
    $crashIdentity = Get-IdentityEvidence
    if ($script:DesktopProcess) {
        $script:DesktopProcess.Dispose()
        $script:DesktopProcess = $null
    }

    $restartedDesktop = Open-Desktop -ExpectStartupWorker
    $recoveredStatus = Wait-WorkerStatus @("ONLINE") 45
    $recoveredWorkers = Get-WorkerProcesses
    $recoveredTask = Get-TaskBinding
    $identityAfter = Get-IdentityEvidence
    $singleRecovered = $recoveredWorkers.Count -eq 1 -and $recoveredTask.state -eq "DISABLED" -and
        $recoveredStatus.worker_id -eq $script:Fixture.worker_id
    Assert-ScenarioCheck "desktop_parent_abnormal_exit" (-not (Get-DesktopProcess $desktopId)) "worker_desktop_parent_exit_not_observed"
    Assert-ScenarioCheck "job_object_reaped_worker_descendants" $reaped "worker_desktop_job_object_did_not_reap_tree"
    Assert-ScenarioCheck "disabled_task_binding_unchanged" (
        $taskAfterCrash.state -eq "DISABLED" -and $recoveredTask.state -eq "DISABLED" -and
        $taskAfterCrash.executable_path -eq $recoveredTask.executable_path -and
        $taskAfterCrash.host_config_path -eq $recoveredTask.host_config_path
    ) "worker_desktop_crash_changed_disabled_task_binding"
    Assert-ScenarioCheck "identity_key_root_unchanged" (
        $identityBefore.worker_id -eq $identityAfter.worker_id -and
        $identityBefore.marker_hash -eq $identityAfter.marker_hash -and
        $identityBefore.key_hash -eq $identityAfter.key_hash
    ) "worker_desktop_crash_changed_identity"
    Assert-ScenarioCheck "journal_profile_preserved" (
        $null -ne $crashIdentity.journal_hash -and
        $identityAfter.profile_sentinel_hash -eq $identityBefore.profile_sentinel_hash -and
        (Test-Path -LiteralPath ([string]$script:Fixture.journal_path) -PathType Leaf)
    ) "worker_desktop_crash_lost_journal_or_profile"
    Assert-ScenarioCheck "worker_reconciliation_observed" (
        $recoveredStatus.status -eq "ONLINE" -and $recoveredStatus.worker_id -eq $script:Fixture.worker_id -and
        [int]$recoveredWorkers[0].ProcessId -ne $workerPid
    ) "worker_desktop_recovery_online_not_observed"
    Assert-ScenarioCheck "no_duplicate_identity_tree" ($singleRecovered -and $recoveredWorkers.Count -eq 1) "worker_desktop_duplicate_recovery_tree"
    Assert-ScenarioCheck "not_reported_as_graceful_offline" (
        [int]$auditAfterCrash.drain_completion_audit_count -eq 0 -and
        $statusAfterCrash.status -ne "OFFLINE" -and $logoutDidNotStop -and $hideDidNotStop -and $singleDesktopBeforeCrash
    ) "worker_desktop_crash_was_misreported_graceful"
    $null = Add-OwnershipObservation $restartedDesktop
    Set-EvidenceIdentity $identityAfter
}

function Watch-DesktopWorkerRestart(
    [int]$OldWorkerPid,
    [System.Diagnostics.Process]$OldWorkerHandle,
    [object]$IdentityBefore,
    [object]$TaskBefore
) {
    $deadline = [DateTime]::UtcNow.AddSeconds(120)
    $seenDraining = $false
    $seenOffline = $false
    $oldExitedNaturally = $false
    $lockReleased = $false
    $taskStayedDisabled = $true
    $newWorkerPid = 0
    $finalStatus = $null
    $lastTaskCheck = [DateTime]::MinValue
    do {
        try { Read-StatusTimeline } catch { }
        $seenOnlineAfterOffline = $false
        foreach ($observation in $script:Facts.status_counts_timeline) {
            if ($observation.status -eq "DRAINING") { $seenDraining = $true }
            if ($seenDraining -and $observation.status -eq "OFFLINE" -and
                [int]$observation.active_browser_sessions -eq 0 -and
                [int]$observation.running_worker_jobs -eq 0) {
                $seenOffline = $true
            }
            if ($seenOffline -and $observation.status -eq "ONLINE") {
                $seenOnlineAfterOffline = $true
            }
        }

        $OldWorkerHandle.Refresh()
        if ($OldWorkerHandle.HasExited -and $seenOffline -and $OldWorkerHandle.ExitCode -eq 0) {
            $oldExitedNaturally = $true
        }
        if ($seenOffline -and $oldExitedNaturally -and (Observe-Lock) -eq "NOT_HELD") {
            $lockReleased = $true
        }

        if (([DateTime]::UtcNow - $lastTaskCheck).TotalMilliseconds -ge 300) {
            $task = Get-TaskBinding
            $lastTaskCheck = [DateTime]::UtcNow
            if ($task.state -ne "DISABLED" -or
                $task.executable_path -ne $TaskBefore.executable_path -or
                $task.host_config_path -ne $TaskBefore.host_config_path -or
                $task.working_directory -ne $TaskBefore.working_directory) {
                $taskStayedDisabled = $false
                throw "worker_desktop_restart_legacy_task_changed"
            }
        }

        $workers = Get-WorkerProcesses
        if ($workers.Count -gt 1) { throw "worker_desktop_duplicate_worker_process" }
        if ($seenOffline -and $oldExitedNaturally -and $lockReleased -and
            $taskStayedDisabled -and $workers.Count -eq 1 -and
            [int]$workers[0].ProcessId -ne $OldWorkerPid) {
            $newWorkerPid = [int]$workers[0].ProcessId
            $script:Facts.process_ids.worker = $newWorkerPid
            if ($seenOnlineAfterOffline) {
                try { $finalStatus = Get-WorkerStatus } catch { $finalStatus = $null }
                if ($finalStatus -and $finalStatus.status -eq "ONLINE") { break }
            }
        }
        Start-Sleep -Milliseconds 20
    } while ([DateTime]::UtcNow -lt $deadline)

    $requestCounts = Get-RequestCounts
    $auditCounts = Get-DrainAuditCounts
    $newWorkers = Get-WorkerProcesses
    $taskAfter = Get-TaskBinding
    $identityAfter = Get-IdentityEvidence
    $lockHeldByNewWorker = (Observe-Lock) -eq "HELD"
    $newLaunchValid = $newWorkers.Count -eq 1 -and
        [int]$newWorkers[0].ProcessId -eq $newWorkerPid -and
        (Test-ExactWorkerLaunch $newWorkers[0])
    $bindingAndIdentityReused = (
        $identityAfter.worker_id -eq $IdentityBefore.worker_id -and
        $identityAfter.marker_hash -eq $IdentityBefore.marker_hash -and
        $identityAfter.key_hash -eq $IdentityBefore.key_hash -and
        $identityAfter.host_config_hash -eq $IdentityBefore.host_config_hash -and
        $identityAfter.data_root -eq $IdentityBefore.data_root -and
        $identityAfter.data_root -eq [IO.Path]::GetFullPath([string]$script:Fixture.data_root) -and
        $taskAfter.executable_path -eq $TaskBefore.executable_path -and
        $taskAfter.host_config_path -eq $TaskBefore.host_config_path -and
        $taskAfter.working_directory -eq $TaskBefore.working_directory
    )

    $script:Facts.drain_post_count = [int]$requestCounts.drain_post_count
    $script:Facts.drain_status_get_count = [int]$requestCounts.drain_status_get_count
    $script:Facts.worker_pid_timeline.Clear()
    $script:Facts.worker_pid_timeline.Add($OldWorkerPid)
    if ($newWorkerPid -gt 0) { $script:Facts.worker_pid_timeline.Add($newWorkerPid) }

    Assert-ScenarioCheck "worker_restart_requested" $seenDraining "worker_desktop_restart_drain_not_requested"
    Assert-ScenarioCheck "restart_drain_post_once" (
        [int]$requestCounts.drain_post_count -eq 1 -and
        [int]$requestCounts.drain_status_get_count -gt 0 -and
        [int]$auditCounts.drain_request_audit_count -eq 1 -and
        [int]$auditCounts.drain_completion_audit_count -eq 1
    ) "worker_desktop_restart_drain_request_count_invalid"
    Assert-ScenarioCheck "restart_authoritative_offline" $seenOffline "worker_desktop_restart_offline_not_observed"
    Assert-ScenarioCheck "old_worker_natural_exit" $oldExitedNaturally "worker_desktop_restart_old_process_not_natural_exit"
    Assert-ScenarioCheck "restart_process_lock_released" $lockReleased "worker_desktop_restart_lock_not_released"
    Assert-ScenarioCheck "legacy_task_disabled_through_restart" (
        $taskStayedDisabled -and $taskAfter.state -eq "DISABLED"
    ) "worker_desktop_restart_changed_legacy_task"
    Assert-ScenarioCheck "new_worker_pid_after_restart" ($newWorkerPid -gt 0 -and $newLaunchValid) "worker_desktop_restart_new_process_not_observed"
    Assert-ScenarioCheck "restart_reused_identity_and_root" $bindingAndIdentityReused "worker_desktop_restart_changed_identity_or_root"
    Assert-ScenarioCheck "restarted_worker_online" (
        $finalStatus -and $finalStatus.status -eq "ONLINE" -and
        $finalStatus.worker_id -eq $script:Fixture.worker_id -and
        $lockHeldByNewWorker -and $taskAfter.state -eq "DISABLED"
    ) "worker_desktop_restart_online_not_observed"
}

function Test-HeadedChromiumProfileContinuity {
    $identityBefore = Get-IdentityEvidence
    $taskBefore = Install-DisabledTask
    if ($taskBefore.state -ne "DISABLED") { throw "worker_desktop_profile_task_not_disabled" }
    $null = Start-PageFixture
    $desktopId = Open-Desktop -ExpectStartupWorker
    Sign-InOperator
    $workerBefore = Get-WorkerProcesses
    if ($workerBefore.Count -ne 1) { throw "worker_desktop_profile_worker_missing" }
    $workerPid = [int]$workerBefore[0].ProcessId
    $workerHandle = [Diagnostics.Process]::GetProcessById($workerPid)
    $profileBefore = Get-IdentityEvidence
    $null = Queue-BlockedProfileJob
    $chromium = Get-ChromiumProcesses
    $firstChromiumPids = @($chromium | ForEach-Object { [int]$_.ProcessId })
    if ($chromium.Count -eq 0) { throw "worker_desktop_profile_chromium_missing" }
    $processSession = [Diagnostics.Process]::GetCurrentProcess().SessionId
    $currentSid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    $interactiveOwners = Test-InteractiveProcessOwner (@($workerBefore) + @($chromium)) $currentSid $processSession
    $firstHeaded = @($chromium | Where-Object {
        [string]$_.CommandLine -notmatch "--headless(?:=|\s|$)" -and
        [string]$_.CommandLine -match [regex]::Escape([string]$script:Fixture.profile_directory)
    }).Count -eq $chromium.Count
    $hideDidNotStop = Test-WorkerHide $desktopId $workerPid $firstChromiumPids
    $reopened = Start-Process -FilePath $DesktopExecutable -PassThru
    $reopened.Dispose()
    $windowVisible = $false
    $deadline = [DateTime]::UtcNow.AddSeconds(10)
    do {
        $desktopProcess = $null
        try {
            $desktopProcess = [Diagnostics.Process]::GetProcessById($desktopId)
            $desktopProcess.Refresh()
            $windowVisible = [ThreadsWorkerDesktopSmoke.NativeMethods]::IsWindowVisible($desktopProcess.MainWindowHandle)
        } catch { $windowVisible = $false }
        finally { if ($desktopProcess) { $desktopProcess.Dispose() } }
        if ($windowVisible) { break }
        Start-Sleep -Milliseconds 150
    } while ([DateTime]::UtcNow -lt $deadline)
    $singleDesktop = @(Get-CimInstance -ClassName Win32_Process -Filter "Name = 'threads-desktop.exe'" |
        Where-Object { $_.ExecutablePath -and [IO.Path]::GetFullPath([string]$_.ExecutablePath).Equals(
            [IO.Path]::GetFullPath($DesktopExecutable), [StringComparison]::OrdinalIgnoreCase
        ) }).Count -eq 1
    Release-BlockedPage
    if ((Wait-ForJobStatus $script:ActiveJobId @("COMPLETED") 40) -ne "COMPLETED") {
        throw "worker_desktop_profile_job_did_not_complete"
    }

    $beforeRestart = Get-IdentityEvidence
    $taskBeforeRestart = Get-TaskBinding
    if ($taskBeforeRestart.state -ne "DISABLED") { throw "worker_desktop_profile_task_not_disabled" }
    Invoke-UiButton $desktopId "Restart…"
    Invoke-UiButton $desktopId "Authenticate and restart"
    Watch-DesktopWorkerRestart $workerPid $workerHandle $beforeRestart $taskBeforeRestart
    if (-not (Wait-ForNoChromium 20) -or
        @($firstChromiumPids | Where-Object { Test-ProcessIdAlive $_ }).Count -gt 0) {
        throw "worker_desktop_restart_chromium_not_reaped"
    }
    $null = Restart-PageFixture

    $profileAtRestart = Get-IdentityEvidence
    $null = Queue-BlockedProfileJob
    $secondWorker = Get-WorkerProcesses
    $secondChromium = Get-ChromiumProcesses
    $secondWorkerPid = if ($secondWorker.Count -eq 1) { [int]$secondWorker[0].ProcessId } else { 0 }
    $secondInteractiveOwners = Test-InteractiveProcessOwner (@($secondWorker) + @($secondChromium)) $currentSid $processSession
    $secondHeaded = $secondChromium.Count -gt 0 -and @($secondChromium | Where-Object {
        [string]$_.CommandLine -notmatch "--headless(?:=|\s|$)" -and
        [string]$_.CommandLine -match [regex]::Escape([string]$script:Fixture.profile_directory)
    }).Count -eq $secondChromium.Count
    $secondChromiumPids = @($secondChromium | ForEach-Object { [int]$_.ProcessId })
    Release-BlockedPage
    if ((Wait-ForJobStatus $script:ActiveJobId @("COMPLETED") 40) -ne "COMPLETED") {
        throw "worker_desktop_profile_second_job_did_not_complete"
    }
    $status = Wait-WorkerStatus @("ONLINE") 30
    $profileAfter = Get-IdentityEvidence
    $completedJob = Invoke-Fixture @("job-status", "--worker-job-id", $script:ActiveJobId)
    $script:Facts.process_ids.desktop = $desktopId
    $script:Facts.process_ids.worker = $secondWorkerPid
    $script:Facts.process_ids.chromium.Clear()
    $allChromiumPids = @($firstChromiumPids) + @($secondChromiumPids)
    foreach ($processId in @($allChromiumPids | Select-Object -Unique)) {
        $script:Facts.process_ids.chromium.Add([int]$processId)
    }

    Assert-ScenarioCheck "packaged_worker_browser_capability_executed" (
        $status.worker_id -eq $script:Fixture.worker_id -and $completedJob.worker_job_status -eq "COMPLETED"
    ) "worker_desktop_profile_capability_not_completed"
    Assert-ScenarioCheck "chromium_started_headed" ($firstHeaded -and $secondHeaded) "worker_desktop_chromium_not_headed"
    Assert-ScenarioCheck "not_session_zero_or_service" ([Environment]::UserInteractive -and $processSession -gt 0) "worker_desktop_process_not_interactive"
    Assert-ScenarioCheck "worker_and_chromium_interactive_user" ($interactiveOwners -and $secondInteractiveOwners) "worker_desktop_process_principal_or_session_invalid"
    Assert-ScenarioCheck "desktop_hide_did_not_stop_worker" ($hideDidNotStop -and $windowVisible -and $singleDesktop) "worker_desktop_hide_stopped_runtime_or_duplicate"
    Assert-ScenarioCheck "second_profile_capability_executed" (
        $secondWorkerPid -gt 0 -and $secondWorkerPid -ne $workerPid -and
        $secondChromium.Count -gt 0 -and $completedJob.worker_job_status -eq "COMPLETED"
    ) "worker_desktop_profile_second_capability_not_completed"
    Assert-ScenarioCheck "profile_sentinel_survived_boundary" (
        $profileBefore.profile_sentinel_hash -eq $profileAtRestart.profile_sentinel_hash -and
        $profileAtRestart.profile_sentinel_hash -eq $profileAfter.profile_sentinel_hash -and
        $profileAfter.profile_sentinel_hash -eq $script:Fixture.profile_sentinel_sha256
    ) "worker_desktop_profile_sentinel_changed"
    $matchingSentinels = @(Get-ChildItem -LiteralPath $script:DataRoot -Filter "dx07-profile-continuity.sentinel" -File -Recurse -Force)
    $profileCommandMatch = @($secondChromium | Where-Object {
        [string]$_.CommandLine -match [regex]::Escape([string]$script:Fixture.profile_directory)
    }).Count -gt 0
    Assert-ScenarioCheck "profile_not_copied_or_reinitialized" (
        (Test-Path -LiteralPath ([string]$script:Fixture.profile_directory) -PathType Container) -and
        $profileCommandMatch -and $matchingSentinels.Count -eq 1 -and
        $profileBefore.profile_sentinel_hash -eq $profileAfter.profile_sentinel_hash
    ) "worker_desktop_profile_reinitialized_or_copied"
    Assert-ScenarioCheck "same_identity_across_boundary" (
        $identityBefore.worker_id -eq $profileAfter.worker_id -and
        $identityBefore.marker_hash -eq $profileAfter.marker_hash -and
        $identityBefore.key_hash -eq $profileAfter.key_hash -and
        $identityBefore.data_root -eq $profileAfter.data_root -and
        $identityBefore.host_config_hash -eq $profileAfter.host_config_hash
    ) "worker_desktop_profile_boundary_changed_identity"
    Set-EvidenceIdentity $profileAfter
    $workerHandle.Dispose()
}

function Complete-SafeScenarioCleanup {
    if ($script:ExternalWorkerPid -gt 0) {
        Stop-Process -Id $script:ExternalWorkerPid -Force -ErrorAction SilentlyContinue
        $script:ExternalWorkerPid = 0
    }
    Release-BlockedPage
    $workers = Get-WorkerProcesses
    if ($script:FailureCode -eq $null -and $workers.Count -gt 0 -and $script:DesktopProcess) {
        $owner = Add-OwnershipObservation $script:DesktopProcess.Id
        if ($owner -in @("LEGACY", "TAKEOVER_REQUIRED")) {
            Invoke-UiButton $script:DesktopProcess.Id "Take over existing Worker" 20
            $null = Wait-WorkerStatus @("ONLINE") 60
            $owner = "DESKTOP"
        }
        if ($owner -eq "DESKTOP") {
            if ((Get-OperatorSessionCount) -eq 0) { Sign-InOperator }
            Submit-GracefulQuit -ExpectDesktopExit
        }
    }
    if ((Get-WorkerProcesses).Count -eq 0) { Stop-DesktopAfterNoWorker }

    foreach ($process in $script:Processes) {
        try {
            $process.Refresh()
            if (-not $process.HasExited) { Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue }
        } catch { }
        try { $process.Dispose() } catch { }
    }
    if ($script:CertificateStore -and $script:RootCertificate) {
        try {
            $script:CertificateStore.Open([Security.Cryptography.X509Certificates.OpenFlags]::ReadWrite)
            $script:CertificateStore.Remove($script:RootCertificate)
            $script:CertificateStore.Close()
        } catch { }
        try { $script:RootCertificate.Dispose() } catch { }
        try { $script:CertificateStore.Dispose() } catch { }
    }
    if ($script:PgStarted -and (Get-AnyWorkerProcesses).Count -eq 0) {
        $pgCtl = Join-Path $RuntimeRoot "postgresql\bin\pg_ctl.exe"
        & $pgCtl -D $script:PgData -m fast stop -w *> $null
        if ($LASTEXITCODE -ne 0) { throw "worker_desktop_fixture_database_stop_failed" }
        $script:PgStarted = $false
    }
    if ($script:DesktopProcess) {
        try { $script:DesktopProcess.Dispose() } catch { }
        $script:DesktopProcess = $null
    }
    if ($script:WorkRoot -and (Test-Path -LiteralPath $script:WorkRoot)) {
        Remove-Item -LiteralPath $script:WorkRoot -Recurse -Force -ErrorAction SilentlyContinue
    }
}

function Write-ScenarioEvidence([string]$FailureCode) {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [Security.Principal.WindowsPrincipal]::new($identity)
    $runner = [ordered]@{
        github_hosted = $env:GITHUB_ACTIONS -eq "true" -and $env:RUNNER_ENVIRONMENT -eq "github-hosted"
        windows_x64 = $env:RUNNER_OS -eq "Windows" -and $env:RUNNER_ARCH -eq "X64" -and [Environment]::Is64BitOperatingSystem
        non_administrator = -not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
        fresh_profile = [bool]$script:FreshProfile
        fresh_worker_root = [bool]$script:FreshWorkerRoot
    }
    $allChecksPassed = @($script:Checks.Values | Where-Object { -not $_ }).Count -eq 0
    $runnerValid = @($runner.Values | Where-Object { -not $_ }).Count -eq 0
    $result = if ([string]::IsNullOrWhiteSpace($FailureCode) -and $allChecksPassed -and $runnerValid) {
        "PASS"
    } else { "BLOCKER" }
    $safeFailure = if ($result -eq "PASS") { $null } elseif ($FailureCode -match "^worker_[a-z0-9_]{2,95}$") {
        $FailureCode
    } elseif (-not $allChecksPassed) { "worker_desktop_scenario_check_incomplete" }
    else { "worker_desktop_runner_evidence_incomplete" }
    $value = [ordered]@{
        schema_version = 1
        source_sha = $ExpectedSourceRevision
        scenario = $Scenario
        runner = $runner
        result = $result
        failure_code = $safeFailure
        checks = $script:Checks
        facts = $script:Facts
    }
    New-Item -ItemType Directory -Path (Split-Path -Parent $EvidencePath) -Force | Out-Null
    [IO.File]::WriteAllText(
        $EvidencePath,
        ($value | ConvertTo-Json -Depth 12),
        [Text.UTF8Encoding]::new($false)
    )
    return $result
}

$script:FailureCode = $null
try {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [Security.Principal.WindowsPrincipal]::new($identity)
    $currentProcess = [Diagnostics.Process]::GetCurrentProcess()
    if (
        $env:GITHUB_ACTIONS -ne "true" -or $env:RUNNER_ENVIRONMENT -ne "github-hosted" -or
        $env:RUNNER_OS -ne "Windows" -or $env:RUNNER_ARCH -ne "X64" -or
        -not [Environment]::Is64BitOperatingSystem -or
        $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator) -or
        -not [Environment]::UserInteractive -or $currentProcess.SessionId -eq 0
    ) {
        throw "worker_desktop_runner_context_invalid"
    }
    $appConfig = Join-Path $env:APPDATA "com.pumni.threads-desktop"
    $appLocal = Join-Path $env:LOCALAPPDATA "com.pumni.threads-desktop"
    $script:FreshProfile = -not (Test-Path -LiteralPath $appConfig) -and -not (Test-Path -LiteralPath $appLocal)
    $script:WorkRoot = Join-Path $env:LOCALAPPDATA ("DX07-WorkerDesktop-" + [guid]::NewGuid().ToString("N"))
    if (Test-Path -LiteralPath $script:WorkRoot) { throw "worker_desktop_worker_root_not_fresh" }
    $script:DataRoot = Join-Path $script:WorkRoot "worker-data"
    $script:FreshWorkerRoot = -not (Test-Path -LiteralPath $script:DataRoot)
    New-Item -ItemType Directory -Path $script:WorkRoot -Force | Out-Null
    Start-PostgresFixture
    Start-ControlPlaneFixture

    switch ($Scenario) {
        "legacy_running_cutover" { Test-LegacyCutover "RUNNING" }
        "legacy_ready_cutover" { Test-LegacyCutover "READY" }
        "legacy_disabled_desktop_start" { Test-DisabledDesktopStart }
        "no_task_fail_closed" { Test-NoTaskFailClosed }
        "identity_key_fail_closed" { Test-IdentityKeyFailClosed }
        "duplicate_process_lock" { Test-DuplicateProcessLock }
        "graceful_busy_drain_quit" { Test-GracefulBusyDrainQuit }
        "drain_failure_force_interrupt" { Test-DrainFailureForceInterrupt }
        "desktop_crash_logout_recovery" { Test-DesktopCrashLogoutRecovery }
        "rollback_legacy_task" { Test-RollbackLegacyTask }
        "headed_chromium_profile_continuity" { Test-HeadedChromiumProfileContinuity }
        default { throw "worker_desktop_scenario_unknown" }
    }
} catch {
    $FailureCode = if ($_.Exception.Message -match "^worker_[a-z0-9_]{2,95}$") {
        $_.Exception.Message
    } else { "worker_desktop_scenario_execution_failed" }
} finally {
    try {
        Read-StatusTimeline
    } catch {
        if (-not $FailureCode) { $FailureCode = "worker_desktop_authoritative_status_evidence_unavailable" }
    }
    try {
        if ($script:Fixture) {
            $counts = Get-RequestCounts
            $script:Facts.drain_post_count = [int]$counts.drain_post_count
            $script:Facts.drain_status_get_count = [int]$counts.drain_status_get_count
            $script:Facts.operator_me_get_count = [int]$counts.operator_me_get_count
        }
    } catch {
        if (-not $FailureCode) { $FailureCode = "worker_desktop_request_count_evidence_unavailable" }
    }
    try {
        Complete-SafeScenarioCleanup
    } catch {
        if (-not $FailureCode) { $FailureCode = "worker_desktop_harness_cleanup_failed" }
    }
    $result = Write-ScenarioEvidence $FailureCode
}

if ($result -eq "PASS") {
    Write-Output "Worker Desktop scenario PASS: $Scenario"
    exit 0
}
[Console]::Error.WriteLine("worker_desktop_scenario_blocked")
exit 1
