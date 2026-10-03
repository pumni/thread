[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$BundleExecutable,
    [Parameter(Mandatory = $true)]
    [string]$ExpectedSourceRevision,
    [Parameter(Mandatory = $true)]
    [string]$EvidencePath
)

$ErrorActionPreference = "Stop"
$PSNativeCommandUseErrorActionPreference = $false
$BundleExecutable = (Resolve-Path -LiteralPath $BundleExecutable).Path
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "../../")).Path
$EvidencePath = [System.IO.Path]::GetFullPath($EvidencePath)
$head = $null
$dirty = ""
$worktreeIsClean = $false

Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
if (-not ("ThreadsDesktopLifecycleSmoke.NativeMethods" -as [type])) {
    Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;

namespace ThreadsDesktopLifecycleSmoke {
    public static class NativeMethods {
        [DllImport("user32.dll", SetLastError = true)]
        public static extern bool PostMessage(IntPtr window, uint message, IntPtr wParam, IntPtr lParam);
        [DllImport("user32.dll")]
        public static extern bool IsWindowVisible(IntPtr window);
        [DllImport("user32.dll")]
        public static extern IntPtr GetForegroundWindow();
    }
}
"@
}

$bundleHash = (Get-FileHash -LiteralPath $BundleExecutable -Algorithm SHA256).Hash.ToLowerInvariant()
$deviceConfigPath = $null
$deviceConfigDirectory = $null
$configDirectoryExistedBeforeSmoke = $false
$configFileExistedBeforeSmoke = $false
$configCreatedBySmoke = $false
$persistedConfigAfterProvision = $null
$persistedConfigAfterRestart = $null
$restartTestTransitionStoppedProcesses = $false
$currentUserRunKeyPath = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"
$machineRunKeyPath = "HKLM:\Software\Microsoft\Windows\CurrentVersion\Run"
$originalAutostart = @{ HKCU = @{}; HKLM = @{} }
$autostartEvidence = [ordered]@{
    after_provision = $null
    after_restart = $null
    after_decommission = $null
}

function ConvertTo-SafeAutostartCommand([string]$Command) {
    $safe = [regex]::Replace(
        $Command,
        '(?i)(--?(?:password|token|secret|authorization|database-url)(?:=|\s+))("[^"]*"|\S+)',
        '$1<REDACTED>'
    )
    $safe = [regex]::Replace($safe, '(?i)(postgres(?:ql)?(?:\+\w+)?://)[^:\s/@]+:[^@\s/]+@', '$1<REDACTED>@')
    return [regex]::Replace($safe, '(?i)\bBearer\s+\S+', 'Bearer <REDACTED>')
}

function Get-BundleAutostartEntries([string]$KeyPath) {
    if (-not (Test-Path -LiteralPath $KeyPath)) { return @() }
    $runValues = Get-ItemProperty -LiteralPath $KeyPath
    @($runValues.PSObject.Properties | Where-Object {
        $_.Name -notlike "PS*" -and
        ([string]$_.Value).IndexOf($BundleExecutable, [System.StringComparison]::OrdinalIgnoreCase) -ge 0
    } | ForEach-Object {
        [pscustomobject]@{ Name = $_.Name; Value = [string]$_.Value }
    })
}

foreach ($registryRoot in @(
    @{ name = "HKCU"; path = $currentUserRunKeyPath },
    @{ name = "HKLM"; path = $machineRunKeyPath }
)) {
    foreach ($entry in @(Get-BundleAutostartEntries $registryRoot.path)) {
        $originalAutostart[$registryRoot.name][$entry.Name] = $entry.Value
    }
}

$checks = [ordered]@{
    concurrent_launch_keeps_one_primary = $false
    user_can_provision_worker_once = $false
    autostart_hkcu_registered = $false
    autostart_current_user_only = $false
    restart_autostart_hkcu_registered = $false
    restart_autostart_current_user_only = $false
    persisted_config_schema_valid = $false
    persisted_config_reports_worker_role = $false
    persisted_config_reports_autostart_enabled = $false
    worker_starts_one_mock_helper = $false
    second_launch_reuses_and_focuses_instance = $false
    window_close_hides_without_stopping_helper = $false
    tray_reopen_relocks_session_without_stopping_helper = $false
    restart_test_transition_stopped_processes = $false
    restart_restores_role_without_duplicate_helper = $false
    force_killed_helper_is_reported_degraded = $false
    decommission_removes_test_autostart = $false
}
$helperProcessIds = [System.Collections.Generic.List[int]]::new()
$startedProcessIds = [System.Collections.Generic.List[int]]::new()
$observedPrimaryPids = [System.Collections.Generic.HashSet[int]]::new()
$failureCodes = [System.Collections.Generic.List[string]]::new()
$failureCode = $null
$cleanupFailureCode = $null
$result = "BLOCKER"
$primaryId = $null
$primaryWindowHandle = [IntPtr]::Zero
$first = $null
$second = $null
$concurrentLaunchDiagnostics = $null

function Add-FailureCode([string]$Code) {
    if ([string]::IsNullOrWhiteSpace($Code)) { return }
    $safeCode = [regex]::Replace($Code, "[^A-Za-z0-9_.-]", "_")
    if (-not $failureCodes.Contains($safeCode)) { $failureCodes.Add($safeCode) }
    if (-not $script:failureCode) { $script:failureCode = $safeCode }
}

function Set-LifecycleCheck([string]$Name, [bool]$Passed, [string]$Failure) {
    $checks[$Name] = $Passed
    if (-not $Passed) { Add-FailureCode $Failure }
}

function Get-DesktopProcesses {
    @(Get-CimInstance Win32_Process -Filter "Name='threads-desktop.exe'" -ErrorAction Stop |
        Where-Object { $_.ExecutablePath -eq $BundleExecutable })
}

function Get-PrimaryProcesses {
    @(Get-DesktopProcesses | Where-Object { $_.CommandLine -notmatch "--threads-desktop-mock-runtime" })
}

function Get-ProcessWindowDetails([int]$ProcessId) {
    try {
        $process = Get-Process -Id $ProcessId -ErrorAction SilentlyContinue
        if (-not $process) {
            return [ordered]@{
                process_alive = $false
                main_window_handle = $null
                main_window_visible = $null
                window_query_failure = $null
            }
        }
        $process.Refresh()
        $handle = [Int64]$process.MainWindowHandle.ToInt64()
        $visible = if ($handle -eq 0) { $false } else {
            [bool][ThreadsDesktopLifecycleSmoke.NativeMethods]::IsWindowVisible([IntPtr]$handle)
        }
        return [ordered]@{
            process_alive = $true
            main_window_handle = $handle
            main_window_visible = $visible
            window_query_failure = $null
        }
    } catch {
        return [ordered]@{
            process_alive = $null
            main_window_handle = $null
            main_window_visible = $null
            window_query_failure = "process_window_query_failed"
        }
    }
}

function Get-LaunchProcessDetails([string]$Name, [System.Diagnostics.Process]$Process) {
    if (-not $Process) {
        return [ordered]@{
            name = $Name
            pid = $null
            has_exited = $null
            exit_code = $null
            window = $null
            process_query_failure = "process_not_created"
        }
    }

    $hasExited = $null
    $exitCode = $null
    $queryFailure = $null
    try {
        $Process.Refresh()
        $hasExited = [bool]$Process.HasExited
        if ($hasExited) { $exitCode = [int]$Process.ExitCode }
    } catch {
        $queryFailure = "launch_process_query_failed"
    }

    $window = if ($hasExited -eq $false) { Get-ProcessWindowDetails $Process.Id } else { $null }
    return [ordered]@{
        name = $Name
        pid = [int]$Process.Id
        has_exited = $hasExited
        exit_code = $exitCode
        window = $window
        process_query_failure = $queryFailure
    }
}

function ConvertTo-SafeProcessCommandLine([string]$CommandLine) {
    if (-not $CommandLine) { return $null }
    $safe = [regex]::Replace(
        $CommandLine,
        '(?i)(--?(?:password|token|secret|authorization|database-url)(?:=|\s+))("[^"]*"|\S+)',
        '$1<REDACTED>'
    )
    $safe = [regex]::Replace($safe, '(?i)(postgres(?:ql)?(?:\+\w+)?://)[^:\s/@]+:[^@\s/]+@', '$1<REDACTED>@')
    $safe = [regex]::Replace($safe, '(?i)\bBearer\s+\S+', 'Bearer <REDACTED>')
    return $safe
}

function Get-ConcurrentLaunchDiagnostics(
    [System.Diagnostics.Process]$FirstProcess,
    [System.Diagnostics.Process]$SecondProcess
) {
    $desktopProcesses = @()
    $inventoryFailure = $null
    try { $desktopProcesses = @(Get-DesktopProcesses) }
    catch { $inventoryFailure = "desktop_process_inventory_failed" }

    $discovered = @(
        foreach ($process in $desktopProcesses) {
            $window = Get-ProcessWindowDetails ([int]$process.ProcessId)
            [ordered]@{
                pid = [int]$process.ProcessId
                executable_path = [string]$process.ExecutablePath
                command_line = ConvertTo-SafeProcessCommandLine ([string]$process.CommandLine)
                session_id = [int]$process.SessionId
                is_primary = [string]$process.CommandLine -notmatch "--threads-desktop-mock-runtime"
                process_alive = $window.process_alive
                main_window_handle = $window.main_window_handle
                main_window_visible = $window.main_window_visible
                window_query_failure = $window.window_query_failure
            }
        }
    )
    $primaryCount = @($desktopProcesses | Where-Object {
        $_.CommandLine -notmatch "--threads-desktop-mock-runtime"
    }).Count
    $currentProcess = [System.Diagnostics.Process]::GetCurrentProcess()
    $runnerContext = [ordered]@{
        current_process_session_id = $currentProcess.SessionId
        session_name = $env:SESSIONNAME
        user_interactive = [Environment]::UserInteractive
        github_actions = $env:GITHUB_ACTIONS -eq "true"
        runner_environment = $env:RUNNER_ENVIRONMENT
        runner_os = $env:RUNNER_OS
        runner_arch = $env:RUNNER_ARCH
        runner_image = $env:ImageOS
        runner_image_version = $env:ImageVersion
    }

    return [ordered]@{
        captured_at_utc = [DateTime]::UtcNow.ToString("o")
        first_launch = Get-LaunchProcessDetails "first" $FirstProcess
        second_launch = Get-LaunchProcessDetails "second" $SecondProcess
        final_primary_process_count = $primaryCount
        observed_primary_pids = @($observedPrimaryPids | Sort-Object)
        discovered_processes = $discovered
        runner_context = $runnerContext
        inventory_failure = $inventoryFailure
    }
}

function Get-ConcurrentLaunchFailureCode($Diagnostics) {
    if ($Diagnostics.final_primary_process_count -ge 2) { return "both_primaries_survived" }
    if ($Diagnostics.inventory_failure) { return "single_instance_convergence_timeout" }
    if ($Diagnostics.final_primary_process_count -eq 0) {
        $observed = @($Diagnostics.observed_primary_pids)
        if ($observed.Count -eq 0) { return "primary_never_started" }
        $observedLaunchExited = @(
            $Diagnostics.first_launch,
            $Diagnostics.second_launch
        ) | Where-Object { $_.has_exited -eq $true -and $_.pid -in $observed }
        if ($observedLaunchExited.Count -gt 0) {
            return "primary_exited_during_startup"
        }
    }
    return "single_instance_convergence_timeout"
}

function Get-HelperProcesses([int]$ParentId) {
    @(Get-DesktopProcesses | Where-Object {
        $_.ParentProcessId -eq $ParentId -and $_.CommandLine -match "--threads-desktop-mock-runtime"
    })
}

function Wait-Until([scriptblock]$Condition, [int]$TimeoutSeconds, [string]$Failure) {
    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    while ([DateTime]::UtcNow -lt $deadline) {
        if (& $Condition) { return }
        Start-Sleep -Milliseconds 150
    }
    throw $Failure
}

function Get-PrimaryWindow {
    $process = Get-Process -Id $primaryId -ErrorAction SilentlyContinue
    if (-not $process) { return $null }
    $process.Refresh()
    if ($process.MainWindowHandle -eq [IntPtr]::Zero) { return $null }
    return [System.Windows.Automation.AutomationElement]::FromHandle($process.MainWindowHandle)
}

function Find-ElementByName(
    [System.Windows.Automation.AutomationElement]$Root,
    [string]$Name,
    [System.Windows.Automation.ControlType]$ControlType
) {
    if (-not $Root) { return $null }
    $conditions = @(
        [System.Windows.Automation.PropertyCondition]::new(
            [System.Windows.Automation.AutomationElement]::NameProperty,
            $Name
        ),
        [System.Windows.Automation.PropertyCondition]::new(
            [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
            $ControlType
        )
    )
    $condition = [System.Windows.Automation.AndCondition]::new($conditions)
    return $Root.FindFirst([System.Windows.Automation.TreeScope]::Descendants, $condition)
}

function Find-FirstByControlType(
    [System.Windows.Automation.AutomationElement]$Root,
    [System.Windows.Automation.ControlType]$ControlType
) {
    if (-not $Root) { return $null }
    $condition = [System.Windows.Automation.PropertyCondition]::new(
        [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
        $ControlType
    )
    return $Root.FindFirst([System.Windows.Automation.TreeScope]::Descendants, $condition)
}

function Invoke-Button([string]$Name) {
    Wait-Until {
        $window = Get-PrimaryWindow
        $button = Find-ElementByName $window $Name ([System.Windows.Automation.ControlType]::Button)
        if (-not $button) { return $false }
        try {
            $button.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern).Invoke()
            return $true
        } catch [System.Management.Automation.MethodInvocationException] {
            # Hosted UI Automation can transiently return a stale COM element.
            # Reacquire the semantic button on the next bounded poll.
            return $false
        }
    } 20 "lifecycle_button_unavailable_$($Name -replace '\W+', '_')"
}

try {
    $headOutput = @(& git -C $repoRoot rev-parse HEAD)
    if ($LASTEXITCODE -ne 0) { throw "lifecycle_git_head_unavailable" }
    $head = ($headOutput -join "`n").Trim()
    $dirtyLines = @(& git -C $repoRoot status --porcelain)
    if ($LASTEXITCODE -ne 0) { throw "lifecycle_git_status_unavailable" }
    $dirty = [string]::Join("`n", [string[]]$dirtyLines)
    if ($head -ne $ExpectedSourceRevision -or $dirty.Length -ne 0) {
        throw "lifecycle_smoke_requires_exact_clean_source_revision"
    }
    $worktreeIsClean = $true

    $tauriConfigPath = Join-Path $repoRoot "apps/desktop/src-tauri/tauri.conf.json"
    $tauriConfig = Get-Content -LiteralPath $tauriConfigPath -Raw | ConvertFrom-Json
    $tauriIdentifier = [string]$tauriConfig.identifier
    if ([string]::IsNullOrWhiteSpace($tauriIdentifier)) { throw "lifecycle_tauri_identifier_unavailable" }
    $roamingAppData = [Environment]::GetFolderPath([Environment+SpecialFolder]::ApplicationData)
    if ([string]::IsNullOrWhiteSpace($roamingAppData)) { throw "lifecycle_roaming_appdata_unavailable" }
    $deviceConfigDirectory = Join-Path $roamingAppData $tauriIdentifier
    $deviceConfigPath = Join-Path $deviceConfigDirectory "device-config.json"
    $configDirectoryExistedBeforeSmoke = Test-Path -LiteralPath $deviceConfigDirectory -PathType Container
    $configFileExistedBeforeSmoke = Test-Path -LiteralPath $deviceConfigPath -PathType Leaf
    if ($configFileExistedBeforeSmoke -or @(Get-DesktopProcesses).Count -gt 0) {
        throw "lifecycle_smoke_requires_disposable_windows_profile"
    }

    $first = Start-Process -FilePath $BundleExecutable -PassThru
    $second = Start-Process -FilePath $BundleExecutable -PassThru
    $startedProcessIds.Add($first.Id)
    $startedProcessIds.Add($second.Id)
    $convergenceDeadline = [DateTime]::UtcNow.AddSeconds(25)
    $concurrentPrimaries = @()
    do {
        $concurrentPrimaries = @(Get-PrimaryProcesses)
        foreach ($candidate in $concurrentPrimaries) {
            [void]$observedPrimaryPids.Add([int]$candidate.ProcessId)
        }
        if ($concurrentPrimaries.Count -eq 1) { break }
        Start-Sleep -Milliseconds 150
    } while ([DateTime]::UtcNow -lt $convergenceDeadline)
    if ($concurrentPrimaries.Count -ne 1) {
        $concurrentLaunchDiagnostics = Get-ConcurrentLaunchDiagnostics $first $second
        $failureCode = Get-ConcurrentLaunchFailureCode $concurrentLaunchDiagnostics
        $concurrentLaunchDiagnostics["failure_code"] = $failureCode
        throw $failureCode
    }
    $primary = Get-PrimaryProcesses | Select-Object -First 1
    $primaryId = [int]$primary.ProcessId
    $process = Get-Process -Id $primaryId -ErrorAction Stop
    Wait-Until {
        $process = Get-Process -Id $primaryId -ErrorAction SilentlyContinue
        if (-not $process) { return $false }
        $process.Refresh()
        if ($process.MainWindowHandle -eq [IntPtr]::Zero) { return $false }
        $script:primaryWindowHandle = $process.MainWindowHandle
        return [ThreadsDesktopLifecycleSmoke.NativeMethods]::IsWindowVisible($script:primaryWindowHandle)
    } 25 "desktop_initial_window_not_visible"
    Set-LifecycleCheck "concurrent_launch_keeps_one_primary" ((Get-PrimaryProcesses).Count -eq 1) "single_instance_primary_count_invalid"

    Invoke-Button "Provision as Worker"
    Wait-Until { (Get-HelperProcesses $primaryId).Count -eq 1 } 10 "worker_mock_helper_did_not_start"
    $helperId = [int](Get-HelperProcesses $primaryId | Select-Object -First 1).ProcessId
    $helperProcessIds.Add($helperId)
    Set-LifecycleCheck "user_can_provision_worker_once" $true "worker_provisioning_failed"
    Set-LifecycleCheck "worker_starts_one_mock_helper" ((Get-HelperProcesses $primaryId).Count -eq 1) "worker_mock_helper_count_invalid"

    $configPersisted = $false
    try {
        Wait-Until {
            if (-not (Test-Path -LiteralPath $script:deviceConfigPath -PathType Leaf)) { return $false }
            $script:configCreatedBySmoke = $true
            try {
                $candidateConfig = Get-Content -LiteralPath $script:deviceConfigPath -Raw | ConvertFrom-Json
            } catch {
                return $false
            }
            $script:persistedConfigAfterProvision = [ordered]@{
                schema_version = $candidateConfig.schema_version
                role = $candidateConfig.role
                autostart_enabled = $candidateConfig.autostart_enabled
            }
            return $candidateConfig.schema_version -eq 1 -and
                $candidateConfig.role -ceq "WORKER" -and
                $candidateConfig.autostart_enabled -eq $true
        } 15 "worker_device_config_not_persisted"
        $configPersisted = $true
    } catch {
        $configPersisted = $false
    }
    Set-LifecycleCheck "persisted_config_schema_valid" $configPersisted "worker_device_config_not_persisted"
    Set-LifecycleCheck "persisted_config_reports_worker_role" $configPersisted "worker_device_config_not_persisted"
    Set-LifecycleCheck "persisted_config_reports_autostart_enabled" $configPersisted "worker_device_config_not_persisted"

    $hkcuEntries = @(Get-BundleAutostartEntries $currentUserRunKeyPath)
    $hklmEntries = @(Get-BundleAutostartEntries $machineRunKeyPath)
    $newHklmEntries = @(
        foreach ($entry in $hklmEntries) {
            if (-not $originalAutostart.HKLM.ContainsKey($entry.Name) -or
                $originalAutostart.HKLM[$entry.Name] -cne $entry.Value) {
                [ordered]@{
                    value_name = $entry.Name
                    command = ConvertTo-SafeAutostartCommand $entry.Value
                }
            }
        }
    )
    $autostartEvidence.after_provision = [ordered]@{
        autostart_hkcu_registered = $hkcuEntries.Count -gt 0
        autostart_hklm_matching_entry_created = $newHklmEntries.Count -gt 0
        hkcu_matching_entries = @($hkcuEntries | ForEach-Object {
            [ordered]@{ value_name = $_.Name; command = ConvertTo-SafeAutostartCommand $_.Value }
        })
        new_hklm_matching_entries = $newHklmEntries
    }
    Set-LifecycleCheck "autostart_hkcu_registered" ($hkcuEntries.Count -gt 0) "controller_autostart_not_registered"
    $checks.autostart_current_user_only =
        ($hkcuEntries.Count -gt 0) -and ($newHklmEntries.Count -eq 0)
    if ($newHklmEntries.Count -gt 0) { Add-FailureCode "controller_autostart_created_hklm_entry" }

    try {
        $again = Start-Process -FilePath $BundleExecutable -PassThru
        $startedProcessIds.Add($again.Id)
        Wait-Until { $again.HasExited } 20 "second_invocation_did_not_exit_after_focus"
        $process = Get-Process -Id $primaryId -ErrorAction Stop
        $process.Refresh()
        $secondLaunchPassed =
            (Get-PrimaryProcesses).Count -eq 1 -and
            [ThreadsDesktopLifecycleSmoke.NativeMethods]::IsWindowVisible($primaryWindowHandle) -and
            [ThreadsDesktopLifecycleSmoke.NativeMethods]::GetForegroundWindow() -eq $primaryWindowHandle -and
            (Get-HelperProcesses $primaryId).Count -eq 1 -and
            [int](Get-HelperProcesses $primaryId | Select-Object -First 1).ProcessId -eq $helperId
        Set-LifecycleCheck "second_launch_reuses_and_focuses_instance" $secondLaunchPassed "second_launch_did_not_reuse_primary"
    } catch {
        Set-LifecycleCheck "second_launch_reuses_and_focuses_instance" $false ([string]$_.Exception.Message)
    }
    $primaryProcesses = @(Get-PrimaryProcesses)
    if ($primaryProcesses.Count -ne 1 -or [int]$primaryProcesses[0].ProcessId -ne $primaryId -or
        (Get-HelperProcesses $primaryId).Count -ne 1) {
        throw "lifecycle_state_unsafe_after_second_launch"
    }

    try {
        if (-not [ThreadsDesktopLifecycleSmoke.NativeMethods]::PostMessage(
            $primaryWindowHandle, 0x0010, [IntPtr]::Zero, [IntPtr]::Zero
        )) { throw "desktop_close_request_failed" }
        Wait-Until { -not [ThreadsDesktopLifecycleSmoke.NativeMethods]::IsWindowVisible($primaryWindowHandle) } 10 "desktop_close_did_not_hide_window"
        $hidePassed = (Get-Process -Id $primaryId -ErrorAction SilentlyContinue) -and
            (Get-Process -Id $helperId -ErrorAction SilentlyContinue) -and
            (Get-HelperProcesses $primaryId).Count -eq 1
        Set-LifecycleCheck "window_close_hides_without_stopping_helper" $hidePassed "window_close_stopped_mock_helper"
    } catch {
        Set-LifecycleCheck "window_close_hides_without_stopping_helper" $false ([string]$_.Exception.Message)
    }
    if ((Get-PrimaryProcesses).Count -ne 1 -or
        -not (Get-Process -Id $primaryId -ErrorAction SilentlyContinue) -or
        (Get-HelperProcesses $primaryId).Count -ne 1) {
        throw "lifecycle_state_unsafe_after_window_close"
    }

    try {
        $reopen = Start-Process -FilePath $BundleExecutable -PassThru
        $startedProcessIds.Add($reopen.Id)
        Wait-Until { $reopen.HasExited } 20 "reopen_invocation_did_not_exit_after_focus"
        Wait-Until { [ThreadsDesktopLifecycleSmoke.NativeMethods]::IsWindowVisible($primaryWindowHandle) } 10 "second_launch_did_not_reopen_hidden_window"
        Wait-Until {
            [bool](Find-ElementByName (Get-PrimaryWindow) "Session locked" ([System.Windows.Automation.ControlType]::Text))
        } 10 "reopen_session_not_locked"
        $reopenedHelpers = @(Get-HelperProcesses $primaryId)
        $helperStayedAlive = $reopenedHelpers.Count -eq 1 -and [int]$reopenedHelpers[0].ProcessId -eq $helperId
        $reopenPassed =
            $helperStayedAlive
        if (-not $helperStayedAlive) { Add-FailureCode "reopen_stopped_mock_helper" }
        Set-LifecycleCheck "tray_reopen_relocks_session_without_stopping_helper" $reopenPassed "reopen_did_not_relock_session_or_stopped_mock_helper"
    } catch {
        Set-LifecycleCheck "tray_reopen_relocks_session_without_stopping_helper" $false ([string]$_.Exception.Message)
    }
    $primaryProcesses = @(Get-PrimaryProcesses)
    if ($primaryProcesses.Count -ne 1 -or [int]$primaryProcesses[0].ProcessId -ne $primaryId -or
        (Get-HelperProcesses $primaryId).Count -ne 1) {
        throw "lifecycle_state_unsafe_after_reopen"
    }

    Stop-Process -Id $helperId -Force -ErrorAction Stop
    Stop-Process -Id $primaryId -Force -ErrorAction Stop
    Wait-Until {
        -not (Get-Process -Id $helperId -ErrorAction SilentlyContinue) -and
        -not (Get-Process -Id $primaryId -ErrorAction SilentlyContinue) -and
        (Get-PrimaryProcesses).Count -eq 0
    } 10 "restart_test_transition_processes_still_running"
    $restartTestTransitionStoppedProcesses = $true
    Set-LifecycleCheck "restart_test_transition_stopped_processes" $true "restart_test_transition_failed"

    $restartReady = $false
    try {
        $restart = Start-Process -FilePath $BundleExecutable -PassThru
        $startedProcessIds.Add($restart.Id)
        Wait-Until { (Get-PrimaryProcesses).Count -eq 1 } 20 "restart_primary_missing"
        $primary = Get-PrimaryProcesses | Select-Object -First 1
        $primaryId = [int]$primary.ProcessId
        $process = Get-Process -Id $primaryId -ErrorAction Stop
        Wait-Until {
            $process = Get-Process -Id $primaryId -ErrorAction SilentlyContinue
            if (-not $process) { return $false }
            $process.Refresh()
            if ($process.MainWindowHandle -eq [IntPtr]::Zero) { return $false }
            $script:primaryWindowHandle = $process.MainWindowHandle
            return [ThreadsDesktopLifecycleSmoke.NativeMethods]::IsWindowVisible($script:primaryWindowHandle)
        } 20 "restart_window_not_visible"
        Wait-Until { (Get-HelperProcesses $primaryId).Count -eq 1 } 10 "restart_did_not_restore_controller_runtime"
        $helperId = [int](Get-HelperProcesses $primaryId | Select-Object -First 1).ProcessId
        $helperProcessIds.Add($helperId)
        Wait-Until {
            $primaryProcesses = @(Get-PrimaryProcesses)
            $helperProcesses = @(Get-HelperProcesses $primaryId)
            if ($primaryProcesses.Count -ne 1 -or [int]$primaryProcesses[0].ProcessId -ne $primaryId -or
                $helperProcesses.Count -ne 1 -or [int]$helperProcesses[0].ProcessId -ne $helperId) {
                return $false
            }
            $process = Get-Process -Id $primaryId -ErrorAction SilentlyContinue
            if (-not $process) { return $false }
            $process.Refresh()
            if ($process.MainWindowHandle -eq [IntPtr]::Zero -or
                -not [ThreadsDesktopLifecycleSmoke.NativeMethods]::IsWindowVisible($process.MainWindowHandle)) {
                return $false
            }
            $script:primaryWindowHandle = $process.MainWindowHandle
            try {
                $workerHeading = Find-ElementByName (Get-PrimaryWindow) "Your Worker" `
                    ([System.Windows.Automation.ControlType]::Text)
                $restartConfig = Get-Content -LiteralPath $script:deviceConfigPath -Raw | ConvertFrom-Json
            } catch {
                return $false
            }
            if (-not $workerHeading -or $restartConfig.schema_version -ne 1 -or
                $restartConfig.role -cne "WORKER" -or $restartConfig.autostart_enabled -ne $true) {
                return $false
            }
            $script:persistedConfigAfterRestart = [ordered]@{
                schema_version = $restartConfig.schema_version
                role = $restartConfig.role
                autostart_enabled = $restartConfig.autostart_enabled
            }
            return $true
        } 15 "restart_worker_ui_not_ready"
        Set-LifecycleCheck "restart_restores_role_without_duplicate_helper" $true "restart_worker_ui_not_ready"

        $restartHkcuEntries = @(Get-BundleAutostartEntries $currentUserRunKeyPath)
        $restartHklmEntries = @(Get-BundleAutostartEntries $machineRunKeyPath)
        $newRestartHklmEntries = @(
            foreach ($entry in $restartHklmEntries) {
                if (-not $originalAutostart.HKLM.ContainsKey($entry.Name) -or
                    $originalAutostart.HKLM[$entry.Name] -cne $entry.Value) {
                    [ordered]@{
                        value_name = $entry.Name
                        command = ConvertTo-SafeAutostartCommand $entry.Value
                    }
                }
            }
        )
        $autostartEvidence.after_restart = [ordered]@{
            autostart_hkcu_registered = $restartHkcuEntries.Count -gt 0
            autostart_hklm_matching_entry_created = $newRestartHklmEntries.Count -gt 0
            hkcu_matching_entries = @($restartHkcuEntries | ForEach-Object {
                [ordered]@{ value_name = $_.Name; command = ConvertTo-SafeAutostartCommand $_.Value }
            })
            new_hklm_matching_entries = $newRestartHklmEntries
        }
        Set-LifecycleCheck "restart_autostart_hkcu_registered" ($restartHkcuEntries.Count -gt 0) "restart_autostart_not_registered"
        $checks.restart_autostart_current_user_only =
            ($restartHkcuEntries.Count -gt 0) -and ($newRestartHklmEntries.Count -eq 0)
        if ($newRestartHklmEntries.Count -gt 0) { Add-FailureCode "restart_autostart_created_hklm_entry" }
        $restartReady = $true
    } catch {
        Set-LifecycleCheck "restart_restores_role_without_duplicate_helper" $false ([string]$_.Exception.Message)
    }

    if ($restartReady) {
        try {
            Stop-Process -Id $helperId -Force -ErrorAction Stop
            Wait-Until { (Get-HelperProcesses $primaryId).Count -eq 0 } 10 "force_killed_helper_still_running"
            Wait-Until {
                [bool](Find-ElementByName (Get-PrimaryWindow) "Needs attention" ([System.Windows.Automation.ControlType]::Text))
            } 10 "force_killed_helper_not_reported_degraded"
            Set-LifecycleCheck "force_killed_helper_is_reported_degraded" $true "force_killed_helper_not_reported_degraded"
        } catch {
            Set-LifecycleCheck "force_killed_helper_is_reported_degraded" $false ([string]$_.Exception.Message)
        }

        $decommissioned = $false
        try {
            Invoke-Button "Decommission device"
            Wait-Until {
                [bool](Find-FirstByControlType (Get-PrimaryWindow) ([System.Windows.Automation.ControlType]::Edit))
            } 20 "decommission_confirmation_input_unavailable"
            $phraseInput = Find-FirstByControlType (Get-PrimaryWindow) ([System.Windows.Automation.ControlType]::Edit)
            if (-not $phraseInput) { throw "decommission_confirmation_input_unavailable" }
            $phraseInput.GetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern).SetValue("RESET THIS DEVICE")
            Invoke-Button "Decommission"
            Wait-Until {
                if ((Get-HelperProcesses $primaryId).Count -ne 0) { return $false }
                $decommissionConfig = Get-Content -LiteralPath $deviceConfigPath -Raw | ConvertFrom-Json
                $decommissionEntries = @(Get-BundleAutostartEntries $currentUserRunKeyPath)
                return $null -eq $decommissionConfig.role -and
                    $decommissionConfig.autostart_enabled -eq $false -and
                    $decommissionEntries.Count -eq 0
            } 15 "decommission_did_not_clear_device_or_autostart"
            $decommissioned = $true
        } catch {
            Add-FailureCode ([string]$_.Exception.Message)
        }
        $postDecommissionHkcu = @(Get-BundleAutostartEntries $currentUserRunKeyPath)
        $postDecommissionHklm = @(Get-BundleAutostartEntries $machineRunKeyPath)
        $autostartEvidence.after_decommission = [ordered]@{
            hkcu_matching_entries = @($postDecommissionHkcu | ForEach-Object {
                [ordered]@{ value_name = $_.Name; command = ConvertTo-SafeAutostartCommand $_.Value }
            })
            hklm_matching_entries = @($postDecommissionHklm | ForEach-Object {
                [ordered]@{ value_name = $_.Name; command = ConvertTo-SafeAutostartCommand $_.Value }
            })
        }
        Set-LifecycleCheck "decommission_removes_test_autostart" ($decommissioned -and $postDecommissionHkcu.Count -eq 0) "decommission_left_autostart_registered"
    } else {
        Set-LifecycleCheck "force_killed_helper_is_reported_degraded" $false "failed_check_unavailable_after_restart_failure"
        Set-LifecycleCheck "decommission_removes_test_autostart" $false "decommission_check_unavailable_after_restart_failure"
    }

    $failedChecks = @($checks.GetEnumerator() | Where-Object { -not $_.Value })
    $result = if ($failedChecks.Count -eq 0 -and $failureCodes.Count -eq 0) { "PASS" } else { "BLOCKER" }
} catch {
    Add-FailureCode ([string]$_.Exception.Message)
    if (-not $concurrentLaunchDiagnostics -and $failureCode -in @(
        "primary_never_started",
        "both_primaries_survived",
        "primary_exited_during_startup",
        "single_instance_convergence_timeout"
    )) {
        $concurrentLaunchDiagnostics = Get-ConcurrentLaunchDiagnostics $first $second
        $concurrentLaunchDiagnostics["failure_code"] = $failureCode
    }
} finally {
    try {
        $ownedProcessIds = [System.Collections.Generic.HashSet[int]]::new()
        foreach ($processId in $startedProcessIds) { [void]$ownedProcessIds.Add([int]$processId) }
        foreach ($processId in $observedPrimaryPids) { [void]$ownedProcessIds.Add([int]$processId) }
        if ($primaryId) { [void]$ownedProcessIds.Add([int]$primaryId) }
        $ownedProcesses = @(
            Get-DesktopProcesses | Where-Object {
                $ownedProcessIds.Contains([int]$_.ProcessId) -or
                ($_.CommandLine -match "--threads-desktop-mock-runtime" -and
                    $ownedProcessIds.Contains([int]$_.ParentProcessId))
            }
        )
        foreach ($process in $ownedProcesses | Where-Object { $_.CommandLine -match "--threads-desktop-mock-runtime" }) {
            Stop-Process -Id $process.ProcessId -Force -ErrorAction SilentlyContinue
        }
        foreach ($process in $ownedProcesses | Where-Object { $_.CommandLine -notmatch "--threads-desktop-mock-runtime" }) {
            Stop-Process -Id $process.ProcessId -Force -ErrorAction SilentlyContinue
        }
        foreach ($registryRoot in @(
            @{ name = "HKCU"; path = $currentUserRunKeyPath },
            @{ name = "HKLM"; path = $machineRunKeyPath }
        )) {
            $originalEntries = $originalAutostart[$registryRoot.name]
            if (Test-Path -LiteralPath $registryRoot.path) {
                $currentValues = Get-ItemProperty -LiteralPath $registryRoot.path
                foreach ($property in $currentValues.PSObject.Properties) {
                    if ($property.Name -like "PS*") { continue }
                    $currentValue = [string]$property.Value
                    if ($originalEntries.ContainsKey($property.Name)) {
                        if ($currentValue -cne $originalEntries[$property.Name]) {
                            Set-ItemProperty -LiteralPath $registryRoot.path -Name $property.Name `
                                -Value $originalEntries[$property.Name]
                        }
                    } elseif ($currentValue.IndexOf(
                        $BundleExecutable,
                        [System.StringComparison]::OrdinalIgnoreCase
                    ) -ge 0) {
                        Remove-ItemProperty -LiteralPath $registryRoot.path -Name $property.Name `
                            -ErrorAction SilentlyContinue
                    }
                }
            }
            foreach ($name in $originalEntries.Keys) {
                if (-not (Test-Path -LiteralPath $registryRoot.path)) {
                    New-Item -Path $registryRoot.path -Force | Out-Null
                }
                Set-ItemProperty -LiteralPath $registryRoot.path -Name $name -Value $originalEntries[$name]
            }
        }
    } catch {
        $cleanupFailureCode = [regex]::Replace($_.Exception.Message, "[^A-Za-z0-9_.-]", "_")
        Add-FailureCode $cleanupFailureCode
        $result = "BLOCKER"
    }
    try {
        if ($configCreatedBySmoke -and -not $configFileExistedBeforeSmoke -and
            $deviceConfigPath -and (Test-Path -LiteralPath $deviceConfigPath -PathType Leaf)) {
            Remove-Item -LiteralPath $deviceConfigPath -Force -ErrorAction Stop
        }
        if (-not $configDirectoryExistedBeforeSmoke -and $deviceConfigDirectory -and
            (Test-Path -LiteralPath $deviceConfigDirectory -PathType Container) -and
            @(Get-ChildItem -LiteralPath $deviceConfigDirectory -Force).Count -eq 0) {
            Remove-Item -LiteralPath $deviceConfigDirectory -Force -ErrorAction Stop
        }
    } catch {
        $cleanupFailureCode = [regex]::Replace($_.Exception.Message, "[^A-Za-z0-9_.-]", "_")
        Add-FailureCode $cleanupFailureCode
        $result = "BLOCKER"
    }
    $evidenceDirectory = Split-Path -Parent $EvidencePath
    New-Item -ItemType Directory -Path $evidenceDirectory -Force | Out-Null
    $evidence = [ordered]@{
        schema_version = 1
        source_revision = $ExpectedSourceRevision
        checked_head = $head
        source_worktree_clean = $worktreeIsClean
        host_windows_version = [Environment]::OSVersion.Version.ToString()
        architecture_x64 = [Environment]::Is64BitOperatingSystem
        current_user_is_administrator = [Security.Principal.WindowsPrincipal]::new(
            [Security.Principal.WindowsIdentity]::GetCurrent()
        ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
        windows_session_context = [ordered]@{
            current_process_session_id = [System.Diagnostics.Process]::GetCurrentProcess().SessionId
            session_name = $env:SESSIONNAME
            user_interactive = [Environment]::UserInteractive
            github_actions = $env:GITHUB_ACTIONS -eq "true"
            runner_environment = $env:RUNNER_ENVIRONMENT
            runner_os = $env:RUNNER_OS
            runner_arch = $env:RUNNER_ARCH
            runner_image = $env:ImageOS
            runner_image_version = $env:ImageVersion
        }
        executable_sha256 = $bundleHash
        device_config_path = $deviceConfigPath
        device_config_after_provision = $persistedConfigAfterProvision
        device_config_after_restart = $persistedConfigAfterRestart
        tray_shell_ui_automation = "NOT_GATED_ON_GITHUB_HOSTED"
        restart_test_transition = if ($restartTestTransitionStoppedProcesses) {
            "forced_process_termination_not_graceful_quit"
        } else { "NOT_RUN" }
        checks = $checks
        autostart = $autostartEvidence
        helper_process_ids = @($helperProcessIds)
        concurrent_launch_diagnostics = $concurrentLaunchDiagnostics
        result = $result
        failure_code = $failureCode
        failure_codes = @($failureCodes)
        cleanup_failure_code = $cleanupFailureCode
    }
    [System.IO.File]::WriteAllText(
        $EvidencePath,
        ($evidence | ConvertTo-Json -Depth 8),
        [System.Text.UTF8Encoding]::new($false)
    )
}

if ($result -ne "PASS") { throw "DX-02 Windows lifecycle smoke blocked: $failureCode" }
Write-Output "DX-02 Windows lifecycle smoke PASS for $ExpectedSourceRevision"
