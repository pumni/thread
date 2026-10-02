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
        [DllImport("user32.dll", SetLastError = true)]
        public static extern bool SetCursorPos(int x, int y);
        [DllImport("user32.dll")]
        public static extern void mouse_event(uint flags, uint dx, uint dy, uint data, UIntPtr extraInfo);
    }
}
"@
}

$bundleHash = (Get-FileHash -LiteralPath $BundleExecutable -Algorithm SHA256).Hash.ToLowerInvariant()
$appDataRoot = Join-Path $env:TEMP ("ThreadsDesktopDx02-" + [guid]::NewGuid().ToString("N"))
$previousAppData = $env:APPDATA
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
    user_can_provision_controller_once = $false
    autostart_hkcu_registered = $false
    autostart_current_user_only = $false
    restart_autostart_hkcu_registered = $false
    restart_autostart_current_user_only = $false
    persisted_config_reports_autostart_enabled = $false
    controller_starts_one_mock_helper = $false
    second_launch_reuses_and_focuses_instance = $false
    window_close_hides_without_stopping_helper = $false
    tray_reopen_relocks_session_without_stopping_helper = $false
    tray_quit_confirms_and_stops_helper_before_exit = $false
    restart_restores_role_without_duplicate_helper = $false
    force_killed_helper_is_reported_degraded = $false
    decommission_removes_test_autostart = $false
    restart_quits_cleanly = $false
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
        $button.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern).Invoke()
        return $true
    } 20 "lifecycle_button_unavailable_$($Name -replace '\W+', '_')"
}

function Get-TrayButtons {
    $root = [System.Windows.Automation.AutomationElement]::RootElement
    $condition = [System.Windows.Automation.PropertyCondition]::new(
        [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
        [System.Windows.Automation.ControlType]::Button
    )
    return $root.FindAll([System.Windows.Automation.TreeScope]::Descendants, $condition)
}

function Open-TrayQuitMenu {
    $trayButtons = @(Get-TrayButtons)
    $overflow = $trayButtons | Where-Object {
        $_.Current.Name -match "(?i)(hidden.*icons|show.*icons|notification.*overflow)"
    } | Select-Object -First 1
    if ($overflow) {
        try { $overflow.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern).Invoke() }
        catch { $overflow.SetFocus() }
        Start-Sleep -Milliseconds 300
    }

    $trayIcon = $null
    foreach ($button in @(Get-TrayButtons)) {
        if ($button.Current.Name -eq "Threads Desktop" -and
            $button.Current.NativeWindowHandle -ne $primaryWindowHandle.ToInt32()) {
            $trayIcon = $button
            break
        }
    }
    if (-not $trayIcon) { throw "desktop_tray_icon_not_accessible" }
    $bounds = $trayIcon.Current.BoundingRectangle
    if ($bounds.IsEmpty) { throw "desktop_tray_icon_has_no_bounds" }
    $x = [int]($bounds.Left + ($bounds.Width / 2))
    $y = [int]($bounds.Top + ($bounds.Height / 2))
    if (-not [ThreadsDesktopLifecycleSmoke.NativeMethods]::SetCursorPos($x, $y)) {
        throw "desktop_tray_pointer_position_failed"
    }
    [ThreadsDesktopLifecycleSmoke.NativeMethods]::mouse_event(0x0008, 0, 0, 0, [UIntPtr]::Zero)
    [ThreadsDesktopLifecycleSmoke.NativeMethods]::mouse_event(0x0010, 0, 0, 0, [UIntPtr]::Zero)
    Wait-Until {
        $root = [System.Windows.Automation.AutomationElement]::RootElement
        $items = $root.FindAll(
            [System.Windows.Automation.TreeScope]::Descendants,
            [System.Windows.Automation.PropertyCondition]::new(
                [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
                [System.Windows.Automation.ControlType]::MenuItem
            )
        )
        return @($items | Where-Object { $_.Current.Name -like "Quit*" }).Count -gt 0
    } 5 "desktop_tray_quit_menu_unavailable"
    $root = [System.Windows.Automation.AutomationElement]::RootElement
    $items = $root.FindAll(
        [System.Windows.Automation.TreeScope]::Descendants,
        [System.Windows.Automation.PropertyCondition]::new(
            [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
            [System.Windows.Automation.ControlType]::MenuItem
        )
    )
    $quitItem = $items | Where-Object { $_.Current.Name -like "Quit*" } | Select-Object -First 1
    if (-not $quitItem) { throw "desktop_tray_quit_menu_unavailable" }
    $quitItem.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern).Invoke()
}

function Confirm-TrayQuit([int]$ExpectedHelperId) {
    Open-TrayQuitMenu
    Wait-Until {
        $window = Get-PrimaryWindow
        $button = Find-ElementByName $window "Stop node and quit" ([System.Windows.Automation.ControlType]::Button)
        if (-not $button) { return $false }
        $button.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern).Invoke()
        return $true
    } 10 "desktop_quit_confirmation_unavailable"
    Wait-Until { -not (Get-Process -Id $primaryId -ErrorAction SilentlyContinue) } 20 "desktop_quit_did_not_exit"
    if (Get-Process -Id $ExpectedHelperId -ErrorAction SilentlyContinue) {
        throw "desktop_quit_exited_before_mock_helper_stopped"
    }
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

    New-Item -ItemType Directory -Path $appDataRoot -Force | Out-Null
    $env:APPDATA = $appDataRoot

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

    Invoke-Button "Provision as Controller"
    Wait-Until { (Get-HelperProcesses $primaryId).Count -eq 1 } 10 "controller_mock_helper_did_not_start"
    $helperId = [int](Get-HelperProcesses $primaryId | Select-Object -First 1).ProcessId
    $helperProcessIds.Add($helperId)
    Set-LifecycleCheck "user_can_provision_controller_once" $true "controller_provisioning_failed"
    Set-LifecycleCheck "controller_starts_one_mock_helper" ((Get-HelperProcesses $primaryId).Count -eq 1) "controller_mock_helper_count_invalid"

    $deviceConfigFiles = @(Get-ChildItem -LiteralPath $appDataRoot -Filter "device-config.json" -File -Recurse)
    $configReportsAutostart = $false
    try {
        if ($deviceConfigFiles.Count -eq 1) {
            $deviceConfig = Get-Content -LiteralPath $deviceConfigFiles[0].FullName -Raw | ConvertFrom-Json
            $configReportsAutostart = $deviceConfig.autostart_enabled -eq $true
        }
    } catch {
        $configReportsAutostart = $false
    }
    Set-LifecycleCheck "persisted_config_reports_autostart_enabled" $configReportsAutostart "controller_autostart_config_not_persisted"

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
        $reopenPassed =
            [bool](Find-ElementByName (Get-PrimaryWindow) "Session locked" ([System.Windows.Automation.ControlType]::Text)) -and
            (Get-HelperProcesses $primaryId).Count -eq 1 -and
            [int](Get-HelperProcesses $primaryId | Select-Object -First 1).ProcessId -eq $helperId
        Set-LifecycleCheck "tray_reopen_relocks_session_without_stopping_helper" $reopenPassed "reopen_did_not_relock_session_or_stopped_mock_helper"
    } catch {
        Set-LifecycleCheck "tray_reopen_relocks_session_without_stopping_helper" $false ([string]$_.Exception.Message)
    }
    $primaryProcesses = @(Get-PrimaryProcesses)
    if ($primaryProcesses.Count -ne 1 -or [int]$primaryProcesses[0].ProcessId -ne $primaryId -or
        (Get-HelperProcesses $primaryId).Count -ne 1) {
        throw "lifecycle_state_unsafe_after_reopen"
    }

    try {
        Confirm-TrayQuit $helperId
        Set-LifecycleCheck "tray_quit_confirms_and_stops_helper_before_exit" $true "tray_quit_failed"
    } catch {
        Set-LifecycleCheck "tray_quit_confirms_and_stops_helper_before_exit" $false ([string]$_.Exception.Message)
        if (Get-Process -Id $helperId -ErrorAction SilentlyContinue) {
            Stop-Process -Id $helperId -Force -ErrorAction SilentlyContinue
        }
        if (Get-Process -Id $primaryId -ErrorAction SilentlyContinue) {
            Stop-Process -Id $primaryId -Force -ErrorAction SilentlyContinue
        }
        Wait-Until { (Get-PrimaryProcesses).Count -eq 0 } 10 "failed_quit_cleanup_did_not_stop_primary"
    }

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
        $controllerHeading = Find-ElementByName (Get-PrimaryWindow) "Your Controller" ([System.Windows.Automation.ControlType]::Text)
        $restartPassed = [bool]$controllerHeading -and (Get-HelperProcesses $primaryId).Count -eq 1
        Set-LifecycleCheck "restart_restores_role_without_duplicate_helper" $restartPassed "restart_did_not_restore_single_controller"

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
            $phraseInput = Find-FirstByControlType (Get-PrimaryWindow) ([System.Windows.Automation.ControlType]::Edit)
            if (-not $phraseInput) { throw "decommission_confirmation_input_unavailable" }
            $phraseInput.GetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern).SetValue("RESET THIS DEVICE")
            Invoke-Button "Decommission"
            Wait-Until { (Get-HelperProcesses $primaryId).Count -eq 0 } 10 "decommission_did_not_stop_mock_helper"
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
        try {
            Confirm-TrayQuit 0
            Set-LifecycleCheck "restart_quits_cleanly" $true "final_quit_failed"
        } catch {
            Set-LifecycleCheck "restart_quits_cleanly" $false ([string]$_.Exception.Message)
        }
    } else {
        Set-LifecycleCheck "force_killed_helper_is_reported_degraded" $false "degraded_check_unavailable_after_restart_failure"
        Set-LifecycleCheck "decommission_removes_test_autostart" $false "decommission_check_unavailable_after_restart_failure"
        Set-LifecycleCheck "restart_quits_cleanly" $false "final_quit_unavailable_after_restart_failure"
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
        foreach ($process in Get-PrimaryProcesses) {
            Stop-Process -Id $process.ProcessId -Force -ErrorAction SilentlyContinue
        }
        foreach ($process in Get-DesktopProcesses) {
            if ($process.CommandLine -match "--threads-desktop-mock-runtime") {
                Stop-Process -Id $process.ProcessId -Force -ErrorAction SilentlyContinue
            }
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
    $env:APPDATA = $previousAppData
    if (Test-Path $appDataRoot) { Remove-Item -LiteralPath $appDataRoot -Recurse -Force -ErrorAction SilentlyContinue }
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
