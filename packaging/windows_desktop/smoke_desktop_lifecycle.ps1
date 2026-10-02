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
$head = (& git -C $repoRoot rev-parse HEAD).Trim()
$dirty = (& git -C $repoRoot status --porcelain).Trim()
if ($head -ne $ExpectedSourceRevision -or $dirty) {
    throw "lifecycle_smoke_requires_exact_clean_source_revision"
}

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
$runKeyPath = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"
$originalAutostart = @{}
if (Test-Path $runKeyPath) {
    $runValues = Get-ItemProperty -LiteralPath $runKeyPath
    foreach ($property in $runValues.PSObject.Properties) {
        if ($property.Name -notlike "PS*" -and [string]$property.Value -like "*$BundleExecutable*") {
            $originalAutostart[$property.Name] = [string]$property.Value
        }
    }
}

$checks = [ordered]@{
    concurrent_launch_keeps_one_primary = $false
    user_can_provision_controller_once = $false
    autostart_enabled_for_current_user = $false
    controller_starts_one_mock_helper = $false
    second_launch_reuses_and_focuses_instance = $false
    window_close_hides_without_stopping_helper = $false
    tray_quit_confirms_and_stops_helper_before_exit = $false
    restart_restores_role_without_duplicate_helper = $false
    decommission_removes_test_autostart = $false
    restart_quits_cleanly = $false
}
$helperProcessIds = [System.Collections.Generic.List[int]]::new()
$startedProcessIds = [System.Collections.Generic.List[int]]::new()
$failureCode = $null
$result = "BLOCKER"
$primaryId = $null
$primaryWindowHandle = [IntPtr]::Zero

function Get-DesktopProcesses {
    @(Get-CimInstance Win32_Process -Filter "Name='threads-desktop.exe'" -ErrorAction Stop |
        Where-Object { $_.ExecutablePath -eq $BundleExecutable })
}

function Get-PrimaryProcesses {
    @(Get-DesktopProcesses | Where-Object { $_.CommandLine -notmatch "--threads-desktop-mock-runtime" })
}

function Get-HelperProcesses([int]$ParentId) {
    @(Get-DesktopProcesses | Where-Object {
        $_.ParentProcessId -eq $ParentId -and $_.CommandLine -match "--threads-desktop-mock-runtime"
    })
}

function Get-BundleAutostartEntries {
    if (-not (Test-Path $runKeyPath)) { return @() }
    $runValues = Get-ItemProperty -LiteralPath $runKeyPath
    @($runValues.PSObject.Properties | Where-Object {
        $_.Name -notlike "PS*" -and [string]$_.Value -like "*$BundleExecutable*"
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
    New-Item -ItemType Directory -Path $appDataRoot -Force | Out-Null
    $env:APPDATA = $appDataRoot

    $first = Start-Process -FilePath $BundleExecutable -PassThru
    $second = Start-Process -FilePath $BundleExecutable -PassThru
    $startedProcessIds.Add($first.Id)
    $startedProcessIds.Add($second.Id)
    Wait-Until { (Get-PrimaryProcesses).Count -eq 1 } 25 "concurrent_launch_did_not_converge_to_one_primary"
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
    $checks.concurrent_launch_keeps_one_primary = (Get-PrimaryProcesses).Count -eq 1

    Invoke-Button "Provision as Controller"
    Wait-Until { (Get-HelperProcesses $primaryId).Count -eq 1 } 10 "controller_mock_helper_did_not_start"
    $helperId = [int](Get-HelperProcesses $primaryId | Select-Object -First 1).ProcessId
    $helperProcessIds.Add($helperId)
    $checks.user_can_provision_controller_once = $true
    $checks.autostart_enabled_for_current_user = (Get-BundleAutostartEntries).Count -gt 0
    $checks.controller_starts_one_mock_helper = (Get-HelperProcesses $primaryId).Count -eq 1
    if (-not $checks.autostart_enabled_for_current_user) { throw "controller_autostart_not_registered" }

    $again = Start-Process -FilePath $BundleExecutable -PassThru
    $startedProcessIds.Add($again.Id)
    Wait-Until { $again.HasExited } 20 "second_invocation_did_not_exit_after_focus"
    $process = Get-Process -Id $primaryId -ErrorAction Stop
    $process.Refresh()
    $checks.second_launch_reuses_and_focuses_instance =
        (Get-PrimaryProcesses).Count -eq 1 -and
        [ThreadsDesktopLifecycleSmoke.NativeMethods]::IsWindowVisible($primaryWindowHandle) -and
        [ThreadsDesktopLifecycleSmoke.NativeMethods]::GetForegroundWindow() -eq $primaryWindowHandle -and
        (Get-HelperProcesses $primaryId).Count -eq 1 -and
        [int](Get-HelperProcesses $primaryId | Select-Object -First 1).ProcessId -eq $helperId
    if (-not $checks.second_launch_reuses_and_focuses_instance) { throw "second_launch_did_not_reuse_primary" }

    if (-not [ThreadsDesktopLifecycleSmoke.NativeMethods]::PostMessage(
        $primaryWindowHandle, 0x0010, [IntPtr]::Zero, [IntPtr]::Zero
    )) { throw "desktop_close_request_failed" }
    Wait-Until { -not [ThreadsDesktopLifecycleSmoke.NativeMethods]::IsWindowVisible($primaryWindowHandle) } 10 "desktop_close_did_not_hide_window"
    $checks.window_close_hides_without_stopping_helper =
        (Get-Process -Id $primaryId -ErrorAction SilentlyContinue) -and
        (Get-Process -Id $helperId -ErrorAction SilentlyContinue) -and
        (Get-HelperProcesses $primaryId).Count -eq 1
    if (-not $checks.window_close_hides_without_stopping_helper) { throw "window_close_stopped_mock_helper" }

    $reopen = Start-Process -FilePath $BundleExecutable -PassThru
    $startedProcessIds.Add($reopen.Id)
    Wait-Until { $reopen.HasExited } 20 "reopen_invocation_did_not_exit_after_focus"
    Wait-Until { [ThreadsDesktopLifecycleSmoke.NativeMethods]::IsWindowVisible($primaryWindowHandle) } 10 "second_launch_did_not_reopen_hidden_window"
    if ((Get-HelperProcesses $primaryId).Count -ne 1) { throw "reopen_duplicated_mock_helper" }

    Confirm-TrayQuit $helperId
    $checks.tray_quit_confirms_and_stops_helper_before_exit = $true

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
    $checks.restart_restores_role_without_duplicate_helper =
        [bool]$controllerHeading -and (Get-HelperProcesses $primaryId).Count -eq 1
    if (-not $checks.restart_restores_role_without_duplicate_helper) { throw "restart_did_not_restore_single_controller" }

    Invoke-Button "Decommission device"
    $phraseInput = Find-FirstByControlType (Get-PrimaryWindow) ([System.Windows.Automation.ControlType]::Edit)
    if (-not $phraseInput) { throw "decommission_confirmation_input_unavailable" }
    $phraseInput.GetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern).SetValue("RESET THIS DEVICE")
    Invoke-Button "Decommission"
    Wait-Until { (Get-HelperProcesses $primaryId).Count -eq 0 } 10 "decommission_did_not_stop_mock_helper"
    $checks.decommission_removes_test_autostart = (Get-BundleAutostartEntries).Count -eq 0
    if (-not $checks.decommission_removes_test_autostart) { throw "decommission_left_autostart_registered" }
    Confirm-TrayQuit 0
    $checks.restart_quits_cleanly = $true
    $result = if (@($checks.Values | Where-Object { -not $_ }).Count -eq 0) { "PASS" } else { "BLOCKER" }
} catch {
    $failureCode = [regex]::Replace($_.Exception.Message, "[^A-Za-z0-9_.-]", "_")
} finally {
    foreach ($process in Get-PrimaryProcesses) {
        Stop-Process -Id $process.ProcessId -Force -ErrorAction SilentlyContinue
    }
    foreach ($process in Get-DesktopProcesses) {
        if ($process.CommandLine -match "--threads-desktop-mock-runtime") {
            Stop-Process -Id $process.ProcessId -Force -ErrorAction SilentlyContinue
        }
    }
    if (Test-Path $runKeyPath) {
        $currentValues = Get-ItemProperty -LiteralPath $runKeyPath
        foreach ($property in $currentValues.PSObject.Properties) {
            if ($property.Name -notlike "PS*" -and [string]$property.Value -like "*$BundleExecutable*" -and
                -not $originalAutostart.ContainsKey($property.Name)) {
                Remove-ItemProperty -LiteralPath $runKeyPath -Name $property.Name -ErrorAction SilentlyContinue
            }
        }
        foreach ($name in $originalAutostart.Keys) {
            Set-ItemProperty -LiteralPath $runKeyPath -Name $name -Value $originalAutostart[$name]
        }
    }
    $env:APPDATA = $previousAppData
    if (Test-Path $appDataRoot) { Remove-Item -LiteralPath $appDataRoot -Recurse -Force -ErrorAction SilentlyContinue }
    $evidenceDirectory = Split-Path -Parent $EvidencePath
    New-Item -ItemType Directory -Path $evidenceDirectory -Force | Out-Null
    $evidence = [ordered]@{
        schema_version = 1
        source_revision = $ExpectedSourceRevision
        source_worktree_clean = -not [bool]$dirty
        host_windows_version = [Environment]::OSVersion.Version.ToString()
        architecture_x64 = [Environment]::Is64BitOperatingSystem
        current_user_is_administrator = [Security.Principal.WindowsPrincipal]::new(
            [Security.Principal.WindowsIdentity]::GetCurrent()
        ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
        executable_sha256 = $bundleHash
        checks = $checks
        helper_process_ids = @($helperProcessIds)
        result = $result
        failure_code = $failureCode
    }
    [System.IO.File]::WriteAllText(
        $EvidencePath,
        ($evidence | ConvertTo-Json -Depth 8),
        [System.Text.UTF8Encoding]::new($false)
    )
}

if ($result -ne "PASS") { throw "DX-02 Windows lifecycle smoke blocked: $failureCode" }
Write-Output "DX-02 Windows lifecycle smoke PASS for $ExpectedSourceRevision"
