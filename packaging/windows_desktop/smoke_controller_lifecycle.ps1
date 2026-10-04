[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$DesktopExecutable,
    [Parameter(Mandatory = $true)]
    [string]$RuntimeRoot,
    [Parameter(Mandatory = $true)]
    [string]$ExpectedSourceRevision,
    [Parameter(Mandatory = $true)]
    [string]$EvidencePath,
    [Parameter(Mandatory = $true)]
    [ValidateSet(
        "bootstrap_https_cutover_tray",
        "restart_renewal",
        "database_crash_recovery",
        "parent_crash_recovery",
        "migration_recovery_auth",
        "database_port_collision",
        "endpoint_port_collision",
        "unowned_root",
        "unwritable_root",
        "corrupt_cluster"
    )]
    [string]$Scenario,
    [Parameter()]
    [string]$VerifiedSourceRevision,
    [Parameter()]
    [switch]$VerifiedWorktreeClean
)

$ErrorActionPreference = "Stop"
$PSNativeCommandUseErrorActionPreference = $false
$privateRootProbePath = Join-Path $PSScriptRoot "controller_https_probe.ps1"
if (-not (Test-Path -LiteralPath $privateRootProbePath -PathType Leaf)) {
    throw "controller_https_probe_script_missing"
}
. $privateRootProbePath
$loginEvidencePath = Join-Path $PSScriptRoot "operator_login_evidence.ps1"
if (-not (Test-Path -LiteralPath $loginEvidencePath -PathType Leaf)) {
    throw "controller_login_evidence_script_missing"
}
. $loginEvidencePath
$DesktopExecutable = (Resolve-Path -LiteralPath $DesktopExecutable).Path
$RuntimeRoot = (Resolve-Path -LiteralPath $RuntimeRoot).Path
$EvidencePath = [System.IO.Path]::GetFullPath($EvidencePath)
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "../../")).Path

Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
Add-Type -AssemblyName System.Security
Add-Type -AssemblyName System.Windows.Forms
if (-not ("ThreadsControllerSmoke.NativeMethods" -as [type])) {
    Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;

namespace ThreadsControllerSmoke {
    public static class NativeMethods {
        [StructLayout(LayoutKind.Sequential)]
        private struct FileTime {
            public uint Low;
            public uint High;
        }

        [DllImport("user32.dll", SetLastError = true)]
        public static extern bool PostMessage(IntPtr window, uint message, IntPtr wParam, IntPtr lParam);
        [DllImport("user32.dll", SetLastError = true)]
        public static extern bool IsWindowVisible(IntPtr window);
        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool GetProcessTimes(
            IntPtr process,
            out FileTime creationTime,
            out FileTime exitTime,
            out FileTime kernelTime,
            out FileTime userTime
        );
        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern IntPtr OpenProcess(uint desiredAccess, bool inheritHandle, uint processId);
        [DllImport("kernel32.dll", SetLastError = true)]
        public static extern bool CloseHandle(IntPtr handle);

        public static IntPtr OpenProcessForExitTime(uint processId) {
            return OpenProcess(0x1000, false, processId);
        }

        public static long GetProcessExitFileTime(IntPtr process) {
            FileTime creationTime;
            FileTime exitTime;
            FileTime kernelTime;
            FileTime userTime;
            if (!GetProcessTimes(process, out creationTime, out exitTime, out kernelTime, out userTime)) {
                return 0L;
            }
            return ((long)exitTime.High << 32) | exitTime.Low;
        }
    }
}
"@
}

$runtimeExecutable = Join-Path $RuntimeRoot "threads-runtime\threads-runtime.exe"
$postgresBin = Join-Path $RuntimeRoot "postgresql\bin"
$pgCtl = Join-Path $postgresBin "pg_ctl.exe"
$psql = Join-Path $postgresBin "psql.exe"
$pgControlData = Join-Path $postgresBin "pg_controldata.exe"
$identifier = "com.pumni.threads-desktop"
$configDirectory = Join-Path ([Environment]::GetFolderPath([Environment+SpecialFolder]::ApplicationData)) $identifier
$configPath = Join-Path $configDirectory "device-config.json"
$localAppData = [Environment]::GetFolderPath([Environment+SpecialFolder]::LocalApplicationData)
$controllerRoot = Join-Path (Join-Path $localAppData $identifier) "Controller"
$savedRoot = "$controllerRoot.dx04-saved"
$unownedRoot = "$controllerRoot.dx04-unowned"
$deviceConfigExisted = Test-Path -LiteralPath $configPath -PathType Leaf
$controllerRootExisted = Test-Path -LiteralPath $controllerRoot -PathType Container
$applicationRunKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"
$originalRunEntries = @{}
$desktopPids = [System.Collections.Generic.List[int]]::new()
$failureCodes = [System.Collections.Generic.List[string]]::new()
$checks = [ordered]@{
    clean_profile = $false
    runtime_bundle_shared = $false
    controller_root_current_user_acl = $false
    dpapi_current_user_round_trip = $false
    atomic_non_secret_config = $false
    controller_https_configuration_persisted = $false
    endpoint_reconfigure_before_owner = $false
    endpoint_unavailable_ip_rolls_back = $false
    endpoint_collision_rolls_back = $false
    endpoint_running_unavailable_ip_rolls_back = $false
    endpoint_running_unavailable_ip_preserves_owner_session = $false
    endpoint_running_collision_rolls_back = $false
    endpoint_running_collision_preserves_owner_session = $false
    endpoint_running_transition_preserves_postgres = $false
    endpoint_running_transition_revokes_owner_session = $false
    endpoint_running_transition_reauthenticates_one_owner_session = $false
    no_lan_listener_before_local_owner_bootstrap = $false
    loopback_postgres_wildcard_https_listener = $false
    controller_https_root_fingerprint_matches_ui = $false
    local_readiness_uses_private_root = $false
    plaintext_health_rejected = $false
    serving_leaf_key_cleaned_on_shutdown = $false
    stale_serving_leaf_key_replaced_on_startup = $false
    leaf_renewal_preserves_root_identity = $false
    root_identity_persists_across_restart = $false
    no_owner_or_lan_bootstrap = $false
    local_first_owner_bootstrap = $false
    separate_http_and_scheduler_processes = $false
    x_hides_and_runtime_continues = $false
    reopen_keeps_one_runtime_and_database_identity = $false
    reopen_requires_operator_sign_in = $false
    owner_reauthenticated_after_reopen = $false
    active_session_verified_before_privileged_quit = $false
    graceful_quit_stops_scheduler_http_then_postgres = $false
    relaunch_preserves_database_and_endpoint = $false
    database_crash_fails_closed_and_recovers_wal = $false
    desktop_parent_crash_owns_process_tree_and_recovers_wal = $false
    controller_root_identity_survives_crash_restart = $false
    failed_migration_preserves_existing_cluster = $false
    database_port_collision_does_not_rotate = $false
    endpoint_port_collision_does_not_rotate = $false
    unowned_root_is_preserved_and_rejected = $false
    unwritable_root_is_rejected = $false
    corrupt_cluster_is_preserved_and_rejected = $false
}
$commonScenarioChecks = @(
    "clean_profile",
    "runtime_bundle_shared",
    "controller_root_current_user_acl",
    "dpapi_current_user_round_trip",
    "atomic_non_secret_config",
    "controller_https_configuration_persisted",
    "no_lan_listener_before_local_owner_bootstrap",
    "controller_https_root_fingerprint_matches_ui",
    "no_owner_or_lan_bootstrap",
    "local_first_owner_bootstrap",
    "separate_http_and_scheduler_processes",
    "loopback_postgres_wildcard_https_listener",
    "local_readiness_uses_private_root",
    "plaintext_health_rejected"
)
$scenarioSpecificChecks = @{
    bootstrap_https_cutover_tray = @(
        "endpoint_reconfigure_before_owner",
        "endpoint_unavailable_ip_rolls_back",
        "endpoint_collision_rolls_back",
        "endpoint_running_unavailable_ip_rolls_back",
        "endpoint_running_unavailable_ip_preserves_owner_session",
        "endpoint_running_collision_rolls_back",
        "endpoint_running_collision_preserves_owner_session",
        "endpoint_running_transition_preserves_postgres",
        "endpoint_running_transition_revokes_owner_session",
        "endpoint_running_transition_reauthenticates_one_owner_session",
        "x_hides_and_runtime_continues",
        "reopen_keeps_one_runtime_and_database_identity",
        "reopen_requires_operator_sign_in",
        "owner_reauthenticated_after_reopen",
        "active_session_verified_before_privileged_quit",
        "graceful_quit_stops_scheduler_http_then_postgres",
        "serving_leaf_key_cleaned_on_shutdown"
    )
    restart_renewal = @(
        "root_identity_persists_across_restart",
        "leaf_renewal_preserves_root_identity",
        "stale_serving_leaf_key_replaced_on_startup",
        "serving_leaf_key_cleaned_on_shutdown",
        "relaunch_preserves_database_and_endpoint"
    )
    database_crash_recovery = @("database_crash_fails_closed_and_recovers_wal")
    parent_crash_recovery = @(
        "desktop_parent_crash_owns_process_tree_and_recovers_wal",
        "controller_root_identity_survives_crash_restart"
    )
    migration_recovery_auth = @(
        "failed_migration_preserves_existing_cluster",
        "active_session_verified_before_privileged_quit",
        "graceful_quit_stops_scheduler_http_then_postgres"
    )
    database_port_collision = @("database_port_collision_does_not_rotate")
    endpoint_port_collision = @("endpoint_port_collision_does_not_rotate")
    unowned_root = @("unowned_root_is_preserved_and_rejected")
    unwritable_root = @("unwritable_root_is_rejected")
    corrupt_cluster = @("corrupt_cluster_is_preserved_and_rejected")
}
$scenarioCheckNames = @($commonScenarioChecks + $scenarioSpecificChecks[$Scenario] | Select-Object -Unique)
$result = "BLOCKER"
$failureCode = $null
$failureScriptLine = $null
$head = $null
$worktreeIsClean = $false
$controllerIdentity = $null
$databaseSystemIdentifier = $null
$shutdownExitOrder = $null
$sentinel = [Guid]::NewGuid().ToString("N")
$script:smokeOwnerUsername = "dx05owner" + [Guid]::NewGuid().ToString("N").Substring(0, 12)
$script:smokeOwnerPassword = "Dx05Owner" + [Guid]::NewGuid().ToString("N")
$processEvidence = [ordered]@{}
$scenarioEvidence = [ordered]@{}
$operatorSessionTimeline = [System.Collections.Generic.List[object]]::new()
$operatorLoginAttemptTimeline = [System.Collections.Generic.List[object]]::new()
$inputMutationEvidence = [System.Collections.Generic.List[object]]::new()
$controllerRuntimeIdentityTimeline = [System.Collections.Generic.List[object]]::new()
$endpointCollisionEvidence = $null
$rootWasMoved = $false
$parentCrashPids = @()
$parentCrashProcesses = @()
$aclBeforeDeny = $null
$pgVersionBackup = $null
$previousRuntimeDirectory = $env:THREADS_DESKTOP_RUNTIME_DIR
$previousPsqlPassword = $env:PGPASSWORD

function Add-Failure([string]$Code) {
    if ([string]::IsNullOrWhiteSpace($Code)) { return }
    $safe = [regex]::Replace($Code, "[^A-Za-z0-9_.-]", "_")
    if (-not $failureCodes.Contains($safe)) { $failureCodes.Add($safe) }
    if (-not $script:failureCode) { $script:failureCode = $safe }
}

function Set-Check([string]$Name, [bool]$Passed, [string]$Failure) {
    $checks[$Name] = $Passed
    if (-not $Passed) { Add-Failure $Failure }
}

function Wait-Until([scriptblock]$Condition, [int]$TimeoutSeconds, [string]$Failure) {
    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    while ([DateTime]::UtcNow -lt $deadline) {
        if (& $Condition) { return }
        Start-Sleep -Milliseconds 200
    }
    throw $Failure
}

function Get-DesktopProcesses {
    @(Get-CimInstance Win32_Process -Filter "Name='threads-desktop.exe'" -ErrorAction Stop |
        Where-Object { $_.ExecutablePath -eq $DesktopExecutable })
}

function Get-ControllerProcesses {
    $processes = @(Get-CimInstance Win32_Process -ErrorAction Stop)
    $postgres = @($processes | Where-Object {
        $_.ExecutablePath -and $_.ExecutablePath -ieq (Join-Path $postgresBin "postgres.exe") -and
        $_.CommandLine -like "*$controllerRoot*"
    })
    $postgresLeader = @($postgres | Where-Object {
        [int]$_.ParentProcessId -eq $script:primaryId
    })
    $runtime = @($processes | Where-Object {
        $_.ExecutablePath -and $_.ExecutablePath -ieq $runtimeExecutable -and
        $_.CommandLine -match 'threads-runtime\.exe"?\s+(?:http|scheduler)(?:\s|$)'
    })
    $http = @($runtime | Where-Object {
        $_.CommandLine -match 'threads-runtime\.exe"?\s+http(?:\s|$)' -and
        [int]$_.ParentProcessId -eq $script:primaryId
    })
    $scheduler = @($runtime | Where-Object {
        $_.CommandLine -match 'threads-runtime\.exe"?\s+scheduler(?:\s|$)' -and
        [int]$_.ParentProcessId -eq $script:primaryId
    })
    return [pscustomobject]@{
        postgres = $postgresLeader
        postgres_tree = $postgres
        http = $http
        scheduler = $scheduler
        all = @($postgres) + @($runtime)
    }
}

function Get-ProcessDescendants([int[]]$RootProcessIds) {
    $processes = @(Get-CimInstance Win32_Process -ErrorAction Stop)
    $seen = [System.Collections.Generic.HashSet[int]]::new()
    $frontier = [System.Collections.Generic.List[int]]::new()
    foreach ($rootProcessId in $RootProcessIds) {
        if ($seen.Add($rootProcessId)) { $frontier.Add($rootProcessId) }
    }
    $descendants = [System.Collections.Generic.List[object]]::new()
    while ($frontier.Count -gt 0) {
        $next = [System.Collections.Generic.List[int]]::new()
        foreach ($process in $processes) {
            if ($frontier.Contains([int]$process.ParentProcessId) -and
                $seen.Add([int]$process.ProcessId)) {
                $descendants.Add($process)
                $next.Add([int]$process.ProcessId)
            }
        }
        $frontier = $next
    }
    return @($descendants)
}

function Get-TrackedProcess([int]$ProcessId, [string]$ExpectedPath) {
    $process = Get-Process -Id $ProcessId -ErrorAction SilentlyContinue
    if (-not $process) { return $null }
    try {
        if ($process.Path -ine $ExpectedPath) {
            $process.Dispose()
            return $null
        }
        $null = $process.StartTime
        return $process
    } catch {
        $process.Dispose()
        return $null
    }
}

function Get-ProcessIfPresent([int]$ProcessId) {
    try {
        return Get-Process -Id $ProcessId -ErrorAction Stop
    } catch {
        if ($_.FullyQualifiedErrorId -like "NoProcessFoundForGivenId,*") { return $null }
        throw
    }
}

function Test-ElementUnavailable([System.Exception]$Exception) {
    while ($null -ne $Exception) {
        if ($Exception -is [System.Windows.Automation.ElementNotAvailableException]) {
            return $true
        }
        $Exception = $Exception.InnerException
    }
    return $false
}

function Test-ProcessExitRace(
    [System.Exception]$Exception,
    [System.Diagnostics.Process]$Process
) {
    $processAccessFailure = $false
    while ($null -ne $Exception) {
        if ($Exception -is [System.InvalidOperationException] -or
            $Exception -is [System.ComponentModel.Win32Exception]) {
            $processAccessFailure = $true
            break
        }
        $Exception = $Exception.InnerException
    }
    if (-not $processAccessFailure) { return $false }
    try { return $Process.HasExited } catch { return $false }
}

function Get-SafeExceptionTypeName([System.Exception]$Exception) {
    if (-not $Exception) { return $null }
    return [regex]::Replace($Exception.GetType().Name, "[^A-Za-z0-9_.+]", "_")
}

function Get-Window([int]$ProcessId) {
    $process = Get-ProcessIfPresent $ProcessId
    if (-not $process) { return $null }
    try {
        try {
            $process.Refresh()
            if ($process.HasExited) { return $null }
            $windowHandle = $process.MainWindowHandle
            if ($windowHandle -eq [IntPtr]::Zero) { return $null }
            return [System.Windows.Automation.AutomationElement]::FromHandle($windowHandle)
        } catch {
            if (Test-ElementUnavailable $_.Exception) { return $null }
            if (Test-ProcessExitRace $_.Exception $process) { return $null }
            throw
        }
    } finally {
        $process.Dispose()
    }
}

function Find-Element(
    [System.Windows.Automation.AutomationElement]$Root,
    [string]$Name,
    [System.Windows.Automation.ControlType]$ControlType
) {
    if (-not $Root) { return $null }
    $conditions = @(
        [System.Windows.Automation.PropertyCondition]::new(
            [System.Windows.Automation.AutomationElement]::NameProperty, $Name
        ),
        [System.Windows.Automation.PropertyCondition]::new(
            [System.Windows.Automation.AutomationElement]::ControlTypeProperty, $ControlType
        )
    )
    $condition = [System.Windows.Automation.AndCondition]::new($conditions)
    try {
        return $Root.FindFirst([System.Windows.Automation.TreeScope]::Descendants, $condition)
    } catch {
        if (Test-ElementUnavailable $_.Exception) { return $null }
        throw
    }
}

function Find-ElementsByName(
    [System.Windows.Automation.AutomationElement]$Root,
    [string]$Name
) {
    if (-not $Root) { return $null }
    $condition = [System.Windows.Automation.PropertyCondition]::new(
        [System.Windows.Automation.AutomationElement]::NameProperty, $Name
    )
    try {
        return @($Root.FindAll([System.Windows.Automation.TreeScope]::Descendants, $condition))
    } catch {
        if (Test-ElementUnavailable $_.Exception) { return @() }
        throw
    }
}

function Find-ElementsByAutomationIdAndType(
    [System.Windows.Automation.AutomationElement]$Root,
    [string]$AutomationId,
    [System.Windows.Automation.ControlType]$ControlType
) {
    if (-not $Root -or [string]::IsNullOrWhiteSpace($AutomationId)) { return @() }
    $conditions = @(
        [System.Windows.Automation.PropertyCondition]::new(
            [System.Windows.Automation.AutomationElement]::AutomationIdProperty, $AutomationId
        ),
        [System.Windows.Automation.PropertyCondition]::new(
            [System.Windows.Automation.AutomationElement]::ControlTypeProperty, $ControlType
        )
    )
    $condition = [System.Windows.Automation.AndCondition]::new($conditions)
    try {
        return @($Root.FindAll([System.Windows.Automation.TreeScope]::Descendants, $condition))
    } catch {
        if (Test-ElementUnavailable $_.Exception) { return @() }
        throw
    }
}

function Find-ElementByType(
    [System.Windows.Automation.AutomationElement]$Root,
    [System.Windows.Automation.ControlType]$ControlType
) {
    if (-not $Root) { return $null }
    $condition = [System.Windows.Automation.PropertyCondition]::new(
        [System.Windows.Automation.AutomationElement]::ControlTypeProperty, $ControlType
    )
    try {
        return $Root.FindFirst([System.Windows.Automation.TreeScope]::Descendants, $condition)
    } catch {
        if (Test-ElementUnavailable $_.Exception) { return $null }
        throw
    }
}

function Test-ElementSupportsPattern(
    [System.Windows.Automation.AutomationElement]$Element,
    [System.Windows.Automation.AutomationPattern]$Pattern
) {
    if (-not $Element) { return $false }
    $patternObject = $null
    try {
        return [bool]$Element.TryGetCurrentPattern($Pattern, [ref]$patternObject)
    } catch { return $false }
}

function Test-InteractiveInputElement(
    [System.Windows.Automation.AutomationElement]$Element,
    [string]$FieldId
) {
    if (-not $Element) { return $false }
    try {
        $current = $Element.Current
        if (-not $current.IsKeyboardFocusable -or -not $current.IsEnabled) { return $false }
        switch ($current.ControlType.ProgrammaticName) {
            "ControlType.Spinner" {
                $hasRangeValue = Test-ElementSupportsPattern $Element `
                    ([System.Windows.Automation.RangeValuePattern]::Pattern)
                if ($hasRangeValue) { return $true }
                $childEdit = Find-ElementByType $Element `
                    ([System.Windows.Automation.ControlType]::Edit)
                if (-not $childEdit) { return $false }
                $childCurrent = $childEdit.Current
                return $childCurrent.IsKeyboardFocusable -and $childCurrent.IsEnabled -and `
                    (Test-ElementSupportsPattern $childEdit `
                        ([System.Windows.Automation.ValuePattern]::Pattern))
            }
            "ControlType.Edit" {
                if ($FieldId -eq "password") { return $true }
                return Test-ElementSupportsPattern $Element `
                    ([System.Windows.Automation.ValuePattern]::Pattern)
            }
            default { return $false }
        }
    } catch { return $false }
}

function Resolve-InputControl(
    [int]$ProcessId,
    [string]$Name,
    [string]$AutomationId,
    [System.Windows.Automation.ControlType[]]$AllowedControlTypes,
    [string]$FieldId
) {
    $window = Get-Window $ProcessId
    if (-not $window) {
        return [pscustomobject]@{ Control = $null; Ambiguous = $false }
    }

    if (-not [string]::IsNullOrWhiteSpace($AutomationId)) {
        $automationIdMatches = @(
            foreach ($controlType in $AllowedControlTypes) {
                Find-ElementsByAutomationIdAndType $window $AutomationId $controlType
            }
        )
        $interactiveIdMatches = @($automationIdMatches | Where-Object {
            Test-InteractiveInputElement $_ $FieldId
        })
        if ($interactiveIdMatches.Count -eq 1) {
            return [pscustomobject]@{ Control = $interactiveIdMatches[0]; Ambiguous = $false }
        }
        if ($interactiveIdMatches.Count -gt 1) {
            return [pscustomobject]@{ Control = $null; Ambiguous = $true }
        }
    }

    $nameMatches = @(Find-ElementsByName $window $Name)
    $interactiveNameMatches = @($nameMatches | Where-Object {
        $controlType = $_.Current.ControlType
        ($AllowedControlTypes -contains $controlType) -and
            (Test-InteractiveInputElement $_ $FieldId)
    })
    if ($interactiveNameMatches.Count -eq 1) {
        return [pscustomobject]@{ Control = $interactiveNameMatches[0]; Ambiguous = $false }
    }
    return [pscustomobject]@{
        Control = $null
        Ambiguous = $interactiveNameMatches.Count -gt 1
    }
}

function Get-InputLookupDiagnostics(
    [int]$ProcessId,
    [string]$Name,
    [string]$AutomationId,
    [System.Windows.Automation.ControlType[]]$AllowedControlTypes
) {
    $window = Get-Window $ProcessId
    $elements = @(Find-ElementsByName $window $Name)
    $matches = @(
        foreach ($element in $elements) {
            $nameValue = $null
            $automationIdValue = $null
            $controlTypeName = "unknown"
            $keyboardFocusable = $null
            $enabled = $null
            try {
                $current = $element.Current
                $nameValue = [string]$current.Name
                $automationIdValue = [string]$current.AutomationId
                $controlTypeName = [string]$current.ControlType.ProgrammaticName
                $keyboardFocusable = [bool]$current.IsKeyboardFocusable
                $enabled = [bool]$current.IsEnabled
            } catch { }

            [ordered]@{
                name = $nameValue
                automation_id = $automationIdValue
                control_type = $controlTypeName
                is_keyboard_focusable = $keyboardFocusable
                is_enabled = $enabled
                supported_patterns = [ordered]@{
                    ValuePattern = Test-ElementSupportsPattern $element `
                        ([System.Windows.Automation.ValuePattern]::Pattern)
                    RangeValuePattern = Test-ElementSupportsPattern $element `
                        ([System.Windows.Automation.RangeValuePattern]::Pattern)
                    TextPattern = Test-ElementSupportsPattern $element `
                        ([System.Windows.Automation.TextPattern]::Pattern)
                    InvokePattern = Test-ElementSupportsPattern $element `
                        ([System.Windows.Automation.InvokePattern]::Pattern)
                    LegacyIAccessiblePattern = Test-ElementSupportsPattern $element `
                        ([System.Windows.Automation.LegacyIAccessiblePattern]::Pattern)
                }
            }
        }
    )
    return [ordered]@{
        expected_name = $Name
        expected_automation_id = $AutomationId
        allowed_control_types = @($AllowedControlTypes | ForEach-Object { $_.ProgrammaticName })
        exact_name_matches = $matches
    }
}

function Get-ElementName([System.Windows.Automation.AutomationElement]$Element) {
    if (-not $Element) { return $null }
    try {
        return [string]$Element.Current.Name
    } catch {
        if (Test-ElementUnavailable $_.Exception) { return $null }
        throw
    }
}

function Find-TextContaining([System.Windows.Automation.AutomationElement]$Root, [string]$Text) {
    if (-not $Root) { return $null }
    try {
        $elements = $Root.FindAll(
            [System.Windows.Automation.TreeScope]::Descendants,
            [System.Windows.Automation.Condition]::TrueCondition
        )
        foreach ($element in $elements) {
            $name = Get-ElementName $element
            if ($name -and
                $name.IndexOf($Text, [System.StringComparison]::OrdinalIgnoreCase) -ge 0) {
                return $element
            }
        }
    } catch {
        if (Test-ElementUnavailable $_.Exception) { return $null }
        throw
    }
    return $null
}

function Invoke-Button([int]$ProcessId, [string]$Name, [scriptblock]$BeforeInvoke) {
    $window = Get-Window $ProcessId
    if ($window) {
        try { $window.SetFocus() } catch { }
    }
    Wait-Until {
        $button = Find-Element (Get-Window $ProcessId) $Name `
            ([System.Windows.Automation.ControlType]::Button)
        if (-not $button) { return $false }
        try {
            $pattern = $button.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern)
        } catch { return $false }
        if (-not $pattern) { return $false }
        if ($BeforeInvoke) { & $BeforeInvoke }
        try {
            $pattern.Invoke()
            return $true
        } catch { return $false }
    } 20 "desktop_button_unavailable_$($Name -replace '\W+', '_')"
}

function Add-InputMutationEvidence([object]$Evidence) {
    $script:inputMutationEvidence.Add($Evidence) | Out-Null
}

function Send-InputKeyboardValue([string]$Value) {
    [System.Windows.Forms.SendKeys]::SendWait("^a")
    [System.Windows.Forms.SendKeys]::SendWait("{BACKSPACE}")
    [System.Windows.Forms.SendKeys]::SendWait($Value)
}

function Test-ResolvedInputValue(
    [scriptblock]$ResolveControl,
    [string]$FieldId,
    [string]$ExpectedValue,
    [int]$ExpectedPort
) {
    $current = & $ResolveControl
    if (-not $current) {
        return [pscustomobject]@{ Matched = $false; ObservedLength = $null }
    }

    $controlTypeName = [string]$current.Current.ControlType.ProgrammaticName
    if ($controlTypeName -eq "ControlType.Spinner") {
        try {
            $rangePattern = $current.GetCurrentPattern(
                [System.Windows.Automation.RangeValuePattern]::Pattern
            )
            return [pscustomobject]@{
                Matched = [double]$rangePattern.Current.Value -eq [double]$ExpectedPort
                ObservedLength = $null
            }
        } catch {
            $childEdit = Find-ElementByType $current `
                ([System.Windows.Automation.ControlType]::Edit)
            if (-not $childEdit) {
                return [pscustomobject]@{ Matched = $false; ObservedLength = $null }
            }
            try {
                $valuePattern = $childEdit.GetCurrentPattern(
                    [System.Windows.Automation.ValuePattern]::Pattern
                )
                $actualPort = 0
                $parsedPort = [int]::TryParse(
                    [string]$valuePattern.Current.Value,
                    [System.Globalization.NumberStyles]::Integer,
                    [System.Globalization.CultureInfo]::InvariantCulture,
                    [ref]$actualPort
                )
                return [pscustomobject]@{
                    Matched = $parsedPort -and $actualPort -eq $ExpectedPort
                    ObservedLength = $null
                }
            } catch {
                return [pscustomobject]@{ Matched = $false; ObservedLength = $null }
            }
        }
    }

    try {
        $valuePattern = $current.GetCurrentPattern(
            [System.Windows.Automation.ValuePattern]::Pattern
        )
        $actualValue = [string]$valuePattern.Current.Value
        if ($FieldId -eq "https_port") {
            $actualPort = 0
            $parsedPort = [int]::TryParse(
                $actualValue,
                [System.Globalization.NumberStyles]::Integer,
                [System.Globalization.CultureInfo]::InvariantCulture,
                [ref]$actualPort
            )
            return [pscustomobject]@{
                Matched = $parsedPort -and $actualPort -eq $ExpectedPort
                ObservedLength = $null
            }
        }
        return [pscustomobject]@{
            Matched = [string]::Equals(
                $actualValue,
                $ExpectedValue,
                [System.StringComparison]::Ordinal
            )
            ObservedLength = if ($FieldId -eq "username") { $actualValue.Length } else { $null }
        }
    } catch {
        return [pscustomobject]@{ Matched = $false; ObservedLength = $null }
    }
}

function Invoke-ResolvedInputMutation {
    param(
        [string]$FieldId,
        [string]$Value,
        [scriptblock]$ResolveControl,
        [int]$ExpectedPort
    )

    $isPassword = $FieldId -eq "password"
    $record = [ordered]@{
        field_id = $FieldId
        control_type = $null
        value_pattern_supported = $null
        value_pattern_read_only = $null
        mutation_method = $null
        programmatic_set_attempted = $false
        programmatic_set_succeeded = $null
        programmatic_failure_code = $null
        programmatic_verification_succeeded = $null
        focus_requested = $false
        focus_confirmed = $false
        verification_performed = $false
        verification_succeeded = $null
        observed_value_length = $null
        failure_code = $null
    }
    if ($isPassword) { $record.Remove("observed_value_length") }

    $control = $null
    try { $control = & $ResolveControl } catch { }
    if (-not $control) {
        $record.failure_code = "desktop_input_unavailable_$FieldId"
        Add-InputMutationEvidence $record
        throw $record.failure_code
    }

    try { $record.control_type = [string]$control.Current.ControlType.ProgrammaticName } catch { }
    $isEdit = $record.control_type -eq "ControlType.Edit"
    if (-not $isPassword -and $isEdit) {
        $valuePattern = $null
        try {
            $valuePattern = $control.GetCurrentPattern(
                [System.Windows.Automation.ValuePattern]::Pattern
            )
            $record.value_pattern_supported = $true
            $record.value_pattern_read_only = [bool]$valuePattern.Current.IsReadOnly
        } catch {
            $record.value_pattern_supported = $false
        }

        if ($record.value_pattern_supported -and -not $record.value_pattern_read_only) {
            $record.mutation_method = "VALUE_PATTERN"
            $record.programmatic_set_attempted = $true
            $setSucceeded = $false
            try {
                $valuePattern.SetValue($Value)
                $record.programmatic_set_succeeded = $true
                $setSucceeded = $true
            } catch {
                $record.programmatic_set_succeeded = $false
                $record.programmatic_failure_code = `
                    "desktop_input_value_pattern_set_failed_$FieldId"
            }
            if ($setSucceeded) {
                $programmaticVerification = $null
                try {
                    $programmaticVerification = Test-ResolvedInputValue `
                        $ResolveControl $FieldId $Value $ExpectedPort
                } catch { }
                $record.verification_performed = $true
                $record.programmatic_verification_succeeded = [bool](
                    $programmaticVerification -and $programmaticVerification.Matched
                )
                $record.verification_succeeded = $record.programmatic_verification_succeeded
                if ($programmaticVerification) {
                    $record.observed_value_length = $programmaticVerification.ObservedLength
                }
                if ($record.programmatic_verification_succeeded) {
                    Add-InputMutationEvidence $record
                    return
                }
                $record.programmatic_failure_code = `
                    "desktop_input_value_not_populated_$FieldId"
            } else {
                $record.programmatic_verification_succeeded = $false
            }
        }
    }

    $record.mutation_method = "KEYBOARD"
    $record.focus_requested = $true
    try { $control.SetFocus() } catch { }
    $focusAcquired = $false
    try {
        Wait-Until {
            $focusedControl = & $ResolveControl
            if (-not $focusedControl) { return $false }
            return [bool]$focusedControl.Current.HasKeyboardFocus
        } 5 "desktop_input_focus_not_acquired_$FieldId"
        $focusAcquired = $true
    } catch { }
    $record.focus_confirmed = $focusAcquired
    if (-not $focusAcquired) {
        $record.failure_code = "desktop_input_focus_not_acquired_$FieldId"
        Add-InputMutationEvidence $record
        throw $record.failure_code
    }

    try {
        Send-InputKeyboardValue -Value $Value
    } catch {
        $record.failure_code = "desktop_input_keyboard_mutation_failed_$FieldId"
        Add-InputMutationEvidence $record
        throw $record.failure_code
    }

    if (-not $isPassword) {
        $verification = [pscustomobject]@{ Value = $null }
        try {
            Wait-Until {
                $verification.Value = Test-ResolvedInputValue `
                    $ResolveControl $FieldId $Value $ExpectedPort
                return [bool]$verification.Value.Matched
            } 5 "desktop_input_value_not_populated_$FieldId"
        } catch {
            $record.verification_performed = $true
            $record.verification_succeeded = $false
            $record.failure_code = "desktop_input_value_not_populated_$FieldId"
            if ($verification.Value) {
                $record.observed_value_length = $verification.Value.ObservedLength
            }
            Add-InputMutationEvidence $record
            throw $record.failure_code
        }
        $record.verification_performed = $true
        $record.verification_succeeded = $true
        $record.observed_value_length = $verification.Value.ObservedLength
    }

    Add-InputMutationEvidence $record
}

function Set-LoginInput {
    param(
        [int]$ProcessId,
        [string]$Name,
        [string]$Value,
        [string]$AutomationId,
        [System.Windows.Automation.ControlType[]]$AllowedControlTypes = @(
            [System.Windows.Automation.ControlType]::Edit
        )
    )

    $fieldId = [regex]::Replace($Name.Trim().ToLowerInvariant(), "[^a-z0-9]+", "_").Trim("_")
    if ([string]::IsNullOrWhiteSpace($fieldId)) { $fieldId = "unknown" }
    $inputUnavailableCode = "desktop_input_unavailable_$fieldId"
    $isNumericPort = $fieldId -eq "https_port"
    $expectedPort = 0
    if ($isNumericPort -and -not [int]::TryParse(
        $Value,
        [System.Globalization.NumberStyles]::Integer,
        [System.Globalization.CultureInfo]::InvariantCulture,
        [ref]$expectedPort
    )) {
        throw "desktop_input_expected_value_invalid_$fieldId"
    }

    $resolvedInput = [pscustomobject]@{ Control = $null }
    try {
        Wait-Until {
            $resolution = Resolve-InputControl `
                $ProcessId $Name $AutomationId $AllowedControlTypes $fieldId
            if (-not $resolution.Control) { return $false }
            $resolvedInput.Control = $resolution.Control
            return $true
        } 20 $inputUnavailableCode
    } catch {
        if ($_.Exception.Message -eq $inputUnavailableCode) {
            $failedResolution = Resolve-InputControl `
                $ProcessId $Name $AutomationId $AllowedControlTypes $fieldId
            $script:processEvidence["input_lookup_diagnostics"] = Get-InputLookupDiagnostics `
                $ProcessId $Name $AutomationId $AllowedControlTypes
            if ($failedResolution.Ambiguous) {
                throw "desktop_input_ambiguous_interactive_controls_$fieldId"
            }
        }
        throw
    }

    $resolveCurrentInput = {
        $resolution = Resolve-InputControl `
            $ProcessId $Name $AutomationId $AllowedControlTypes $fieldId
        return $resolution.Control
    }
    Invoke-ResolvedInputMutation `
        -FieldId $fieldId `
        -Value $Value `
        -ResolveControl $resolveCurrentInput `
        -ExpectedPort $expectedPort
}

function Get-ActiveOwnerSessionCount([object]$Config) {
    $username = $script:smokeOwnerUsername
    $count = Invoke-Psql $Config `
        "SELECT COUNT(*) FROM public.operator_sessions s JOIN public.operator_users u ON u.id = s.operator_user_id WHERE u.username = '$username' AND s.revoked_at IS NULL AND s.expires_at > now();"
    if ($count -notmatch '^\d+$') { throw "controller_operator_session_count_invalid" }
    return [int]$count
}

function Get-OperatorLoginUiState([int]$ProcessId) {
    $window = Get-Window $ProcessId
    $username = if ($window) {
        Find-Element $window "Username" ([System.Windows.Automation.ControlType]::Edit)
    } else { $null }
    $password = if ($window) {
        Find-Element $window "Password" ([System.Windows.Automation.ControlType]::Edit)
    } else { $null }
    $signIn = if ($window) {
        Find-Element $window "Sign in" ([System.Windows.Automation.ControlType]::Button)
    } else { $null }
    $usernameCurrent = if ($username) { $username.Current } else { $null }
    $passwordCurrent = if ($password) { $password.Current } else { $null }
    $signInCurrent = if ($signIn) { $signIn.Current } else { $null }
    $invokePattern = $null
    $invokeAvailable = $false
    if ($signIn) {
        try {
            $invokeAvailable = $signIn.TryGetCurrentPattern(
                [System.Windows.Automation.InvokePattern]::Pattern,
                [ref]$invokePattern
            )
        } catch { $invokeAvailable = $false }
    }
    return [pscustomobject]@{
        Username = $username
        Password = $password
        SignIn = $signIn
        UsernamePresent = $null -ne $username
        UsernameEnabled = $null -ne $usernameCurrent -and $usernameCurrent.IsEnabled
        UsernameFocusable = $null -ne $usernameCurrent -and $usernameCurrent.IsKeyboardFocusable
        PasswordPresent = $null -ne $password
        PasswordEnabled = $null -ne $passwordCurrent -and $passwordCurrent.IsEnabled
        PasswordFocusable = $null -ne $passwordCurrent -and $passwordCurrent.IsKeyboardFocusable
        SignInPresent = $null -ne $signIn
        SignInEnabled = $null -ne $signInCurrent -and $signInCurrent.IsEnabled
        SignInInvokePatternAvailable = [bool]$invokeAvailable
        Ready = $null -ne $username -and $null -ne $password -and $null -ne $signIn -and
            $null -ne $usernameCurrent -and $usernameCurrent.IsEnabled -and
            $usernameCurrent.IsKeyboardFocusable -and
            $null -ne $passwordCurrent -and $passwordCurrent.IsEnabled -and
            $passwordCurrent.IsKeyboardFocusable -and
            $null -ne $signInCurrent -and $signInCurrent.IsEnabled -and $invokeAvailable
    }
}

function Get-OperatorLoginDatabaseState([object]$Config) {
    $username = $script:smokeOwnerUsername
    $ownerRow = Invoke-Psql $Config `
        "SELECT enabled::text || '|' || role || '|' || must_change_password::text FROM public.operator_users WHERE username = '$username';"
    $ownerFields = if ([string]::IsNullOrWhiteSpace($ownerRow)) { @() } else { $ownerRow.Split('|') }
    $throttleRow = Invoke-Psql $Config `
        "SELECT (t.operator_user_id IS NOT NULL)::text || '|' || COALESCE(t.failed_attempts, 0)::text || '|' || COALESCE((t.locked_until > now())::text, 'false') FROM public.operator_users u LEFT JOIN public.operator_login_throttles t ON t.operator_user_id = u.id WHERE u.username = '$username';"
    $throttleFields = $throttleRow.Split('|')
    $activeSessions = Get-ActiveOwnerSessionCount $Config
    $eventRows = Invoke-Psql $Config `
        "SELECT COALESCE(string_agg(event_type, ',' ORDER BY created_at DESC), '') FROM (SELECT event_type, created_at FROM public.workspace_audit_events WHERE actor_username = '$username' AND event_type LIKE 'operator.login_%' ORDER BY created_at DESC LIMIT 8) recent;"
    $loginSuccessAuditCount = Invoke-Psql $Config `
        "SELECT COUNT(*) FROM public.workspace_audit_events WHERE actor_username = '$username' AND event_type = 'operator.login_succeeded';"
    if ($loginSuccessAuditCount -notmatch '^\d+$') {
        throw "controller_operator_login_audit_count_invalid"
    }
    return [ordered]@{
        active_owner_session_count = $activeSessions
        owner_exists = $ownerFields.Count -eq 3
        owner_enabled = $ownerFields.Count -eq 3 -and $ownerFields[0] -eq "true"
        owner_role = if ($ownerFields.Count -eq 3) { $ownerFields[1] } else { $null }
        owner_must_change_password = $ownerFields.Count -eq 3 -and $ownerFields[2] -eq "true"
        login_throttle_present = $throttleFields.Count -eq 3 -and $throttleFields[0] -eq "true"
        login_failed_attempts = if ($throttleFields.Count -eq 3) { [int]$throttleFields[1] } else { 0 }
        login_lock_active = $throttleFields.Count -eq 3 -and $throttleFields[2] -eq "true"
        login_success_audit_count = [int]$loginSuccessAuditCount
        recent_owner_audit_event_types = if ([string]::IsNullOrWhiteSpace($eventRows)) {
            @()
        } else { @($eventRows.Split(',') | Where-Object { $_ -match '^operator\.login_[a-z_]+$' }) }
    }
}

function Get-PostCrashOperatorAuthState([object]$Config) {
    $username = $script:smokeOwnerUsername
    $activeSessions = Get-ActiveOwnerSessionCount $Config
    $loginSuccessAuditCount = Invoke-Psql $Config `
        "SELECT COUNT(*) FROM public.workspace_audit_events WHERE actor_username = '$username' AND event_type = 'operator.login_succeeded';"
    $loginAuditEventCount = Invoke-Psql $Config `
        "SELECT COUNT(*) FROM public.workspace_audit_events WHERE actor_username = '$username' AND event_type LIKE 'operator.login_%';"
    if ($loginSuccessAuditCount -notmatch '^\d+$' -or $loginAuditEventCount -notmatch '^\d+$') {
        throw "controller_post_crash_login_audit_count_invalid"
    }
    return [pscustomobject]@{
        server_active_session_count = [int]$activeSessions
        login_success_audit_count = [int]$loginSuccessAuditCount
        login_audit_event_count = [int]$loginAuditEventCount
        operator_login_attempt_timeline_count = [int]$script:operatorLoginAttemptTimeline.Count
    }
}

function Assert-PostCrashOperatorReauthenticationRequired(
    [int]$ProcessId,
    [object]$Config,
    [string]$Stage,
    [object]$BeforeCrash
) {
    $uiStateHolder = [pscustomobject]@{ Value = $null }
    try {
        Wait-Until {
            $uiStateHolder.Value = Get-OperatorLoginUiState $ProcessId
            return [bool]$uiStateHolder.Value.Ready
        } 15 "controller_post_crash_login_ui_not_actionable"
    } catch { }

    $uiState = $uiStateHolder.Value
    if (-not $uiState) {
        $uiState = [pscustomobject]@{
            Ready = $false
            UsernamePresent = $false
            UsernameEnabled = $false
            UsernameFocusable = $false
            PasswordPresent = $false
            PasswordEnabled = $false
            PasswordFocusable = $false
            SignInPresent = $false
            SignInEnabled = $false
            SignInInvokePatternAvailable = $false
        }
    }
    $afterRelaunch = Get-PostCrashOperatorAuthState $Config
    $automaticLoginAuditDelta =
        [int]$afterRelaunch.login_audit_event_count - [int]$BeforeCrash.login_audit_event_count
    $loginSuccessAuditDelta =
        [int]$afterRelaunch.login_success_audit_count - [int]$BeforeCrash.login_success_audit_count
    $serverSessionCountDelta =
        [int]$afterRelaunch.server_active_session_count - [int]$BeforeCrash.server_active_session_count
    $loginAttemptTimelineDelta = [int]$afterRelaunch.operator_login_attempt_timeline_count -
        [int]$BeforeCrash.operator_login_attempt_timeline_count
    $failureCode = if (-not [bool]$uiState.Ready) {
        "controller_post_crash_login_ui_not_actionable"
    } elseif ($automaticLoginAuditDelta -ne 0 -or $loginSuccessAuditDelta -ne 0 -or
        $loginAttemptTimelineDelta -ne 0) {
        "controller_post_crash_automatic_login_detected"
    } elseif ($serverSessionCountDelta -ne 0) {
        "controller_post_crash_server_session_count_changed"
    } else { $null }
    $evidence = [ordered]@{
        stage = $Stage
        recorded_utc = [DateTimeOffset]::UtcNow.ToString("O")
        client_reauthentication_required = [bool]$uiState.Ready
        login_ui_ready = [bool]$uiState.Ready
        username_control_present = [bool]$uiState.UsernamePresent
        username_control_enabled = [bool]$uiState.UsernameEnabled
        username_control_keyboard_focusable = [bool]$uiState.UsernameFocusable
        password_control_present = [bool]$uiState.PasswordPresent
        password_control_enabled = [bool]$uiState.PasswordEnabled
        password_control_keyboard_focusable = [bool]$uiState.PasswordFocusable
        sign_in_present = [bool]$uiState.SignInPresent
        sign_in_enabled = [bool]$uiState.SignInEnabled
        sign_in_invoke_pattern_available = [bool]$uiState.SignInInvokePatternAvailable
        server_active_session_count_before_crash = [int]$BeforeCrash.server_active_session_count
        server_active_session_count_after_relaunch = [int]$afterRelaunch.server_active_session_count
        login_success_audit_count_before_crash = [int]$BeforeCrash.login_success_audit_count
        login_success_audit_count_after_relaunch = [int]$afterRelaunch.login_success_audit_count
        automatic_login_audit_delta = $automaticLoginAuditDelta
        login_success_audit_delta = $loginSuccessAuditDelta
        server_session_count_delta = $serverSessionCountDelta
        operator_login_attempt_timeline_delta = $loginAttemptTimelineDelta
        server_session_rows_are_local_bearer_evidence = $false
        outcome = if ($failureCode) { "BLOCKER" } else { "PASS" }
        failure_code = $failureCode
    }
    $script:scenarioEvidence[$Stage] = $evidence
    if ($failureCode) { throw $failureCode }
    return $evidence
}

function Get-OperatorLoginUiErrorCategory([int]$ProcessId) {
    try {
        $window = Get-Window $ProcessId
        if (-not $window) { return "NONE" }
        if (Find-TextContaining $window "The Controller could not verify Operator access.") {
            return "OPERATOR_API_UNAVAILABLE"
        }
        if (Find-TextContaining $window "Sign-in failed.") { return "SIGN_IN_FAILED" }
        if (Find-TextContaining $window "First Owner setup failed.") { return "FIRST_OWNER_SETUP_FAILED" }
    } catch { return "NONE" }
    return "NONE"
}

function Get-OperatorLoginSnapshot(
    [int]$ProcessId,
    [object]$Config,
    [object]$UiState
) {
    $runtime = Get-ControllerProcesses
    $supervisorState = "UNKNOWN"
    try {
        $window = Get-Window $ProcessId
        if ($window -and (Find-TextContaining $window "Controller runtime is running")) {
            $supervisorState = "RUNNING"
        } elseif ($window -and (Find-TextContaining $window "Controller runtime failed")) {
            $supervisorState = "FAILED"
        }
    } catch { }
    $healthOutcome = "UNAVAILABLE"
    $readyOutcome = "UNAVAILABLE"
    try { $healthOutcome = [string](Invoke-ControllerHttpsProbe $Config "/health").outcome } catch { }
    try { $readyOutcome = [string](Invoke-ControllerHttpsProbe $Config "/ready").outcome } catch { }
    $database = Get-OperatorLoginDatabaseState $Config
    return [ordered]@{
        recorded_utc = [DateTimeOffset]::UtcNow.ToString("O")
        supervisor_state = $supervisorState
        supervisor_diagnostic_code = Get-ControllerDiagnosticCode $ProcessId
        endpoint_port = [int]$Config.endpointPort
        health_probe_outcome = $healthOutcome
        ready_probe_outcome = $readyOutcome
        username_control_present = [bool]$UiState.UsernamePresent
        username_control_enabled = [bool]$UiState.UsernameEnabled
        username_control_keyboard_focusable = [bool]$UiState.UsernameFocusable
        password_control_present = [bool]$UiState.PasswordPresent
        password_control_enabled = [bool]$UiState.PasswordEnabled
        password_control_keyboard_focusable = [bool]$UiState.PasswordFocusable
        sign_in_present = [bool]$UiState.SignInPresent
        sign_in_enabled = [bool]$UiState.SignInEnabled
        sign_in_invoke_pattern_available = [bool]$UiState.SignInInvokePatternAvailable
        active_owner_session_count = [int]$database.active_owner_session_count
        owner_exists = [bool]$database.owner_exists
        owner_enabled = [bool]$database.owner_enabled
        owner_role = $database.owner_role
        owner_must_change_password = [bool]$database.owner_must_change_password
        login_throttle_present = [bool]$database.login_throttle_present
        login_failed_attempts = [int]$database.login_failed_attempts
        login_lock_active = [bool]$database.login_lock_active
        login_success_audit_count = [int]$database.login_success_audit_count
        recent_owner_audit_event_types = @($database.recent_owner_audit_event_types)
        postgres_count = @($runtime.postgres).Count
        http_count = @($runtime.http).Count
        scheduler_count = @($runtime.scheduler).Count
    }
}

function Invoke-ObservedOperatorLogin([int]$ProcessId, [object]$Config, [string]$Stage) {
    $uiStateHolder = [pscustomobject]@{ Value = $null }
    try {
        Wait-Until {
            $uiStateHolder.Value = Get-OperatorLoginUiState $ProcessId
            return [bool]$uiStateHolder.Value.Ready
        } 15 "controller_operator_login_ui_not_ready"
    } catch { }
    $uiState = if ($uiStateHolder.Value) { $uiStateHolder.Value } else {
        Get-OperatorLoginUiState $ProcessId
    }

    $uiReady = [bool]$uiState.Ready
    $inputMutationSucceeded = $false
    if ($uiReady) {
        try {
            Set-LoginInput $ProcessId "Username" $script:smokeOwnerUsername
            Set-LoginInput $ProcessId "Password" $script:smokeOwnerPassword
            $inputMutationSucceeded = $true
        } catch { $inputMutationSucceeded = $false }
    }
    $uiState = Get-OperatorLoginUiState $ProcessId
    $beforeState = Get-OperatorLoginSnapshot $ProcessId $Config $uiState
    $beforeState.stage = $Stage
    $beforeState.active_owner_session_count_before = [int]$beforeState.active_owner_session_count
    $beforeState.login_throttle_present_before = [bool]$beforeState.login_throttle_present
    $beforeState.login_failed_attempts_before = [int]$beforeState.login_failed_attempts
    $beforeState.login_lock_active_before = [bool]$beforeState.login_lock_active
    $beforeState.login_success_audit_count_before = [int]$beforeState.login_success_audit_count
    $before = [ordered]@{
        active_owner_session_count_before = [int]$beforeState.active_owner_session_count
        login_failed_attempts = [int]$beforeState.login_failed_attempts
        login_lock_active = [bool]$beforeState.login_lock_active
        login_success_audit_count_before = [int]$beforeState.login_success_audit_count
        health_probe_outcome_before = [string]$beforeState.health_probe_outcome
        ready_probe_outcome_before = [string]$beforeState.ready_probe_outcome
    }
    $invokeCompleted = $false
    if ($uiReady -and $inputMutationSucceeded) {
        try {
            $invokePattern = $uiState.SignIn.GetCurrentPattern(
                [System.Windows.Automation.InvokePattern]::Pattern
            )
            $invokePattern.Invoke()
            $invokeCompleted = $true
        } catch { $invokeCompleted = $false }
    }

    $uiErrorCategory = "NONE"
    $afterState = Get-OperatorLoginSnapshot $ProcessId $Config `
        (Get-OperatorLoginUiState $ProcessId)
    $after = [ordered]@{
        active_owner_session_count_after = [int]$afterState.active_owner_session_count
        login_failed_attempts = [int]$afterState.login_failed_attempts
        login_lock_active = [bool]$afterState.login_lock_active
        login_success_audit_count_after = [int]$afterState.login_success_audit_count
        recent_owner_audit_event_types_after = @($afterState.recent_owner_audit_event_types)
        health_probe_outcome = [string]$afterState.health_probe_outcome
        ready_probe_outcome = [string]$afterState.ready_probe_outcome
    }
    $afterState.stage = $Stage
    $afterState.active_owner_session_count_after = [int]$afterState.active_owner_session_count
    $afterState.login_throttle_present_after = [bool]$afterState.login_throttle_present
    $afterState.login_failed_attempts_after = [int]$afterState.login_failed_attempts
    $afterState.login_lock_active_after = [bool]$afterState.login_lock_active
    $outcome = Get-OperatorLoginOutcome `
        $before $after $uiReady $inputMutationSucceeded $invokeCompleted $uiErrorCategory
    $deadline = [DateTime]::UtcNow.AddSeconds(30)
    while ($outcome -eq "LOGIN_FAILURE_UNCLASSIFIED" -and [DateTime]::UtcNow -lt $deadline) {
        Start-Sleep -Milliseconds 500
        $uiErrorCategory = Get-OperatorLoginUiErrorCategory $ProcessId
        $afterState = Get-OperatorLoginSnapshot $ProcessId $Config `
            (Get-OperatorLoginUiState $ProcessId)
        $after = [ordered]@{
            active_owner_session_count_after = [int]$afterState.active_owner_session_count
            login_failed_attempts = [int]$afterState.login_failed_attempts
            login_lock_active = [bool]$afterState.login_lock_active
            login_success_audit_count_after = [int]$afterState.login_success_audit_count
            recent_owner_audit_event_types_after = @($afterState.recent_owner_audit_event_types)
            health_probe_outcome = [string]$afterState.health_probe_outcome
            ready_probe_outcome = [string]$afterState.ready_probe_outcome
        }
        $afterState.stage = $Stage
        $afterState.active_owner_session_count_after = [int]$afterState.active_owner_session_count
        $afterState.login_throttle_present_after = [bool]$afterState.login_throttle_present
        $afterState.login_failed_attempts_after = [int]$afterState.login_failed_attempts
        $afterState.login_lock_active_after = [bool]$afterState.login_lock_active
        $outcome = Get-OperatorLoginOutcome `
            $before $after $uiReady $inputMutationSucceeded $invokeCompleted $uiErrorCategory
    }
    $failure = if ($outcome -eq "SUCCESS") { $null } else { Get-OperatorLoginFailureCode $outcome }
    $script:operatorLoginAttemptTimeline.Add([ordered]@{
        stage = $Stage
        recorded_utc = [DateTimeOffset]::UtcNow.ToString("O")
        pre_login = $beforeState
        login_ui_ready = $uiReady
        input_mutation_completed = $inputMutationSucceeded
        sign_in_invoke_completed = $invokeCompleted
        ui_error_category = $uiErrorCategory
        post_login = $afterState
        outcome = $outcome
        failure_code = $failure
    }) | Out-Null
    if ($outcome -ne "SUCCESS") { throw $failure }
}

function Add-OperatorSessionTransition(
    [string]$Stage,
    [int]$ExpectedCount,
    [Nullable[int]]$ObservedCount,
    [string]$QueryOutcome,
    [string]$ExceptionType,
    [string]$Outcome,
    [string]$FailureCode
) {
    $script:operatorSessionTimeline.Add([ordered]@{
        stage = $Stage
        recorded_utc = [DateTimeOffset]::UtcNow.ToString("O")
        expected_active_owner_session_count = $ExpectedCount
        observed_active_owner_session_count = $ObservedCount
        query_outcome = $QueryOutcome
        query_exception_type = $ExceptionType
        outcome = $Outcome
        failure_code = $FailureCode
    }) | Out-Null
}

function Wait-ForOperatorSessionCount(
    [object]$Config,
    [string]$Stage,
    [int]$ExpectedCount,
    [int]$TimeoutSeconds,
    [string]$FailureCode
) {
    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    $observedCount = $null
    $queryOutcome = "NOT_RUN"
    $exceptionType = $null
    do {
        try {
            $observedCount = Get-ActiveOwnerSessionCount $Config
            $queryOutcome = "PASS"
            $exceptionType = $null
        } catch {
            $observedCount = $null
            $queryOutcome = "FAIL"
            $exceptionType = Get-SafeExceptionTypeName $_.Exception
        }

        if ($queryOutcome -eq "PASS" -and $observedCount -eq $ExpectedCount) {
            Add-OperatorSessionTransition `
                $Stage $ExpectedCount $observedCount $queryOutcome $exceptionType "PASS" $null
            return [int]$observedCount
        }
        if ([DateTime]::UtcNow -ge $deadline) { break }
        Start-Sleep -Milliseconds 200
    } while ($true)

    Add-OperatorSessionTransition `
        $Stage $ExpectedCount $observedCount $queryOutcome $exceptionType "FAIL" $FailureCode
    throw $FailureCode
}

function Assert-OneActiveOwnerSession([object]$Config) {
    $activeSessions = Wait-ForOperatorSessionCount `
        $Config "before_privileged_quit" 1 3 "controller_active_owner_session_count_not_one"
    $processEvidence.operator_session_preflight = [ordered]@{
        active_sessions = [string]$activeSessions
    }
    $checks.active_session_verified_before_privileged_quit = $true
}

function Test-FirstOwnerSetupFailed([int]$ProcessId) {
    return $null -ne (Find-TextContaining (Get-Window $ProcessId) "First Owner setup failed")
}

function Bootstrap-ControllerOwner([int]$ProcessId) {
    Invoke-Button $ProcessId "Set up first Owner"
    Set-LoginInput $ProcessId "Username" $script:smokeOwnerUsername
    Set-LoginInput $ProcessId "Password" $script:smokeOwnerPassword
    Invoke-Button $ProcessId "Create first Owner"
    Wait-Until {
        $config = Get-ControllerConfig
        $ownerCount = Invoke-Psql $config `
            "SELECT COUNT(*) FROM public.operator_users WHERE username = '$script:smokeOwnerUsername' AND role = 'OWNER' AND enabled;"
        ($ownerCount -eq "1" -and (Get-ActiveOwnerSessionCount $config) -eq 1) -or
            (Test-FirstOwnerSetupFailed $ProcessId)
    } 30 "controller_first_owner_setup_no_response"
    if (Test-FirstOwnerSetupFailed $ProcessId) {
        $ownerCount = Invoke-Psql (Get-ControllerConfig) `
            "SELECT COUNT(*) FROM public.operator_users WHERE role = 'OWNER' AND enabled;"
        throw "controller_first_owner_setup_rejected_enabled_owners_$ownerCount"
    }
}

function Ensure-ControllerOwner(
    [int]$ProcessId,
    [string]$LoginStage,
    [switch]$ForceReauthentication,
    [switch]$AfterEndpointReconfiguration
) {
    $config = Get-ControllerConfig
    if ($ForceReauthentication) {
        $revocationStage = if ($AfterEndpointReconfiguration) {
            "cutover_revoke"
        } else {
            "reopen_before_login"
        }
        $revocationFailure = if ($AfterEndpointReconfiguration) {
            "controller_endpoint_reconfigure_session_not_revoked"
        } else {
            "controller_owner_session_not_revoked_on_lock"
        }
        $null = Wait-ForOperatorSessionCount `
            $config $revocationStage 0 15 $revocationFailure
        if ($AfterEndpointReconfiguration) {
            $checks.endpoint_running_transition_revokes_owner_session = $true
        }
    } else {
        $activeSessions = Get-ActiveOwnerSessionCount $config
        if ($activeSessions -eq 1) { return }
        if ($activeSessions -ne 0) {
            throw "controller_active_owner_session_count_not_one"
        }
    }
    Invoke-ObservedOperatorLogin $ProcessId $config $LoginStage
    $null = Wait-ForOperatorSessionCount `
        $config $LoginStage 1 3 "controller_owner_login_session_not_observed"
    if ($AfterEndpointReconfiguration) {
        $checks.endpoint_running_transition_reauthenticates_one_owner_session = $true
    }
}

function Set-OperatorSessionMode(
    [int]$ProcessId,
    [object]$Config,
    [ValidateSet("signed_in", "signed_out")]
    [string]$Mode,
    [string]$Stage,
    [string]$FailureCode
) {
    if ($Mode -eq "signed_in") {
        Ensure-ControllerOwner $ProcessId $Stage
        return Wait-ForOperatorSessionCount `
            $Config $Stage 1 3 $FailureCode
    }
    $observed = Get-ActiveOwnerSessionCount $Config
    if ($observed -eq 1) {
        $window = Get-ProcessIfPresent $ProcessId
        if (-not $window) { throw "controller_scenario_signed_out_window_unavailable" }
        $window.Refresh()
        if (-not [ThreadsControllerSmoke.NativeMethods]::PostMessage(
            $window.MainWindowHandle, 0x0010, [IntPtr]::Zero, [IntPtr]::Zero
        )) { throw "controller_scenario_signed_out_lock_failed" }
        Wait-Until {
            -not [ThreadsControllerSmoke.NativeMethods]::IsWindowVisible($window.MainWindowHandle)
        } 10 "controller_scenario_signed_out_lock_timeout"
        $window.Dispose()
    } elseif ($observed -ne 0) {
        throw "controller_scenario_owner_session_count_invalid"
    }
    return Wait-ForOperatorSessionCount `
        $Config $Stage 0 3 $FailureCode
}

function Start-Desktop([int]$WindowTimeout = 30) {
    $process = Start-Process -FilePath $DesktopExecutable -PassThru
    $desktopPids.Add([int]$process.Id)
    $script:primaryId = [int]$process.Id
    Wait-Until {
        $candidate = Get-Process -Id $script:primaryId -ErrorAction SilentlyContinue
        if (-not $candidate) { return $false }
        $candidate.Refresh()
        return $candidate.MainWindowHandle -ne [IntPtr]::Zero -and
            [ThreadsControllerSmoke.NativeMethods]::IsWindowVisible($candidate.MainWindowHandle)
    } $WindowTimeout "desktop_window_unavailable"
    return $process
}

function Start-ExistingController {
    $process = Start-Desktop 260
    Wait-Until {
        $text = Find-Element (Get-Window $script:primaryId) "Your Controller" `
            ([System.Windows.Automation.ControlType]::Text)
        return $null -ne $text
    } 40 "controller_window_not_ready"
    return $process
}

function Wait-ControllerState([int]$ProcessId, [string]$State, [string]$Diagnostic, [int]$Timeout = 60) {
    Wait-Until {
        $window = Get-Window $ProcessId
        if (-not $window) { return $false }
        if ($Diagnostic) {
            $diagnosticElement = Find-TextContaining $window "Diagnostic code:"
            if (-not $diagnosticElement) { return $false }
            $actualDiagnostic = ([string]$diagnosticElement.Current.Name) -replace `
                '^.*Diagnostic code:\s*', ''
            if (-not [string]::IsNullOrWhiteSpace($actualDiagnostic) -and
                $actualDiagnostic -cne $Diagnostic) {
                throw "controller_unexpected_diagnostic_$actualDiagnostic"
            }
            return $actualDiagnostic -ceq $Diagnostic
        }
        if ($State -eq "Controller runtime is running") {
            $failed = Find-TextContaining $window "Controller runtime failed"
            if ($failed) {
                $diagnosticElement = Find-TextContaining $window "Diagnostic code:"
                if ($diagnosticElement) {
                    $diagnosticCode = ([string]$diagnosticElement.Current.Name) -replace `
                        '^.*Diagnostic code:\s*', ''
                    if (-not [string]::IsNullOrWhiteSpace($diagnosticCode)) {
                        throw "controller_runtime_start_failed_$diagnosticCode"
                    }
                }
                throw "controller_runtime_start_failed"
            }
        }
        return $null -ne (Find-TextContaining $window $State)
    } $Timeout "controller_state_unavailable_$(if ($Diagnostic) { $Diagnostic } else { $State })"
}

function Get-ControllerDiagnosticCode([int]$ProcessId) {
    try {
        $window = Get-Window $ProcessId
        if (-not $window) { return $null }
        $diagnosticElement = Find-TextContaining $window "Diagnostic code:"
        if (-not $diagnosticElement) { return $null }
        $diagnosticCode = ([string]$diagnosticElement.Current.Name) -replace `
            '^.*Diagnostic code:\s*', ''
        if ([string]::IsNullOrWhiteSpace($diagnosticCode)) { return $null }
        return [regex]::Replace($diagnosticCode, "[^A-Za-z0-9_.-]", "_")
    } catch {
        return $null
    }
}

function Get-EndpointCollisionFailureCode([object]$Evidence) {
    if ([int]$Evidence.observed_persisted_endpoint_port -ne
        [int]$Evidence.expected_endpoint_port) {
        return "controller_endpoint_collision_changed_persisted_endpoint"
    }
    if ([int]$Evidence.postgres_count -ne 0 -or
        [int]$Evidence.http_count -ne 0 -or
        [int]$Evidence.scheduler_count -ne 0) {
        return "controller_endpoint_collision_left_runtime_processes"
    }
    if (-not [bool]$Evidence.diagnostic_wait_completed -or
        ($Evidence.observed_diagnostic_code -and
            [string]$Evidence.observed_diagnostic_code -cne
                [string]$Evidence.expected_diagnostic_code)) {
        return "controller_endpoint_collision_diagnostic_not_confirmed"
    }
    return $null
}

function Get-ControllerConfig {
    if (-not (Test-Path -LiteralPath (Join-Path $controllerRoot "controller.json") -PathType Leaf)) {
        throw "controller_config_missing"
    }
    return Get-Content -LiteralPath (Join-Path $controllerRoot "controller.json") -Raw | ConvertFrom-Json
}

function Get-DatabaseCredential {
    $path = Join-Path $controllerRoot "database-credential.dpapi"
    $encrypted = [System.IO.File]::ReadAllBytes($path)
    $plain = [System.Security.Cryptography.ProtectedData]::Unprotect(
        $encrypted,
        $null,
        [System.Security.Cryptography.DataProtectionScope]::CurrentUser
    )
    if ($plain.Length -ne 64) { throw "controller_dpapi_payload_invalid" }
    return $plain
}

function Invoke-Psql([object]$Config, [string]$Sql) {
    $plain = Get-DatabaseCredential
    $oldPassword = $env:PGPASSWORD
    try {
        $env:PGPASSWORD = [System.Text.Encoding]::UTF8.GetString($plain)
        $output = @(& $psql -X -w -h 127.0.0.1 -p $Config.databasePort `
            -U threads_platform -d postgres -A -t -v ON_ERROR_STOP=1 -c $Sql 2>$null
        )
        if ($LASTEXITCODE -ne 0) { throw "controller_database_query_failed" }
        return ([string]::Join("", [string[]]$output)).Trim()
    } finally {
        [Array]::Clear($plain, 0, $plain.Length)
        $env:PGPASSWORD = $oldPassword
    }
}

function Get-PostgresSystemIdentifierFromControlFile {
    $dataDirectory = Join-Path $controllerRoot "postgresql"
    if (-not (Test-Path -LiteralPath $pgControlData -PathType Leaf)) {
        throw "controller_postgres_control_data_tool_missing"
    }
    $lines = @(& $pgControlData $dataDirectory 2>$null)
    if ($LASTEXITCODE -ne 0) { throw "controller_postgres_control_data_read_failed" }
    $match = @($lines | Where-Object { $_ -match '^Database system identifier:\s*(\d+)$' })
    if ($match.Count -ne 1) { throw "controller_postgres_system_identifier_unavailable" }
    return [regex]::Match([string]$match[0], '\d+').Value
}

function Get-OperatorSessionEvidence([object]$Config) {
    try {
        $username = $script:smokeOwnerUsername
        $activeSessions = Invoke-Psql $Config `
            "SELECT COUNT(*) FROM public.operator_sessions s JOIN public.operator_users u ON u.id = s.operator_user_id WHERE u.username = '$username' AND s.revoked_at IS NULL AND s.expires_at > now();"
        $recentEvents = Invoke-Psql $Config `
            "SELECT COALESCE(string_agg(event_type, ',' ORDER BY created_at DESC), '') FROM (SELECT event_type, created_at FROM public.workspace_audit_events WHERE actor_username = '$username' ORDER BY created_at DESC LIMIT 8) recent;"
        return [ordered]@{
            active_sessions = $activeSessions
            recent_auth_events = $recentEvents
        }
    } catch {
        return [ordered]@{
            active_sessions = "unavailable"
            recent_auth_events = "unavailable"
        }
    }
}

function Get-ListenerAddresses([int]$Port) {
    @(Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue |
        Select-Object -ExpandProperty LocalAddress -Unique)
}

function Assert-ControllerProcesses {
    $owned = Get-ControllerProcesses
    if ($owned.postgres.Count -ne 1 -or $owned.http.Count -ne 1 -or $owned.scheduler.Count -ne 1) {
        throw "controller_process_ownership_count_invalid"
    }
    $parentIds = @(
        $owned.postgres[0].ParentProcessId,
        $owned.http[0].ParentProcessId,
        $owned.scheduler[0].ParentProcessId
    ) | Select-Object -Unique
    if ($parentIds.Count -ne 1 -or [int]$parentIds[0] -ne $script:primaryId) {
        throw "controller_process_parent_invalid"
    }
    return $owned
}

function Add-ControllerRuntimeIdentitySnapshot(
    [string]$Stage,
    [object]$Owned,
    [object]$Config
) {
    $snapshot = [pscustomobject][ordered]@{
        stage = $Stage
        postgres_pid = [int]$Owned.postgres[0].ProcessId
        http_pid = [int]$Owned.http[0].ProcessId
        scheduler_pid = [int]$Owned.scheduler[0].ProcessId
        controller_id = [string]$Config.controllerId
        lan_address = [string]$Config.lanAddress
        endpoint_port = [int]$Config.endpointPort
    }
    $script:controllerRuntimeIdentityTimeline.Add($snapshot)
    return $snapshot
}

function Assert-ControllerRuntimeIdentityCutover([object]$Initial, [object]$Cutover) {
    if ([int]$Cutover.postgres_pid -ne [int]$Initial.postgres_pid -or
        [int]$Cutover.http_pid -eq [int]$Initial.http_pid -or
        [int]$Cutover.scheduler_pid -eq [int]$Initial.scheduler_pid -or
        [string]$Cutover.controller_id -cne [string]$Initial.controller_id -or
        ([string]$Cutover.lan_address -ceq [string]$Initial.lan_address -and
            [int]$Cutover.endpoint_port -eq [int]$Initial.endpoint_port)) {
        throw "controller_running_endpoint_transition_invalid"
    }
}

function Assert-ControllerRuntimeIdentityContinuity(
    [object]$Expected,
    [object]$Observed,
    [string]$FailureCode
) {
    if ([int]$Observed.postgres_pid -ne [int]$Expected.postgres_pid -or
        [int]$Observed.http_pid -ne [int]$Expected.http_pid -or
        [int]$Observed.scheduler_pid -ne [int]$Expected.scheduler_pid -or
        [string]$Observed.controller_id -cne [string]$Expected.controller_id -or
        [string]$Observed.lan_address -cne [string]$Expected.lan_address -or
        [int]$Observed.endpoint_port -ne [int]$Expected.endpoint_port) {
        throw $FailureCode
    }
}

function Wait-ForDesktopParentExit([int]$ProcessId) {
    Wait-Until {
        $desktopProcess = Get-ProcessIfPresent $ProcessId
        if (-not $desktopProcess) { return $true }
        $desktopProcess.Dispose()
        return $false
    } 55 "desktop_graceful_quit_timeout"
}

function Quit-Desktop([int]$ProcessId, [string]$LoginStage) {
    $owned = Get-ControllerProcesses
    if ($owned.postgres.Count -eq 1 -and $owned.http.Count -eq 1 -and $owned.scheduler.Count -eq 1) {
        Ensure-ControllerOwner $ProcessId $LoginStage
        Invoke-Button $ProcessId "Quit…"
        Invoke-Button $ProcessId "Stop node and quit" -BeforeInvoke {
            Assert-OneActiveOwnerSession (Get-ControllerConfig)
        }
        try {
            Wait-ForDesktopParentExit $ProcessId
        } catch {
            $primaryFailure = $_
            $remaining = $null
            $processDiagnosticExceptionType = $null
            try {
                $remaining = Get-ControllerProcesses
            } catch {
                $processDiagnosticExceptionType = Get-SafeExceptionTypeName $_.Exception
            }
            $operatorSessionState = $null
            $sessionDiagnosticExceptionType = $null
            try {
                $operatorSessionState = Get-OperatorSessionEvidence (Get-ControllerConfig)
            } catch {
                $sessionDiagnosticExceptionType = Get-SafeExceptionTypeName $_.Exception
            }
            $uiDiagnostics = [ordered]@{
                ui_diagnostics_available = $false
                ui_diagnostics_exception_type = $null
                diagnostic_code = $null
                runtime_failed_visible = $null
                operator_auth_error = $null
                operator_session_revoked = $null
                shutdown_error = $null
            }
            try {
                $window = Get-Window $ProcessId
                if ($window) {
                    $diagnosticName = Get-ElementName (Find-TextContaining $window "Diagnostic code:")
                    $uiDiagnostics.diagnostic_code = if ($diagnosticName) {
                        $diagnosticName -replace '^.*Diagnostic code:\s*', ''
                    } else { $null }
                    $uiDiagnostics.runtime_failed_visible =
                        $null -ne (Find-TextContaining $window "Controller runtime failed")
                    $uiDiagnostics.operator_auth_error = $null -ne (Find-TextContaining `
                        $window "An active Operator session with permission to stop this node is required"
                    )
                    $uiDiagnostics.operator_session_revoked = $null -ne (Find-TextContaining `
                        $window "Operator access changed. Sign in again"
                    )
                    $uiDiagnostics.shutdown_error = $null -ne (
                        Find-TextContaining $window "The node could not stop cleanly"
                    )
                    $uiDiagnostics.ui_diagnostics_available = $true
                }
            } catch {
                $uiDiagnostics.ui_diagnostics_exception_type = Get-SafeExceptionTypeName $_.Exception
            }
            $processEvidence.graceful_quit_failure = [ordered]@{
                postgres = if ($remaining) { $remaining.postgres.Count } else { $null }
                http = if ($remaining) { $remaining.http.Count } else { $null }
                scheduler = if ($remaining) { $remaining.scheduler.Count } else { $null }
                process_diagnostics_exception_type = $processDiagnosticExceptionType
                session_diagnostics_exception_type = $sessionDiagnosticExceptionType
                operator_session_state = $operatorSessionState
                ui_diagnostics_available = $uiDiagnostics.ui_diagnostics_available
                ui_diagnostics_exception_type = $uiDiagnostics.ui_diagnostics_exception_type
                diagnostic_code = $uiDiagnostics.diagnostic_code
                runtime_failed_visible = $uiDiagnostics.runtime_failed_visible
                operator_auth_error = $uiDiagnostics.operator_auth_error
                operator_session_revoked = $uiDiagnostics.operator_session_revoked
                shutdown_error = $uiDiagnostics.shutdown_error
            }
            throw $primaryFailure
        }
        return
    }

    # The smoke fixture cannot authenticate when a failed runtime has no healthy Operator API.
    # Stop only this test-owned Desktop process; healthy Controller stops above always use OWNER auth.
    if ($ProcessId -notin $desktopPids) { throw "controller_smoke_desktop_process_not_owned" }
    $tracked = Get-TrackedProcess $ProcessId $DesktopExecutable
    if (-not $tracked) { throw "controller_smoke_desktop_process_unavailable" }
    try {
        $tracked.Kill()
        if (-not $tracked.WaitForExit(10000)) { throw "controller_smoke_failed_runtime_cleanup_timeout" }
    } finally {
        $tracked.Dispose()
    }
    Wait-Until { (Get-ControllerProcesses).all.Count -eq 0 } 20 `
        "controller_smoke_failed_runtime_processes_remain"
}

function Get-ProcessExitTime(
    [System.Diagnostics.Process]$Process,
    [IntPtr]$NativeHandle,
    [string]$Failure
) {
    Wait-Until {
        try {
            $Process.Refresh()
            return $Process.HasExited
        } catch { return $false }
    } 20 $Failure
    $fileTime = [ThreadsControllerSmoke.NativeMethods]::GetProcessExitFileTime($NativeHandle)
    if ($fileTime -eq 0) { throw $Failure }
    return [DateTime]::FromFileTimeUtc($fileTime)
}

function Get-LocalControllerIpv4 {
    $defaultRoute = Get-NetRoute -DestinationPrefix "0.0.0.0/0" -ErrorAction SilentlyContinue |
        Sort-Object RouteMetric, InterfaceMetric |
        Select-Object -First 1
    $addresses = @(Get-NetIPAddress -AddressFamily IPv4 -AddressState Preferred -ErrorAction Stop |
        Where-Object {
            $_.IPAddress -notlike "127.*" -and $_.IPAddress -notlike "169.254.*" -and
            $_.IPAddress -notlike "224.*" -and $_.IPAddress -ne "255.255.255.255"
        })
    if ($defaultRoute) {
        $preferred = $addresses | Where-Object { $_.InterfaceIndex -eq $defaultRoute.InterfaceIndex } |
            Select-Object -First 1
        if ($preferred) { return [string]$preferred.IPAddress }
    }
    $selected = $addresses | Select-Object -First 1
    if (-not $selected) { throw "controller_smoke_lan_ipv4_unavailable" }
    return [string]$selected.IPAddress
}

function Get-FreeHttpsPort {
    $listener = [System.Net.Sockets.TcpListener]::new([Net.IPAddress]::Any, 0)
    $listener.Start()
    try { return ([System.Net.IPEndPoint]$listener.LocalEndpoint).Port }
    finally { $listener.Stop() }
}

function Get-UnassignedControllerIpv4 {
    $assigned = [System.Collections.Generic.HashSet[string]]::new(
        [System.StringComparer]::OrdinalIgnoreCase
    )
    foreach ($item in Get-NetIPAddress -AddressFamily IPv4 -ErrorAction Stop) {
        $null = $assigned.Add([string]$item.IPAddress)
    }
    foreach ($candidate in @("192.0.2.254", "198.51.100.254", "203.0.113.254")) {
        if (-not $assigned.Contains($candidate)) { return $candidate }
    }
    throw "controller_smoke_unassigned_ipv4_unavailable"
}

function Get-AlternateControllerIpv4([string]$CurrentAddress) {
    $candidate = Get-NetIPAddress -AddressFamily IPv4 -AddressState Preferred -ErrorAction Stop |
        Where-Object {
            $_.IPAddress -cne $CurrentAddress -and $_.IPAddress -notlike "127.*" -and
            $_.IPAddress -notlike "169.254.*" -and $_.IPAddress -notlike "224.*" -and
            $_.IPAddress -ne "255.255.255.255"
        } |
        Select-Object -First 1
    if ($candidate) { return [string]$candidate.IPAddress }
    return $null
}

function Open-ControllerEndpointReconfiguration([int]$ProcessId) {
    Invoke-Button $ProcessId "Reconfigure HTTPS endpoint…"
}

function Set-ControllerEndpointFields([int]$ProcessId, [string]$Address, [int]$Port) {
    Set-LoginInput `
        -ProcessId $ProcessId `
        -Name "Stable LAN IPv4 address" `
        -Value $Address `
        -AutomationId "controller-lan-address" `
        -AllowedControlTypes @([System.Windows.Automation.ControlType]::Edit)
    Set-LoginInput `
        -ProcessId $ProcessId `
        -Name "HTTPS port" `
        -Value ([string]$Port) `
        -AutomationId "controller-https-port" `
        -AllowedControlTypes @(
            [System.Windows.Automation.ControlType]::Spinner,
            [System.Windows.Automation.ControlType]::Edit
        )
}

function Get-FileSha256([string]$Path) {
    (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

function Invoke-ControllerHttpsProbe(
    [object]$Config,
    [ValidateSet("/health", "/ready")]
    [string]$Path,
    [int]$TimeoutMilliseconds = 3000
) {
    $rootPath = Join-Path (Join-Path $controllerRoot "tls") "root-cert.der"
    return Invoke-PrivateRootHttpsProbe `
        -Port ([int]$Config.endpointPort) `
        -Path $Path `
        -RootCertificatePath $rootPath `
        -TimeoutMilliseconds $TimeoutMilliseconds
}

function Get-PostOwnerRuntimeSnapshot([object]$Config) {
    $owned = $null
    $processQueryOutcome = "PASS"
    $processQueryExceptionType = $null
    try {
        $owned = Get-ControllerProcesses
    } catch {
        $processQueryOutcome = "FAIL"
        $processQueryExceptionType = Get-SafeExceptionTypeName $_.Exception
    }

    $listenerAddresses = @()
    $listenerQueryOutcome = "PASS"
    $listenerQueryExceptionType = $null
    try {
        $listenerAddresses = @(Get-ListenerAddresses ([int]$Config.endpointPort))
    } catch {
        $listenerQueryOutcome = "FAIL"
        $listenerQueryExceptionType = Get-SafeExceptionTypeName $_.Exception
    }

    $ownerSessionCount = $null
    $ownerSessionQueryOutcome = "PASS"
    $ownerSessionExceptionType = $null
    try {
        $ownerSessionCount = Get-ActiveOwnerSessionCount $Config
    } catch {
        $ownerSessionQueryOutcome = "FAIL"
        $ownerSessionExceptionType = Get-SafeExceptionTypeName $_.Exception
    }

    $servingKeyPath = Join-Path `
        (Join-Path (Join-Path $controllerRoot "tls") "serving") "leaf-key.pem"
    return [ordered]@{
        captured_utc = [DateTimeOffset]::UtcNow.ToString("O")
        endpoint_port = [int]$Config.endpointPort
        process_query_outcome = $processQueryOutcome
        process_query_exception_type = $processQueryExceptionType
        postgres_count = if ($owned) { @($owned.postgres).Count } else { 0 }
        postgres_pids = if ($owned) { @($owned.postgres | ForEach-Object { [int]$_.ProcessId }) } else { @() }
        http_count = if ($owned) { @($owned.http).Count } else { 0 }
        http_pids = if ($owned) { @($owned.http | ForEach-Object { [int]$_.ProcessId }) } else { @() }
        scheduler_count = if ($owned) { @($owned.scheduler).Count } else { 0 }
        scheduler_pids = if ($owned) { @($owned.scheduler | ForEach-Object { [int]$_.ProcessId }) } else { @() }
        listener_query_outcome = $listenerQueryOutcome
        listener_query_exception_type = $listenerQueryExceptionType
        endpoint_listener_count = $listenerAddresses.Count
        endpoint_listener_addresses = $listenerAddresses
        serving_leaf_key_present = Test-Path -LiteralPath $servingKeyPath -PathType Leaf
        owner_session_query_outcome = $ownerSessionQueryOutcome
        owner_session_query_exception_type = $ownerSessionExceptionType
        active_owner_session_count = $ownerSessionCount
    }
}

function Save-PostOwnerRuntimeProbe(
    [object]$Snapshot,
    [switch]$BootstrapProcessSnapshot,
    [switch]$AfterOwnerBootstrap,
    [switch]$FailedProbe
) {
    if (-not $script:processEvidence.Contains("post_owner_runtime_probe")) {
        $script:processEvidence["post_owner_runtime_probe"] = [ordered]@{
            after_owner_bootstrap_process_snapshot = $null
            after_owner_bootstrap = $null
            latest_probe = $null
            last_failed_probe = $null
        }
    }
    $probeEvidence = $script:processEvidence["post_owner_runtime_probe"]
    if ($BootstrapProcessSnapshot) {
        $probeEvidence.after_owner_bootstrap_process_snapshot = $Snapshot
    }
    if ($AfterOwnerBootstrap) {
        $probeEvidence.after_owner_bootstrap = $Snapshot
    }
    $probeEvidence.latest_probe = $Snapshot
    if ($FailedProbe) {
        $probeEvidence.last_failed_probe = $Snapshot
    }
}

function Get-PostOwnerRuntimeFailureCode([object]$Snapshot) {
    if ($Snapshot.process_query_outcome -ne "PASS") {
        return "controller_post_owner_process_snapshot_failed"
    }
    if ($Snapshot.postgres_count -eq 0) { return "controller_post_owner_postgres_process_missing" }
    if ($Snapshot.postgres_count -ne 1) { return "controller_post_owner_postgres_process_count_invalid" }
    if ($Snapshot.http_count -eq 0) { return "controller_post_owner_http_process_missing" }
    if ($Snapshot.http_count -ne 1) { return "controller_post_owner_http_process_count_invalid" }
    if ($Snapshot.scheduler_count -eq 0) { return "controller_post_owner_scheduler_process_missing" }
    if ($Snapshot.scheduler_count -ne 1) { return "controller_post_owner_scheduler_process_count_invalid" }
    if ($Snapshot.listener_query_outcome -ne "PASS") {
        return "controller_post_owner_listener_snapshot_failed"
    }
    if ($Snapshot.endpoint_listener_count -eq 0) {
        return "controller_post_owner_endpoint_listener_absent"
    }
    return $null
}

function Get-ControllerHttpsProbeFailureCode([object]$Probe, [string]$Path) {
    switch ([string]$Probe.failure_stage) {
        "root_certificate_load" { return "controller_tls_probe_root_certificate_load_failed" }
        "tcp_connect" {
            if ($Probe.tcp_connect.outcome -eq "TIMEOUT") {
                return "controller_tls_probe_tcp_connect_timeout"
            }
            return "controller_tls_probe_tcp_connect_failed"
        }
        "tls_authentication" { return "controller_tls_probe_validation_failed" }
        "http_request_write" { return "controller_tls_probe_http_write_failed" }
        "http_response" { return "controller_tls_probe_http_response_failed" }
        "http_status" {
            if ($Path -eq "/health") {
                return "controller_https_health_status_$($Probe.http_response.status_code)"
            }
            return "controller_https_readiness_status_$($Probe.http_response.status_code)"
        }
        default { return "controller_tls_probe_failed" }
    }
}

function Wait-ForControllerHttps(
    [object]$Config,
    [int]$TimeoutSeconds = 60,
    [switch]$AfterOwnerBootstrap
) {
    $waitTimer = [System.Diagnostics.Stopwatch]::StartNew()
    if ($AfterOwnerBootstrap) {
        $bootstrapSnapshot = Get-PostOwnerRuntimeSnapshot $Config
        $bootstrapSnapshot["health_probe"] = [ordered]@{ outcome = "NOT_RUN" }
        $bootstrapSnapshot["readiness_probe"] = [ordered]@{ outcome = "NOT_RUN" }
        Save-PostOwnerRuntimeProbe $bootstrapSnapshot -BootstrapProcessSnapshot
    }

    $attempt = 0
    $lastSnapshot = $null
    do {
        $attempt++
        $healthProbe = Invoke-ControllerHttpsProbe $Config "/health"
        $readinessProbe = Invoke-ControllerHttpsProbe $Config "/ready"
        $lastSnapshot = Get-PostOwnerRuntimeSnapshot $Config
        $lastSnapshot["attempt"] = $attempt
        $lastSnapshot["elapsed_ms"] = $waitTimer.ElapsedMilliseconds
        $lastSnapshot["health_probe"] = $healthProbe
        $lastSnapshot["readiness_probe"] = $readinessProbe
        $runtimeFailureCode = Get-PostOwnerRuntimeFailureCode $lastSnapshot
        $failedProbe = $healthProbe.outcome -ne "PASS" -or
            $readinessProbe.outcome -ne "PASS" -or $null -ne $runtimeFailureCode
        Save-PostOwnerRuntimeProbe `
            $lastSnapshot `
            -AfterOwnerBootstrap:($AfterOwnerBootstrap -and $attempt -eq 1) `
            -FailedProbe:$failedProbe

        $probesPassed = $healthProbe.outcome -eq "PASS" -and $readinessProbe.outcome -eq "PASS"
        if (-not $runtimeFailureCode -and $probesPassed) { return $lastSnapshot }
        if ($waitTimer.Elapsed.TotalSeconds -ge $TimeoutSeconds) { break }
        Start-Sleep -Milliseconds 250
    } while ($true)

    $runtimeFailureCode = Get-PostOwnerRuntimeFailureCode $lastSnapshot
    if ($runtimeFailureCode) { throw $runtimeFailureCode }
    if ($healthProbe.outcome -ne "PASS") {
        throw (Get-ControllerHttpsProbeFailureCode $healthProbe "/health")
    }
    throw (Get-ControllerHttpsProbeFailureCode $readinessProbe "/ready")
}

function Test-PlaintextHttpRejected([object]$Config) {
    $client = [System.Net.Sockets.TcpClient]::new()
    try {
        $client.ReceiveTimeout = 1000
        $client.SendTimeout = 1000
        $client.Connect([Net.IPAddress]::Loopback, [int]$Config.endpointPort)
        $stream = $client.GetStream()
        $request = [Text.Encoding]::ASCII.GetBytes(
            "GET /ready HTTP/1.1`r`nHost: 127.0.0.1`r`nConnection: close`r`n`r`n"
        )
        $stream.Write($request, 0, $request.Length)
        $buffer = [byte[]]::new(16)
        try {
            $read = $stream.Read($buffer, 0, $buffer.Length)
            return $read -eq 0 -or [Text.Encoding]::ASCII.GetString($buffer, 0, $read) -notmatch '^HTTP/'
        } catch [System.IO.IOException] {
            return $true
        }
    } catch [System.Net.Sockets.SocketException] {
        return $true
    } finally {
        $client.Dispose()
    }
}

function ConvertTo-SmokePem([string]$Label, [byte[]]$Der) {
    $encoded = [Convert]::ToBase64String($Der)
    $lines = [System.Collections.Generic.List[string]]::new()
    for ($offset = 0; $offset -lt $encoded.Length; $offset += 64) {
        $count = [Math]::Min(64, $encoded.Length - $offset)
        $lines.Add($encoded.Substring($offset, $count))
    }
    return "-----BEGIN $Label-----`n$($lines -join "`n")`n-----END $Label-----`n"
}

function Set-ExpiringControllerLeaf([object]$Config) {
    $tlsDirectory = Join-Path $controllerRoot "tls"
    $rootCertificatePath = Join-Path $tlsDirectory "root-cert.der"
    $protectedRootKeyPath = Join-Path $tlsDirectory "root-key.dpapi"
    $rootCertificate = [System.Security.Cryptography.X509Certificates.X509Certificate2]::new(
        [System.IO.File]::ReadAllBytes($rootCertificatePath)
    )
    $protectedRootKey = [System.IO.File]::ReadAllBytes($protectedRootKeyPath)
    $rootKeyBytes = [System.Security.Cryptography.ProtectedData]::Unprotect(
        $protectedRootKey,
        $null,
        [System.Security.Cryptography.DataProtectionScope]::CurrentUser
    )
    $rootKey = [System.Security.Cryptography.ECDsa]::Create()
    $consumed = 0
    try {
        $rootKey.ImportPkcs8PrivateKey($rootKeyBytes, [ref]$consumed)
        if ($consumed -ne $rootKeyBytes.Length) { throw "controller_smoke_root_key_parse_failed" }
        $leafKey = [System.Security.Cryptography.ECDsa]::Create()
        $leafKey.KeySize = 256
        $request = [System.Security.Cryptography.X509Certificates.CertificateRequest]::new(
            "CN=Threads Controller lifecycle smoke leaf",
            $leafKey,
            [System.Security.Cryptography.HashAlgorithmName]::SHA256
        )
        $request.CertificateExtensions.Add(
            [System.Security.Cryptography.X509Certificates.X509BasicConstraintsExtension]::new(
                $false, $false, 0, $true
            )
        )
        $request.CertificateExtensions.Add(
            [System.Security.Cryptography.X509Certificates.X509KeyUsageExtension]::new(
                [System.Security.Cryptography.X509Certificates.X509KeyUsageFlags]::DigitalSignature,
                $true
            )
        )
        $eku = [System.Security.Cryptography.OidCollection]::new()
        $null = $eku.Add([System.Security.Cryptography.Oid]::new("1.3.6.1.5.5.7.3.1"))
        $request.CertificateExtensions.Add(
            [System.Security.Cryptography.X509Certificates.X509EnhancedKeyUsageExtension]::new(
                $eku, $false
            )
        )
        $sans = [System.Security.Cryptography.X509Certificates.SubjectAlternativeNameBuilder]::new()
        $sans.AddIpAddress([Net.IPAddress]::Parse([string]$Config.lanAddress))
        $sans.AddIpAddress([Net.IPAddress]::Loopback)
        $request.CertificateExtensions.Add($sans.Build())
        $serial = [byte[]]::new(16)
        $random = [System.Security.Cryptography.RandomNumberGenerator]::Create()
        try { $random.GetBytes($serial) }
        finally { $random.Dispose() }
        $now = [DateTimeOffset]::UtcNow
        $signatureGenerator =
            [System.Security.Cryptography.X509Certificates.X509SignatureGenerator]::CreateForECDsa(
                $rootKey
            )
        $leaf = $request.Create(
            $rootCertificate.SubjectName,
            $signatureGenerator,
            $now.AddMinutes(-5),
            $now.AddDays(20),
            $serial
        )
        $leafKeyBytes = $leafKey.ExportPkcs8PrivateKey()
        try {
            $protectedLeafKey = [System.Security.Cryptography.ProtectedData]::Protect(
                $leafKeyBytes,
                $null,
                [System.Security.Cryptography.DataProtectionScope]::CurrentUser
            )
            $leafDer = $leaf.Export([System.Security.Cryptography.X509Certificates.X509ContentType]::Cert)
            $rootDer = [System.IO.File]::ReadAllBytes($rootCertificatePath)
            [System.IO.File]::WriteAllBytes((Join-Path $tlsDirectory "leaf-cert.der"), $leafDer)
            [System.IO.File]::WriteAllBytes((Join-Path $tlsDirectory "leaf-key.dpapi"), $protectedLeafKey)
            [System.IO.File]::WriteAllText(
                (Join-Path $tlsDirectory "leaf-fullchain.pem"),
                (ConvertTo-SmokePem "CERTIFICATE" $leafDer) + (ConvertTo-SmokePem "CERTIFICATE" $rootDer),
                [System.Text.UTF8Encoding]::new($false)
            )
        } finally {
            [Array]::Clear($leafKeyBytes, 0, $leafKeyBytes.Length)
            $leafKey.Dispose()
            $leaf.Dispose()
        }
    } finally {
        [Array]::Clear($rootKeyBytes, 0, $rootKeyBytes.Length)
        $rootKey.Dispose()
        $rootCertificate.Dispose()
    }
}

function Assert-DatabaseValue([object]$Config, [string]$Expected) {
    $actual = Invoke-Psql $Config "SELECT marker FROM dx04_runtime_evidence WHERE id = 1;"
    if ($actual -cne $Expected) { throw "controller_durable_sentinel_mismatch" }
}

function Initialize-HealthyControllerFixture([int]$ProcessId, [object]$Config) {
    $databaseSystemIdentifier = Invoke-Psql $Config "SELECT system_identifier FROM pg_control_system();"
    $operatorUsersTable = Invoke-Psql $Config "SELECT to_regclass('public.operator_users') IS NOT NULL;"
    $operatorUserCount = Invoke-Psql $Config "SELECT COUNT(*) FROM public.operator_users;"
    $script:checks.no_owner_or_lan_bootstrap = $operatorUsersTable -eq "t" -and
        $operatorUserCount -eq "0" -and $Config.endpointPort -gt 0 -and $Config.databasePort -gt 0
    if (-not $script:checks.no_owner_or_lan_bootstrap) {
        throw "controller_m1_owner_boundary_invalid"
    }

    Bootstrap-ControllerOwner $ProcessId
    $null = Wait-ForOperatorSessionCount `
        $Config "bootstrap" 1 5 "controller_first_owner_session_missing"
    $null = Wait-ForControllerHttps $Config 60 -AfterOwnerBootstrap
    $enabledOwnerCount = Invoke-Psql $Config "SELECT COUNT(*) FROM public.operator_users WHERE role = 'OWNER' AND enabled;"
    $script:checks.local_first_owner_bootstrap = $enabledOwnerCount -eq "1"
    if (-not $script:checks.local_first_owner_bootstrap) {
        throw "controller_local_first_owner_bootstrap_invalid"
    }
    $script:checks.local_readiness_uses_private_root = $true
    if (-not (Test-PlaintextHttpRejected $Config)) {
        throw "controller_plaintext_health_listener_present"
    }
    $script:checks.plaintext_health_rejected = $true
    $owned = Assert-ControllerProcesses
    $identity = Add-ControllerRuntimeIdentitySnapshot `
        "after_owner_bootstrap" $owned $Config
    if ([int]$identity.http_pid -eq [int]$identity.scheduler_pid) {
        throw "controller_http_scheduler_process_collapsed"
    }
    $script:checks.separate_http_and_scheduler_processes = $true
    $dbListeners = @(Get-ListenerAddresses ([int]$Config.databasePort))
    $httpListeners = @(Get-ListenerAddresses ([int]$Config.endpointPort))
    $privateDatabaseAndWildcardTls =
        $dbListeners.Count -eq 1 -and $dbListeners[0] -eq "127.0.0.1" -and
        $httpListeners.Count -eq 1 -and $httpListeners[0] -eq "0.0.0.0" -and
        (Test-Path -LiteralPath $servingKeyPath -PathType Leaf)
    Set-Check "loopback_postgres_wildcard_https_listener" $privateDatabaseAndWildcardTls `
        "controller_listener_topology_invalid"
    if (-not $privateDatabaseAndWildcardTls) { throw "controller_listener_topology_invalid" }
    Invoke-Psql $Config "CREATE TABLE dx04_runtime_evidence (id integer PRIMARY KEY, marker text NOT NULL); INSERT INTO dx04_runtime_evidence (id, marker) VALUES (1, '$script:sentinel');" | Out-Null
    Assert-DatabaseValue $Config $script:sentinel

    return [pscustomobject]@{
        Config = Get-ControllerConfig
        ControllerId = [string]$Config.controllerId
        DatabaseSystemIdentifier = [string]$databaseSystemIdentifier
        Sentinel = [string]$script:sentinel
        RuntimeIdentity = $identity
        Processes = $owned
        RootFingerprint = Get-FileSha256 $rootCertificatePath
        EndpointPort = [int]$Config.endpointPort
        LanAddress = [string]$Config.lanAddress
    }
}

try {
    if ($VerifiedSourceRevision) {
        if (-not $VerifiedWorktreeClean -or $VerifiedSourceRevision -ne $ExpectedSourceRevision) {
            throw "controller_smoke_requires_verified_exact_clean_source_revision"
        }
        $head = $VerifiedSourceRevision
        $worktreeIsClean = $true
    } else {
        $headLines = @(& git -C $repoRoot rev-parse HEAD)
        if ($LASTEXITCODE -ne 0 -or $headLines.Count -eq 0) { throw "controller_git_head_unavailable" }
        $head = ($headLines -join "`n").Trim()
        $dirtyLines = @(& git -C $repoRoot status --porcelain)
        if ($LASTEXITCODE -ne 0) { throw "controller_git_status_unavailable" }
        $dirty = [string]::Join("`n", [string[]]$dirtyLines)
        if ($head -ne $ExpectedSourceRevision -or $dirty.Length -ne 0) {
            throw "controller_smoke_requires_exact_clean_source_revision"
        }
        $worktreeIsClean = $true
    }
    if ($deviceConfigExisted -or $controllerRootExisted -or
        (Test-Path -LiteralPath $savedRoot) -or (Test-Path -LiteralPath $unownedRoot)) {
        throw "controller_smoke_requires_disposable_windows_profile"
    }
    $checks.clean_profile = $true

    if (-not (Test-Path -LiteralPath $runtimeExecutable -PathType Leaf) -or
        -not (Test-Path -LiteralPath $pgCtl -PathType Leaf) -or
        -not (Test-Path -LiteralPath $psql -PathType Leaf)) {
        throw "controller_shared_runtime_bundle_missing"
    }
    $checks.runtime_bundle_shared = $true
    $env:THREADS_DESKTOP_RUNTIME_DIR = $RuntimeRoot

    foreach ($property in @(
        "ThreadsDesktop", "ThreadsDesktopController", "Threads Desktop"
    )) {
        if (Test-Path -LiteralPath $applicationRunKey) {
            $values = Get-ItemProperty -LiteralPath $applicationRunKey
            if ($values.PSObject.Properties.Name -contains $property) {
                $originalRunEntries[$property] = [string]$values.$property
            }
        }
    }

    $desktop = Start-Desktop
    Invoke-Button $desktop.Id "Provision as Controller"
    Wait-ControllerState $desktop.Id "Configure Controller HTTPS" $null 420
    $lanAddress = Get-LocalControllerIpv4
    $selectedHttpsPort = Get-FreeHttpsPort
    Set-ControllerEndpointFields $desktop.Id $lanAddress $selectedHttpsPort
    Invoke-Button $desktop.Id "Configure HTTPS"
    Wait-Until {
        $configured = Get-ControllerConfig
        return $configured.schemaVersion -eq 2 -and
            $configured.lanAddress -ceq $lanAddress -and
            $configured.endpointPort -eq $selectedHttpsPort
    } 30 "controller_https_configuration_not_persisted"
    Wait-Until {
        $window = Get-Window $desktop.Id
        return $null -ne (Find-Element $window "Set up first Owner" `
            ([System.Windows.Automation.ControlType]::Button))
    } 30 "controller_https_owner_bootstrap_boundary_not_ready"
    $config = Get-ControllerConfig
    $rootCertificatePath = Join-Path (Join-Path $controllerRoot "tls") "root-cert.der"
    $rootPrivateKeyPath = Join-Path (Join-Path $controllerRoot "tls") "root-key.dpapi"
    $servingKeyPath = Join-Path (Join-Path (Join-Path $controllerRoot "tls") "serving") "leaf-key.pem"
    if (-not (Test-Path -LiteralPath $rootCertificatePath -PathType Leaf) -or
        -not (Test-Path -LiteralPath $rootPrivateKeyPath -PathType Leaf) -or
        (Test-Path -LiteralPath (Join-Path (Join-Path $controllerRoot "tls") "root-key.pem"))) {
        throw "controller_tls_identity_custody_invalid"
    }
    $rootFingerprint = "SHA256:" + (Get-FileHash -LiteralPath $rootCertificatePath -Algorithm SHA256).Hash.ToLowerInvariant()
    $fingerprintDisplay = Find-TextContaining (Get-Window $desktop.Id) $rootFingerprint
    $checks.controller_https_configuration_persisted =
        $config.schemaVersion -eq 2 -and $config.lanAddress -ceq $lanAddress -and
        $config.endpointPort -eq $selectedHttpsPort
    $checks.controller_https_root_fingerprint_matches_ui = $null -ne $fingerprintDisplay
    if (-not $checks.controller_https_configuration_persisted -or
        -not $checks.controller_https_root_fingerprint_matches_ui) {
        throw "controller_https_identity_summary_invalid"
    }
    $leafCertificatePath = Join-Path (Join-Path $controllerRoot "tls") "leaf-cert.der"
    if ($Scenario -eq "bootstrap_https_cutover_tray") {
        $initialRootFingerprint = Get-FileSha256 $rootCertificatePath
        $initialLeafFingerprint = Get-FileSha256 $leafCertificatePath
        $initialPort = [int]$config.endpointPort

        # Local setup can correct an explicitly selected endpoint before the first Owner exists.
        Open-ControllerEndpointReconfiguration $desktop.Id
        $unavailableAddress = Get-UnassignedControllerIpv4
        Set-ControllerEndpointFields $desktop.Id $unavailableAddress $initialPort
        Invoke-Button $desktop.Id "Apply endpoint change"
        Wait-Until {
            $null -ne (Find-TextContaining (Get-Window $desktop.Id) "not assigned to this PC")
        } 15 "controller_unavailable_ip_error_not_visible"
        $unchanged = Get-ControllerConfig
        if ($unchanged.lanAddress -cne $lanAddress -or $unchanged.endpointPort -ne $initialPort -or
            (Get-FileSha256 $rootCertificatePath) -cne $initialRootFingerprint -or
            (Get-FileSha256 $leafCertificatePath) -cne $initialLeafFingerprint) {
            throw "controller_unavailable_ip_changed_persisted_state"
        }
        $checks.endpoint_unavailable_ip_rolls_back = $true

        $endpointReservation = [System.Net.Sockets.TcpListener]::new([Net.IPAddress]::Any, 0)
        $endpointReservation.Start()
        $collisionPort = ([System.Net.IPEndPoint]$endpointReservation.LocalEndpoint).Port
        try {
            Set-ControllerEndpointFields $desktop.Id $lanAddress $collisionPort
            Invoke-Button $desktop.Id "Apply endpoint change"
            Wait-Until {
                $null -ne (Find-TextContaining (Get-Window $desktop.Id) "already in use")
            } 15 "controller_endpoint_collision_error_not_visible"
            $unchanged = Get-ControllerConfig
            if ($unchanged.lanAddress -cne $lanAddress -or $unchanged.endpointPort -ne $initialPort -or
                (Get-FileSha256 $rootCertificatePath) -cne $initialRootFingerprint -or
                (Get-FileSha256 $leafCertificatePath) -cne $initialLeafFingerprint) {
                throw "controller_endpoint_collision_changed_persisted_state"
            }
            $checks.endpoint_collision_rolls_back = $true
        } finally {
            $endpointReservation.Stop()
        }

        $selectedHttpsPort = Get-FreeHttpsPort
        Set-ControllerEndpointFields $desktop.Id $lanAddress $selectedHttpsPort
        Invoke-Button $desktop.Id "Apply endpoint change"
        Wait-Until {
            $configured = Get-ControllerConfig
            return $configured.lanAddress -ceq $lanAddress -and
                $configured.endpointPort -eq $selectedHttpsPort
        } 30 "controller_pre_owner_endpoint_reconfiguration_not_persisted"
        $config = Get-ControllerConfig
        $afterPreOwnerLeafFingerprint = Get-FileSha256 $leafCertificatePath
        if ((Get-FileSha256 $rootCertificatePath) -cne $initialRootFingerprint -or
            $afterPreOwnerLeafFingerprint -ceq $initialLeafFingerprint) {
            throw "controller_pre_owner_reconfiguration_identity_invalid"
        }
        $alternateAddress = Get-AlternateControllerIpv4 $lanAddress
        if ($alternateAddress) {
            Open-ControllerEndpointReconfiguration $desktop.Id
            Set-ControllerEndpointFields $desktop.Id $alternateAddress $selectedHttpsPort
            Invoke-Button $desktop.Id "Apply endpoint change"
            Wait-Until {
                $updated = Get-ControllerConfig
                return $updated.lanAddress -ceq $alternateAddress -and
                    $updated.endpointPort -eq $selectedHttpsPort
            } 30 "controller_pre_owner_ip_reconfiguration_not_persisted"
            $config = Get-ControllerConfig
            $ipChangedLeafFingerprint = Get-FileSha256 $leafCertificatePath
            if ((Get-FileSha256 $rootCertificatePath) -cne $initialRootFingerprint -or
                $ipChangedLeafFingerprint -ceq $afterPreOwnerLeafFingerprint -or
                (Find-TextContaining (Get-Window $desktop.Id) $rootFingerprint) -eq $null) {
                throw "controller_pre_owner_ip_reconfiguration_identity_invalid"
            }
            $lanAddress = $alternateAddress
            $afterPreOwnerLeafFingerprint = $ipChangedLeafFingerprint
            $processEvidence.endpoint_ip_reconfiguration = "PASS"
        } else {
            $processEvidence.endpoint_ip_reconfiguration = "UNAVAILABLE: no second assigned IPv4"
        }
        $beforeOwnerProcesses = Get-ControllerProcesses
        if ($beforeOwnerProcesses.postgres.Count -ne 1 -or $beforeOwnerProcesses.http.Count -ne 0 -or
            $beforeOwnerProcesses.scheduler.Count -ne 0 -or
            @(Get-ListenerAddresses $initialPort).Count -ne 0 -or
            @(Get-ListenerAddresses $selectedHttpsPort).Count -ne 0 -or
            (Find-TextContaining (Get-Window $desktop.Id) $rootFingerprint) -eq $null) {
            throw "controller_pre_owner_reconfiguration_started_listener_or_changed_root"
        }
        $checks.endpoint_reconfigure_before_owner = $true
    }
    $config = Get-ControllerConfig
    $checks.controller_https_configuration_persisted =
        $config.schemaVersion -eq 2 -and $config.lanAddress -ceq $lanAddress -and
        $config.endpointPort -eq $selectedHttpsPort
    if (-not $checks.controller_https_configuration_persisted) {
        throw "controller_https_configuration_not_persisted"
    }
    $beforeOwnerProcesses = Get-ControllerProcesses
    $beforeOwnerListeners = @(Get-ListenerAddresses ([int]$config.endpointPort))
    $checks.no_lan_listener_before_local_owner_bootstrap =
        $beforeOwnerProcesses.postgres.Count -eq 1 -and
        $beforeOwnerProcesses.http.Count -eq 0 -and
        $beforeOwnerProcesses.scheduler.Count -eq 0 -and
        $beforeOwnerListeners.Count -eq 0 -and
        -not (Test-Path -LiteralPath $servingKeyPath)
    if (-not $checks.no_lan_listener_before_local_owner_bootstrap) {
        throw "controller_lan_listener_started_before_owner_bootstrap"
    }
    $controllerIdentity = [string]$config.controllerId
    $controllerAppData = [System.IO.Path]::GetFullPath($controllerRoot)
    if (-not $controllerAppData.StartsWith(
        [System.IO.Path]::GetFullPath($localAppData),
        [System.StringComparison]::OrdinalIgnoreCase
    )) { throw "controller_root_outside_current_user_local_appdata" }
    $currentSid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    $acl = Get-Acl -LiteralPath $controllerRoot
    $currentUserFullControl = @($acl.Access | Where-Object {
        try {
            $_.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value -eq $currentSid -and
            ($_.FileSystemRights -band [Security.AccessControl.FileSystemRights]::FullControl) -eq
                [Security.AccessControl.FileSystemRights]::FullControl
        } catch { $false }
    }).Count -gt 0
    Set-Check "controller_root_current_user_acl" $currentUserFullControl "controller_root_acl_invalid"
    if (-not $currentUserFullControl) { throw "controller_root_acl_invalid" }

    $plainCredential = Get-DatabaseCredential
    [Array]::Clear($plainCredential, 0, $plainCredential.Length)
    $checks.dpapi_current_user_round_trip = $true
    $serializedConfig = Get-Content -LiteralPath (Join-Path $controllerRoot "controller.json") -Raw
    $checks.atomic_non_secret_config =
        $serializedConfig -notmatch '(?i)password|secret|databaseurl|authorization|token' -and
        $config.schemaVersion -eq 2 -and $config.clusterInitialized -eq $true -and
        $config.lanAddress -ceq $lanAddress -and $config.endpointPort -eq $selectedHttpsPort
    if (-not $checks.atomic_non_secret_config) { throw "controller_config_contains_secret_or_invalid_state" }

    $fixture = Initialize-HealthyControllerFixture $desktop.Id $config
    $config = $fixture.Config
    $controllerIdentity = $fixture.ControllerId
    $databaseSystemIdentifier = $fixture.DatabaseSystemIdentifier
    $initialRuntimeIdentity = $fixture.RuntimeIdentity
    $owned = $fixture.Processes
    $fixtureSessionStage = "${Scenario}_fixture_signed_in"
    $null = Set-OperatorSessionMode $desktop.Id $config "signed_in" `
        $fixtureSessionStage "controller_scenario_fixture_owner_session_missing"
    $initialHttpPid = [int]$initialRuntimeIdentity.http_pid
    $initialSchedulerPid = [int]$initialRuntimeIdentity.scheduler_pid
    $initialPostgresPid = [int]$initialRuntimeIdentity.postgres_pid
    if ($initialHttpPid -eq $initialSchedulerPid) {
        throw "controller_http_scheduler_process_collapsed"
    }
    $checks.separate_http_and_scheduler_processes = $true

    if ($Scenario -eq "bootstrap_https_cutover_tray") {
    # Failed post-Owner attempts preserve the endpoint and both certificate identities.
    $ownedBeforeEndpointChange = Assert-ControllerProcesses
    $oldHttpsPort = [int]$config.endpointPort
    $oldPostgresPid = [int]$ownedBeforeEndpointChange.postgres[0].ProcessId
    $oldHttpPid = [int]$ownedBeforeEndpointChange.http[0].ProcessId
    $oldSchedulerPid = [int]$ownedBeforeEndpointChange.scheduler[0].ProcessId
    $rootBeforeEndpointChange = Get-FileSha256 $rootCertificatePath
    $leafBeforeEndpointChange = Get-FileSha256 $leafCertificatePath

    Open-ControllerEndpointReconfiguration $desktop.Id
    Set-ControllerEndpointFields $desktop.Id $unavailableAddress $oldHttpsPort
    Invoke-Button $desktop.Id "Apply endpoint change"
    Wait-Until {
        $null -ne (Find-TextContaining (Get-Window $desktop.Id) "not assigned to this PC")
    } 15 "controller_running_unavailable_ip_error_not_visible"
    $null = Wait-ForControllerHttps $config 5
    if ((Get-ControllerConfig).endpointPort -ne $oldHttpsPort -or
        (Get-FileSha256 $rootCertificatePath) -cne $rootBeforeEndpointChange -or
        (Get-FileSha256 $leafCertificatePath) -cne $leafBeforeEndpointChange) {
        throw "controller_running_unavailable_ip_changed_state"
    }
    $null = Wait-ForOperatorSessionCount `
        $config "failed_ip_reconfigure" 1 3 "controller_endpoint_reconfigure_session_lost_on_unavailable_ip"
    $checks.endpoint_running_unavailable_ip_rolls_back = $true
    $checks.endpoint_running_unavailable_ip_preserves_owner_session = $true

    $endpointReservation = [System.Net.Sockets.TcpListener]::new([Net.IPAddress]::Any, 0)
    $endpointReservation.Start()
    $collisionPort = ([System.Net.IPEndPoint]$endpointReservation.LocalEndpoint).Port
    try {
        Set-ControllerEndpointFields $desktop.Id $lanAddress $collisionPort
        Invoke-Button $desktop.Id "Apply endpoint change"
        Wait-Until {
            $null -ne (Find-TextContaining (Get-Window $desktop.Id) "already in use")
        } 15 "controller_running_endpoint_collision_error_not_visible"
        $null = Wait-ForControllerHttps $config 5
        $afterCollisionProcesses = Assert-ControllerProcesses
        if ((Get-ControllerConfig).endpointPort -ne $oldHttpsPort -or
            (Get-FileSha256 $rootCertificatePath) -cne $rootBeforeEndpointChange -or
            (Get-FileSha256 $leafCertificatePath) -cne $leafBeforeEndpointChange -or
            [int]$afterCollisionProcesses.postgres[0].ProcessId -ne $oldPostgresPid -or
            [int]$afterCollisionProcesses.http[0].ProcessId -ne $oldHttpPid -or
            [int]$afterCollisionProcesses.scheduler[0].ProcessId -ne $oldSchedulerPid) {
            throw "controller_running_endpoint_collision_changed_runtime_or_identity"
        }
        $null = Wait-ForOperatorSessionCount `
            $config "failed_port_reconfigure" 1 3 "controller_endpoint_reconfigure_session_lost_on_collision"
        $checks.endpoint_running_collision_rolls_back = $true
        $checks.endpoint_running_collision_preserves_owner_session = $true
    } finally {
        $endpointReservation.Stop()
    }

    $newHttpsPort = Get-FreeHttpsPort
    $oldHttpTracked = Get-TrackedProcess $oldHttpPid $runtimeExecutable
    $oldSchedulerTracked = Get-TrackedProcess $oldSchedulerPid $runtimeExecutable
    if (-not $oldHttpTracked -or -not $oldSchedulerTracked) {
        if ($oldHttpTracked) { $oldHttpTracked.Dispose() }
        if ($oldSchedulerTracked) { $oldSchedulerTracked.Dispose() }
        throw "controller_reconfiguration_process_handle_unavailable"
    }
    $schedulerExitHandle = [ThreadsControllerSmoke.NativeMethods]::OpenProcessForExitTime(
        [uint32]$oldSchedulerPid
    )
    $httpExitHandle = [ThreadsControllerSmoke.NativeMethods]::OpenProcessForExitTime(
        [uint32]$oldHttpPid
    )
    if ($schedulerExitHandle -eq [IntPtr]::Zero -or $httpExitHandle -eq [IntPtr]::Zero) {
        if ($schedulerExitHandle -ne [IntPtr]::Zero) {
            $null = [ThreadsControllerSmoke.NativeMethods]::CloseHandle($schedulerExitHandle)
        }
        if ($httpExitHandle -ne [IntPtr]::Zero) {
            $null = [ThreadsControllerSmoke.NativeMethods]::CloseHandle($httpExitHandle)
        }
        $oldHttpTracked.Dispose()
        $oldSchedulerTracked.Dispose()
        throw "controller_reconfiguration_process_handle_unavailable"
    }
    try {
        Set-ControllerEndpointFields $desktop.Id $lanAddress $newHttpsPort
        Invoke-Button $desktop.Id "Apply endpoint change"
        Wait-Until {
            $updated = Get-ControllerConfig
            return $updated.lanAddress -ceq $lanAddress -and $updated.endpointPort -eq $newHttpsPort
        } 30 "controller_running_endpoint_reconfiguration_not_persisted"
        $config = Get-ControllerConfig
        $null = Wait-ForControllerHttps $config 60
        Wait-Until {
            $current = Get-ControllerProcesses
            return $current.postgres.Count -eq 1 -and $current.http.Count -eq 1 -and
                $current.scheduler.Count -eq 1
        } 20 "controller_reconfigured_processes_not_running"
        $schedulerExited = Get-ProcessExitTime $oldSchedulerTracked $schedulerExitHandle `
            "controller_old_scheduler_not_stopped_during_reconfiguration"
        $httpExited = Get-ProcessExitTime $oldHttpTracked $httpExitHandle `
            "controller_old_http_not_stopped_during_reconfiguration"
        $newProcesses = Assert-ControllerProcesses
        $newSchedulerStart = ([DateTime]$newProcesses.scheduler[0].CreationDate).ToUniversalTime()
        $newHttpStart = ([DateTime]$newProcesses.http[0].CreationDate).ToUniversalTime()
        $rootAfterEndpointChange = Get-FileSha256 $rootCertificatePath
        $leafAfterEndpointChange = Get-FileSha256 $leafCertificatePath
        if ($rootAfterEndpointChange -cne $rootBeforeEndpointChange -or
            $leafAfterEndpointChange -ceq $leafBeforeEndpointChange -or
            $schedulerExited -ge $httpExited -or $newSchedulerStart -lt $newHttpStart -or
            @(Get-ListenerAddresses $oldHttpsPort).Count -ne 0 -or
            (Get-ListenerAddresses $newHttpsPort).Count -ne 1 -or
            -not (Test-PlaintextHttpRejected $config)) {
            throw "controller_running_endpoint_transition_invalid"
        }
        $postCutoverRuntimeIdentity = Add-ControllerRuntimeIdentitySnapshot `
            "after_endpoint_cutover" $newProcesses $config
        Assert-ControllerRuntimeIdentityCutover $initialRuntimeIdentity $postCutoverRuntimeIdentity
        $checks.endpoint_running_transition_preserves_postgres = $true
        $checks.plaintext_health_rejected = $true
        Ensure-ControllerOwner $desktop.Id "cutover_relogin" `
            -ForceReauthentication -AfterEndpointReconfiguration
    } finally {
        $null = [ThreadsControllerSmoke.NativeMethods]::CloseHandle($schedulerExitHandle)
        $null = [ThreadsControllerSmoke.NativeMethods]::CloseHandle($httpExitHandle)
        $oldHttpTracked.Dispose()
        $oldSchedulerTracked.Dispose()
    }

    $window = Get-Process -Id $desktop.Id -ErrorAction Stop
    $window.Refresh()
    if (-not [ThreadsControllerSmoke.NativeMethods]::PostMessage(
        $window.MainWindowHandle, 0x0010, [IntPtr]::Zero, [IntPtr]::Zero
    )) { throw "controller_window_close_message_failed" }
    Wait-Until { -not [ThreadsControllerSmoke.NativeMethods]::IsWindowVisible($window.MainWindowHandle) } `
        10 "controller_x_did_not_hide_window"
    Wait-Until {
        $current = Get-ControllerProcesses
        return $current.postgres.Count -eq 1 -and $current.http.Count -eq 1 -and
            $current.scheduler.Count -eq 1
    } 15 "controller_runtime_stopped_when_window_hidden"
    $null = Wait-ForControllerHttps $config 5
    $hiddenProcesses = Assert-ControllerProcesses
    $windowHideRuntimeIdentity = Add-ControllerRuntimeIdentitySnapshot `
        "after_window_hide" $hiddenProcesses $config
    Assert-ControllerRuntimeIdentityContinuity $postCutoverRuntimeIdentity `
        $windowHideRuntimeIdentity "controller_window_hide_changed_runtime_identity"
    $null = Set-OperatorSessionMode $desktop.Id $config "signed_out" `
        "window_hide_lock" "controller_owner_session_not_revoked_on_lock"
    Assert-DatabaseValue $config $sentinel
    $checks.x_hides_and_runtime_continues = $true

    $second = Start-Process -FilePath $DesktopExecutable -PassThru
    $desktopPids.Add([int]$second.Id)
    Wait-Until { $second.HasExited } 20 "second_desktop_launch_did_not_converge"
    Wait-Until { [ThreadsControllerSmoke.NativeMethods]::IsWindowVisible($window.MainWindowHandle) } `
        10 "controller_reopen_did_not_restore_window"
    Wait-Until {
        $window = Get-Window $desktop.Id
        return $null -ne (Find-TextContaining $window "Session locked") -and
            $null -ne (Find-Element $window "Sign in" ([System.Windows.Automation.ControlType]::Button))
    } 15 "controller_reopen_did_not_require_operator_sign_in"
    $null = Set-OperatorSessionMode $desktop.Id (Get-ControllerConfig) "signed_out" `
        "reopen_before_login" "controller_reopen_owner_session_not_locked"
    $checks.reopen_requires_operator_sign_in = $true
    Ensure-ControllerOwner $desktop.Id "reopen_after_login" -ForceReauthentication
    $checks.owner_reauthenticated_after_reopen = $true
    $owned = Assert-ControllerProcesses
    $trayReopenRuntimeIdentity = Add-ControllerRuntimeIdentitySnapshot `
        "after_tray_reopen" $owned (Get-ControllerConfig)
    Assert-ControllerRuntimeIdentityContinuity $postCutoverRuntimeIdentity `
        $trayReopenRuntimeIdentity "controller_reopen_changed_runtime_identity"
    $checks.reopen_keeps_one_runtime_and_database_identity = $true

    $shutdownProcesses = @{
        scheduler = Get-TrackedProcess ([int]$trayReopenRuntimeIdentity.scheduler_pid) $runtimeExecutable
        http = Get-TrackedProcess ([int]$trayReopenRuntimeIdentity.http_pid) $runtimeExecutable
        postgres = Get-TrackedProcess ([int]$trayReopenRuntimeIdentity.postgres_pid) `
            (Join-Path $postgresBin "postgres.exe")
    }
    if ($shutdownProcesses.Values -contains $null) {
        foreach ($process in $shutdownProcesses.Values) { if ($process) { $process.Dispose() } }
        throw "controller_shutdown_process_handle_unavailable"
    }
    $shutdownProcessHandles = [ordered]@{}
    try {
        foreach ($name in @("scheduler", "http", "postgres")) {
            $process = $shutdownProcesses[$name]
            $nativeHandle = [ThreadsControllerSmoke.NativeMethods]::OpenProcessForExitTime(
                [uint32]$process.Id
            )
            if ($nativeHandle -eq [IntPtr]::Zero) {
                throw "controller_shutdown_process_handle_unavailable"
            }
            $shutdownProcessHandles[$name] = $nativeHandle
        }
        Quit-Desktop $desktop.Id "graceful_quit_authorization_login"
        $schedulerExit = Get-ProcessExitTime `
            $shutdownProcesses.scheduler $shutdownProcessHandles["scheduler"] `
            "controller_scheduler_did_not_stop_first"
        $httpExit = Get-ProcessExitTime `
            $shutdownProcesses.http $shutdownProcessHandles["http"] `
            "controller_http_did_not_stop_after_scheduler"
        $postgresExit = Get-ProcessExitTime `
            $shutdownProcesses.postgres $shutdownProcessHandles["postgres"] `
            "controller_database_did_not_stop_after_http"
        if (-not ($schedulerExit -lt $httpExit -and $httpExit -lt $postgresExit)) {
            throw "controller_shutdown_order_invalid"
        }
        $shutdownExitOrder = @(
            [ordered]@{
                process = "scheduler"; exited_utc = $schedulerExit.ToString("o")
            },
            [ordered]@{ process = "http"; exited_utc = $httpExit.ToString("o") },
            [ordered]@{
                process = "postgres"; exited_utc = $postgresExit.ToString("o")
            }
        )
    } finally {
        foreach ($nativeHandle in $shutdownProcessHandles.Values) {
            $null = [ThreadsControllerSmoke.NativeMethods]::CloseHandle($nativeHandle)
        }
        foreach ($process in $shutdownProcesses.Values) { $process.Dispose() }
    }
    Wait-Until { (Get-ControllerProcesses).all.Count -eq 0 } 15 "controller_quit_left_runtime_processes"
    $checks.serving_leaf_key_cleaned_on_shutdown = -not (Test-Path -LiteralPath $servingKeyPath)
    if (-not $checks.serving_leaf_key_cleaned_on_shutdown) {
        throw "controller_serving_leaf_key_not_cleaned_on_shutdown"
    }
    $checks.graceful_quit_stops_scheduler_http_then_postgres = $true
    }

    if ($Scenario -eq "restart_renewal") {
    Quit-Desktop $desktop.Id "restart_renewal_quit_login"
    Wait-Until { (Get-ControllerProcesses).all.Count -eq 0 } 15 "controller_quit_left_runtime_processes"
    $checks.graceful_quit_stops_scheduler_http_then_postgres = $true
    $rootFingerprintBeforeRestart =
        (Get-FileHash -LiteralPath $rootCertificatePath -Algorithm SHA256).Hash.ToLowerInvariant()
    $leafCertificatePath = Join-Path (Join-Path $controllerRoot "tls") "leaf-cert.der"
    $leafFingerprintBeforeRenewal = (Get-FileHash -LiteralPath $leafCertificatePath -Algorithm SHA256).Hash
    $checks.serving_leaf_key_cleaned_on_shutdown = -not (Test-Path -LiteralPath $servingKeyPath)
    if (-not $checks.serving_leaf_key_cleaned_on_shutdown) {
        throw "controller_serving_leaf_key_not_cleaned_on_shutdown"
    }
    [System.IO.File]::WriteAllText(
        $servingKeyPath,
        "STALE SERVING KEY MUST BE REMOVED BEFORE STARTUP",
        [System.Text.UTF8Encoding]::new($false)
    )
    Set-ExpiringControllerLeaf $config

    $desktop = Start-ExistingController
    $config = Get-ControllerConfig
    $null = Wait-ForControllerHttps $config 60
    $null = Set-OperatorSessionMode $desktop.Id $config "signed_out" `
        "after_quit_relaunch" "controller_owner_session_exists_after_quit_relaunch"
    $rootFingerprintAfterRestart =
        (Get-FileHash -LiteralPath $rootCertificatePath -Algorithm SHA256).Hash.ToLowerInvariant()
    $renewedLeaf = [System.Security.Cryptography.X509Certificates.X509Certificate2]::new(
        [System.IO.File]::ReadAllBytes($leafCertificatePath)
    )
    $renewedLeafRemaining = $renewedLeaf.NotAfter.ToUniversalTime() - [DateTime]::UtcNow
    $servingKeyContents = [System.IO.File]::ReadAllText($servingKeyPath)
    $checks.root_identity_persists_across_restart =
        $rootFingerprintAfterRestart -ceq $rootFingerprintBeforeRestart
    $checks.leaf_renewal_preserves_root_identity =
        $rootFingerprintAfterRestart -ceq $rootFingerprintBeforeRestart -and
        (Get-FileHash -LiteralPath $leafCertificatePath -Algorithm SHA256).Hash -cne $leafFingerprintBeforeRenewal -and
        $renewedLeafRemaining.TotalDays -gt 80
    $checks.stale_serving_leaf_key_replaced_on_startup =
        $servingKeyContents -match '^-----BEGIN PRIVATE KEY-----' -and
        $servingKeyContents -notmatch 'STALE SERVING KEY MUST BE REMOVED'
    $renewedLeaf.Dispose()
    if (-not $checks.root_identity_persists_across_restart -or
        -not $checks.leaf_renewal_preserves_root_identity -or
        -not $checks.stale_serving_leaf_key_replaced_on_startup) {
        throw "controller_tls_restart_or_renewal_evidence_invalid"
    }
    Assert-DatabaseValue $config $sentinel
    $owned = Assert-ControllerProcesses
    if ($config.controllerId -cne $controllerIdentity -or
        [string](Invoke-Psql $config "SELECT system_identifier FROM pg_control_system();") -cne $databaseSystemIdentifier) {
        throw "controller_relaunch_changed_database_identity"
    }
    $restartIdentity = Add-ControllerRuntimeIdentitySnapshot `
        "after_restart_renewal" $owned $config
    $scenarioEvidence.restart_runtime_identity = $restartIdentity
    $checks.relaunch_preserves_database_and_endpoint = $true
    }

    if ($Scenario -eq "database_crash_recovery") {
    $owned = Assert-ControllerProcesses
    $null = Set-OperatorSessionMode $desktop.Id $config "signed_in" `
        "database_crash_before_crash" "controller_scenario_fixture_owner_session_missing"
    $databaseCrashAuthBefore = Get-PostCrashOperatorAuthState $config
    $scenarioEvidence.database_crash_before = Add-ControllerRuntimeIdentitySnapshot `
        "before_database_crash" $owned $config
    $databaseCrashPid = [int]$owned.postgres[0].ProcessId
    $databaseCrashProcess = Get-TrackedProcess $databaseCrashPid (Join-Path $postgresBin "postgres.exe")
    if (-not $databaseCrashProcess) { throw "controller_database_process_unavailable_before_crash" }
    $databaseCrashProcess.Kill()
    $databaseCrashProcess.WaitForExit()
    $databaseCrashProcess.Dispose()
    Wait-ControllerState $desktop.Id "Failed" "controller_database_process_exited" 25
    Wait-Until { (Get-ControllerProcesses).http.Count -eq 0 -and (Get-ControllerProcesses).scheduler.Count -eq 0 } `
        15 "controller_dependents_survived_database_crash"
    Quit-Desktop $desktop.Id "database_crash_failure_shutdown_login"
    $desktop = Start-ExistingController
    $config = Get-ControllerConfig
    $null = Wait-ForControllerHttps $config 60
    $null = Assert-PostCrashOperatorReauthenticationRequired `
        $desktop.Id $config "database_crash_reauthentication_boundary" $databaseCrashAuthBefore
    Assert-DatabaseValue $config $sentinel
    if ([string](Invoke-Psql $config "SELECT system_identifier FROM pg_control_system();") -cne $databaseSystemIdentifier) {
        throw "controller_database_identity_changed_after_crash"
    }
    $scenarioEvidence.database_crash_after = Add-ControllerRuntimeIdentitySnapshot `
        "after_database_crash_recovery" (Assert-ControllerProcesses) $config
    $checks.database_crash_fails_closed_and_recovers_wal = $true
    }

    if ($Scenario -eq "parent_crash_recovery") {
    $rootFingerprintBeforeRestart = Get-FileSha256 $rootCertificatePath
    $beforeParentCrash = Assert-ControllerProcesses
    $null = Set-OperatorSessionMode $desktop.Id $config "signed_in" `
        "parent_crash_before_crash" "controller_scenario_fixture_owner_session_missing"
    $parentCrashAuthBefore = Get-PostCrashOperatorAuthState $config
    $scenarioEvidence.parent_crash_before = Add-ControllerRuntimeIdentitySnapshot `
        "before_parent_crash" $beforeParentCrash $config
    $runtimeRootPids = @(
        $beforeParentCrash.postgres[0].ProcessId,
        $beforeParentCrash.http[0].ProcessId,
        $beforeParentCrash.scheduler[0].ProcessId
    ) | ForEach-Object { [int]$_ }
    $ownedTreeRecords = @(
        $beforeParentCrash.postgres[0],
        $beforeParentCrash.http[0],
        $beforeParentCrash.scheduler[0]
    ) + @(Get-ProcessDescendants $runtimeRootPids)
    $parentCrashProcesses = @($ownedTreeRecords | ForEach-Object {
        Get-TrackedProcess ([int]$_.ProcessId) ([string]$_.ExecutablePath)
    } | Where-Object { $null -ne $_ })
    $parentCrashPids = @($parentCrashProcesses | ForEach-Object { [int]$_.Id })
    if (@($runtimeRootPids | Where-Object { $_ -notin $parentCrashPids }).Count -gt 0) {
        throw "controller_process_tree_capture_incomplete"
    }
    $desktop.Kill()
    $desktop.WaitForExit()
    $desktop.Dispose()
    Wait-Until {
        $current = Get-ControllerProcesses
        $survivingCapturedProcesses = @($parentCrashProcesses | Where-Object {
            try {
                $_.Refresh()
                -not $_.HasExited
            } catch { $false }
        })
        return $current.all.Count -eq 0 -and $survivingCapturedProcesses.Count -eq 0
    } 25 "controller_job_object_left_runtime_after_parent_crash"
    $desktop = Start-ExistingController
    $config = Get-ControllerConfig
    $null = Wait-ForControllerHttps $config 60
    $null = Assert-PostCrashOperatorReauthenticationRequired `
        $desktop.Id $config "parent_crash_reauthentication_boundary" $parentCrashAuthBefore
    Assert-DatabaseValue $config $sentinel
    $owned = Assert-ControllerProcesses
    $rootFingerprintAfterParentCrash =
        (Get-FileHash -LiteralPath $rootCertificatePath -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($owned.scheduler.Count -ne 1 -or
        [string](Invoke-Psql $config "SELECT system_identifier FROM pg_control_system();") -cne $databaseSystemIdentifier -or
        $rootFingerprintAfterParentCrash -cne $rootFingerprintBeforeRestart) {
        throw "controller_scheduler_or_database_duplicated_after_parent_crash"
    }
    $scenarioEvidence.parent_crash_after = Add-ControllerRuntimeIdentitySnapshot `
        "after_parent_crash_recovery" $owned $config
    $checks.controller_root_identity_survives_crash_restart = $true
    $checks.desktop_parent_crash_owns_process_tree_and_recovers_wal = $true
    }

    if ($Scenario -eq "migration_recovery_auth") {
    $null = Set-OperatorSessionMode $desktop.Id $config "signed_in" `
        "migration_recovery_before_mutation" "controller_scenario_fixture_owner_session_missing"
    $scenarioEvidence.migration_recovery_before = Add-ControllerRuntimeIdentitySnapshot `
        "before_migration_failure" (Assert-ControllerProcesses) $config
    $currentMigration = Invoke-Psql $config "SELECT version_num FROM alembic_version;"
    Invoke-Psql $config "UPDATE alembic_version SET version_num = 'dx04_missing_revision';" | Out-Null
    Quit-Desktop $desktop.Id "migration_recovery_fixture_quit_login"
    $desktop = Start-Desktop 260
    Wait-ControllerState $desktop.Id "Failed" "controller_migration_failed" 150
    $config = Get-ControllerConfig
    Assert-DatabaseValue $config $sentinel
    if ([string](Invoke-Psql $config "SELECT system_identifier FROM pg_control_system();") -cne $databaseSystemIdentifier) {
        throw "controller_migration_failure_reinitialized_database"
    }
    Invoke-Psql $config "UPDATE alembic_version SET version_num = '$currentMigration';" | Out-Null
    Quit-Desktop $desktop.Id "failed_migration_failure_shutdown_login"
    $desktop = Start-ExistingController
    $config = Get-ControllerConfig
    $null = Wait-ForControllerHttps $config 60
    $null = Set-OperatorSessionMode $desktop.Id $config "signed_out" `
        "after_failed_migration_recovery" "controller_owner_session_exists_after_failed_migration_recovery"
    Assert-DatabaseValue $config $sentinel
    $scenarioEvidence.migration_recovery_after = Add-ControllerRuntimeIdentitySnapshot `
        "after_failed_migration_recovery" (Assert-ControllerProcesses) $config
    $checks.failed_migration_preserves_existing_cluster = $true

    Quit-Desktop $desktop.Id "failed_migration_recovery_quit_login"
    $checks.graceful_quit_stops_scheduler_http_then_postgres = $true
    }

    if ($Scenario -eq "database_port_collision") {
    Quit-Desktop $desktop.Id "database_port_collision_fixture_quit_login"
    Wait-Until { (Get-ControllerProcesses).all.Count -eq 0 } 15 "controller_quit_left_runtime_processes"
    $config = Get-ControllerConfig
    $expectedDatabasePort = [int]$config.databasePort
    $databaseConfigHash = Get-FileSha256 (Join-Path $controllerRoot "controller.json")
    $databaseRootHash = Get-FileSha256 $rootCertificatePath
    $databaseLeafHash = Get-FileSha256 $leafCertificatePath
    $databaseReservation = [System.Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, [int]$config.databasePort)
    $databaseReservation.Start()
    try {
        $desktop = Start-Desktop
        $databaseDiagnosticWaitCompleted = $false
        try {
            Wait-ControllerState $desktop.Id "Failed" "controller_database_port_in_use" 40
            $databaseDiagnosticWaitCompleted = $true
        } catch { }
        $databaseObservedConfig = Get-ControllerConfig
        $databaseProcesses = Get-ControllerProcesses
        $databaseCollisionEvidence = [ordered]@{
            expected_database_port = $expectedDatabasePort
            observed_persisted_database_port = [int]$databaseObservedConfig.databasePort
            expected_diagnostic_code = "controller_database_port_in_use"
            observed_diagnostic_code = Get-ControllerDiagnosticCode $desktop.Id
            diagnostic_wait_completed = $databaseDiagnosticWaitCompleted
            postgres_count = @($databaseProcesses.postgres_tree).Count
            postgres_pids = @($databaseProcesses.postgres_tree | ForEach-Object { [int]$_.ProcessId })
            http_count = @($databaseProcesses.http).Count
            http_pids = @($databaseProcesses.http | ForEach-Object { [int]$_.ProcessId })
            scheduler_count = @($databaseProcesses.scheduler).Count
            scheduler_pids = @($databaseProcesses.scheduler | ForEach-Object { [int]$_.ProcessId })
            config_sha256_unchanged = (Get-FileSha256 (Join-Path $controllerRoot "controller.json")) -ceq $databaseConfigHash
            root_sha256_unchanged = (Get-FileSha256 $rootCertificatePath) -ceq $databaseRootHash
            leaf_sha256_unchanged = (Get-FileSha256 $leafCertificatePath) -ceq $databaseLeafHash
            database_system_identifier_unchanged =
                (Get-PostgresSystemIdentifierFromControlFile) -ceq $databaseSystemIdentifier
        }
        $scenarioEvidence.database_port_collision = $databaseCollisionEvidence
        if ($databaseCollisionEvidence.observed_persisted_database_port -ne $expectedDatabasePort) {
            throw "controller_database_port_collision_changed_persisted_port"
        }
        if ($databaseCollisionEvidence.postgres_count -ne 0 -or
            $databaseCollisionEvidence.http_count -ne 0 -or
            $databaseCollisionEvidence.scheduler_count -ne 0) {
            throw "controller_database_port_collision_left_runtime_processes"
        }
        if (-not $databaseDiagnosticWaitCompleted -or
            $databaseCollisionEvidence.observed_diagnostic_code -cne "controller_database_port_in_use") {
            throw "controller_database_port_collision_diagnostic_not_confirmed"
        }
        if (-not $databaseCollisionEvidence.config_sha256_unchanged -or
            -not $databaseCollisionEvidence.root_sha256_unchanged -or
            -not $databaseCollisionEvidence.leaf_sha256_unchanged -or
            -not $databaseCollisionEvidence.database_system_identifier_unchanged) {
            throw "controller_database_port_collision_changed_identity"
        }
        $checks.database_port_collision_does_not_rotate = $true
        Quit-Desktop $desktop.Id "database_port_collision_shutdown_login"
    } finally {
        $databaseReservation.Stop()
    }
    }

    if ($Scenario -eq "endpoint_port_collision") {
    Quit-Desktop $desktop.Id "endpoint_port_collision_fixture_quit_login"
    Wait-Until { (Get-ControllerProcesses).all.Count -eq 0 } 15 "controller_quit_left_runtime_processes"
    $config = Get-ControllerConfig
    $endpointReservation = [System.Net.Sockets.TcpListener]::new([Net.IPAddress]::Any, [int]$config.endpointPort)
    $endpointReservation.Start()
    try {
        $expectedCollisionPort = [int]$config.endpointPort
        $expectedCollisionDiagnostic = "controller_endpoint_port_in_use"
        $endpointConfigHash = Get-FileSha256 (Join-Path $controllerRoot "controller.json")
        $endpointRootHash = Get-FileSha256 $rootCertificatePath
        $endpointLeafHash = Get-FileSha256 $leafCertificatePath
        $desktop = Start-Desktop
        $collisionDiagnosticWaitCompleted = $false
        try {
            Wait-ControllerState $desktop.Id "Failed" $expectedCollisionDiagnostic 40
            $collisionDiagnosticWaitCompleted = $true
        } catch {
            # Preserve the observed endpoint/process state so the artifact identifies the failed invariant.
        }
        $collisionObservedConfig = Get-ControllerConfig
        $collisionProcesses = Get-ControllerProcesses
        $endpointCollisionEvidence = [ordered]@{
            expected_endpoint_port = $expectedCollisionPort
            observed_persisted_endpoint_port = [int]$collisionObservedConfig.endpointPort
            postgres_count = @($collisionProcesses.postgres_tree).Count
            postgres_pids = @($collisionProcesses.postgres_tree | ForEach-Object {
                [int]$_.ProcessId
            })
            http_count = @($collisionProcesses.http).Count
            http_pids = @($collisionProcesses.http | ForEach-Object { [int]$_.ProcessId })
            scheduler_count = @($collisionProcesses.scheduler).Count
            scheduler_pids = @($collisionProcesses.scheduler | ForEach-Object {
                [int]$_.ProcessId
            })
            expected_diagnostic_code = $expectedCollisionDiagnostic
            observed_diagnostic_code = (Get-ControllerDiagnosticCode $desktop.Id)
            diagnostic_wait_completed = $collisionDiagnosticWaitCompleted
            config_sha256_unchanged =
                (Get-FileSha256 (Join-Path $controllerRoot "controller.json")) -ceq $endpointConfigHash
            root_sha256_unchanged = (Get-FileSha256 $rootCertificatePath) -ceq $endpointRootHash
            leaf_sha256_unchanged = (Get-FileSha256 $leafCertificatePath) -ceq $endpointLeafHash
            database_system_identifier_unchanged =
                (Get-PostgresSystemIdentifierFromControlFile) -ceq $databaseSystemIdentifier
        }
        $scenarioEvidence.endpoint_port_collision = $endpointCollisionEvidence
        $collisionFailure = Get-EndpointCollisionFailureCode $endpointCollisionEvidence
        if ($collisionFailure) { throw $collisionFailure }
        if (-not $endpointCollisionEvidence.config_sha256_unchanged -or
            -not $endpointCollisionEvidence.root_sha256_unchanged -or
            -not $endpointCollisionEvidence.leaf_sha256_unchanged -or
            -not $endpointCollisionEvidence.database_system_identifier_unchanged) {
            throw "controller_endpoint_collision_changed_tls_or_cluster_identity"
        }
        $checks.endpoint_port_collision_does_not_rotate = $true
        Quit-Desktop $desktop.Id "endpoint_port_collision_shutdown_login"
    } finally {
        $endpointReservation.Stop()
    }
    }

    if ($Scenario -eq "unowned_root") {
    Quit-Desktop $desktop.Id "unowned_root_fixture_quit_login"
    Wait-Until { (Get-ControllerProcesses).all.Count -eq 0 } 15 "controller_quit_left_runtime_processes"
    Move-Item -LiteralPath $controllerRoot -Destination $savedRoot
    $rootWasMoved = $true
    New-Item -ItemType Directory -Path $controllerRoot | Out-Null
    $foreignMarker = Join-Path $controllerRoot "foreign-data.marker"
    [System.IO.File]::WriteAllText($foreignMarker, "keep")
    $desktop = Start-Desktop
    Wait-ControllerState $desktop.Id "Failed" "controller_data_root_unowned" 40
    if ((Get-Content -LiteralPath $foreignMarker -Raw) -cne "keep") {
        throw "controller_unowned_root_was_modified"
    }
    $checks.unowned_root_is_preserved_and_rejected = $true
    Quit-Desktop $desktop.Id "unowned_root_shutdown_login"
    Move-Item -LiteralPath $controllerRoot -Destination $unownedRoot
    Move-Item -LiteralPath $savedRoot -Destination $controllerRoot
    $rootWasMoved = $false
    }

    if ($Scenario -eq "unwritable_root") {
    Quit-Desktop $desktop.Id "unwritable_root_fixture_quit_login"
    Wait-Until { (Get-ControllerProcesses).all.Count -eq 0 } 15 "controller_quit_left_runtime_processes"
    $aclBeforeDeny = Get-Acl -LiteralPath $controllerRoot
    $aclWithDeny = Get-Acl -LiteralPath $controllerRoot
    $denyWriteRule = [System.Security.AccessControl.FileSystemAccessRule]::new(
        [Security.Principal.SecurityIdentifier]::new($currentSid),
        [System.Security.AccessControl.FileSystemRights]::WriteData,
        [System.Security.AccessControl.InheritanceFlags]::ContainerInherit -bor
            [System.Security.AccessControl.InheritanceFlags]::ObjectInherit,
        [System.Security.AccessControl.PropagationFlags]::None,
        [System.Security.AccessControl.AccessControlType]::Deny
    )
    $aclWithDeny.AddAccessRule($denyWriteRule)
    Set-Acl -LiteralPath $controllerRoot -AclObject $aclWithDeny
    $writeDenied = $false
    try {
        $writeProbe = [System.IO.File]::Open(
            (Join-Path $controllerRoot "controller.json"),
            [System.IO.FileMode]::Open,
            [System.IO.FileAccess]::Write,
            [System.IO.FileShare]::None
        )
        $writeProbe.Dispose()
    } catch [System.UnauthorizedAccessException] {
        $writeDenied = $true
    }
    if (-not $writeDenied) { throw "controller_unwritable_root_acl_not_enforced" }
    $desktop = Start-Desktop
    Wait-ControllerState $desktop.Id "Failed" "controller_data_root_access_denied" 40
    $checks.unwritable_root_is_rejected = $true
    Quit-Desktop $desktop.Id "unwritable_root_shutdown_login"
    Set-Acl -LiteralPath $controllerRoot -AclObject $aclBeforeDeny
    $aclBeforeDeny = $null
    }

    if ($Scenario -eq "corrupt_cluster") {
    Quit-Desktop $desktop.Id "corrupt_cluster_fixture_quit_login"
    Wait-Until { (Get-ControllerProcesses).all.Count -eq 0 } 15 "controller_quit_left_runtime_processes"
    $pgVersionPath = Join-Path $controllerRoot "postgresql\PG_VERSION"
    $pgVersionBackup = Get-Content -LiteralPath $pgVersionPath -Raw
    [System.IO.File]::WriteAllText($pgVersionPath, "corrupt")
    $desktop = Start-Desktop
    Wait-ControllerState $desktop.Id "Failed" "controller_database_cluster_invalid" 40
    if ((Get-Content -LiteralPath $pgVersionPath -Raw) -cne "corrupt" -or
        (Get-ControllerProcesses).all.Count -ne 0) {
        throw "controller_corrupt_cluster_was_modified_or_started"
    }
    $checks.corrupt_cluster_is_preserved_and_rejected = $true
    Quit-Desktop $desktop.Id "corrupt_cluster_shutdown_login"
    [System.IO.File]::WriteAllText($pgVersionPath, $pgVersionBackup)
    }

    $finalProcesses = Get-ControllerProcesses
    $processEvidence.initial_postgres_pid = [int]$initialPostgresPid
    $processEvidence.initial_http_pid = [int]$initialHttpPid
    $processEvidence.initial_scheduler_pid = [int]$initialSchedulerPid
    if ($Scenario -eq "bootstrap_https_cutover_tray") {
        $processEvidence.post_cutover_postgres_pid = [int]$postCutoverRuntimeIdentity.postgres_pid
        $processEvidence.post_cutover_http_pid = [int]$postCutoverRuntimeIdentity.http_pid
        $processEvidence.post_cutover_scheduler_pid = [int]$postCutoverRuntimeIdentity.scheduler_pid
        $processEvidence.tray_reopen_postgres_pid = [int]$trayReopenRuntimeIdentity.postgres_pid
        $processEvidence.tray_reopen_http_pid = [int]$trayReopenRuntimeIdentity.http_pid
        $processEvidence.tray_reopen_scheduler_pid = [int]$trayReopenRuntimeIdentity.scheduler_pid
        $processEvidence.graceful_quit_exit_order = $shutdownExitOrder
    }
    if ($Scenario -eq "parent_crash_recovery") {
        $processEvidence.parent_crash_runtime_pids = @($parentCrashPids)
    }
    $processEvidence.controller_id = $controllerIdentity
    $processEvidence.database_system_identifier = $databaseSystemIdentifier
    $processEvidence.persisted_database_port = if ($config) { [int]$config.databasePort } else { $null }
    $processEvidence.persisted_endpoint_port = if ($config) { [int]$config.endpointPort } else { $null }
    $processEvidence.final_runtime_counts = [ordered]@{
        postgres = @($finalProcesses.postgres).Count
        http = @($finalProcesses.http).Count
        scheduler = @($finalProcesses.scheduler).Count
    }
    $scenarioEvidence.controller_id = $controllerIdentity
    $scenarioEvidence.database_system_identifier = $databaseSystemIdentifier
    $scenarioEvidence.persisted_database_port = if ($config) { [int]$config.databasePort } else { $null }
    $scenarioEvidence.persisted_endpoint_port = if ($config) { [int]$config.endpointPort } else { $null }
    $scenarioEvidence.sentinel_readable = if ($config) {
        try { Assert-DatabaseValue $config $sentinel; $true } catch { $false }
    } else { $false }
    $missingScenarioChecks = @($scenarioCheckNames | Where-Object { -not [bool]$checks[$_] })
    if ($missingScenarioChecks.Count -gt 0) {
        Add-Failure "controller_scenario_required_check_failed"
        $scenarioEvidence.missing_checks = @($missingScenarioChecks)
    }
    $result = if ($failureCodes.Count -eq 0) {
        "PASS"
    } else { "BLOCKER" }
} catch {
    $failureScriptLine = [int]$_.InvocationInfo.ScriptLineNumber
    Add-Failure ([string]$_.Exception.Message)
} finally {
    try {
        foreach ($process in @(Get-DesktopProcesses)) {
            if ([int]$process.ProcessId -in $desktopPids) {
                $tracked = Get-TrackedProcess ([int]$process.ProcessId) $DesktopExecutable
                if ($tracked) {
                    try {
                        $tracked.Kill()
                        $tracked.WaitForExit(5000) | Out-Null
                    } catch { }
                    $tracked.Dispose()
                }
            }
        }
        foreach ($process in $parentCrashProcesses) {
            try {
                $process.Refresh()
                if (-not $process.HasExited) { $process.Kill() }
            } catch { }
            $process.Dispose()
        }
        foreach ($process in (Get-ControllerProcesses).all) {
            $tracked = Get-TrackedProcess ([int]$process.ProcessId) ([string]$process.ExecutablePath)
            if ($tracked) {
                try {
                    $tracked.Kill()
                    $tracked.WaitForExit(5000) | Out-Null
                } catch { }
                $tracked.Dispose()
            }
        }
        if ($rootWasMoved -and (Test-Path -LiteralPath $savedRoot)) {
            if (Test-Path -LiteralPath $controllerRoot) {
                $recoveryRoot = "$unownedRoot-recovery"
                if (Test-Path -LiteralPath $recoveryRoot) {
                    throw "controller_smoke_cleanup_root_recovery_conflict"
                }
                Move-Item -LiteralPath $controllerRoot -Destination $recoveryRoot
            }
            Move-Item -LiteralPath $savedRoot -Destination $controllerRoot
        }
        if ($aclBeforeDeny -and (Test-Path -LiteralPath $controllerRoot)) {
            Set-Acl -LiteralPath $controllerRoot -AclObject $aclBeforeDeny
        }
        if ($pgVersionBackup -and (Test-Path -LiteralPath (Join-Path $controllerRoot "postgresql\PG_VERSION"))) {
            [System.IO.File]::WriteAllText(
                (Join-Path $controllerRoot "postgresql\PG_VERSION"), $pgVersionBackup
            )
        }
        if (Test-Path -LiteralPath $applicationRunKey) {
            $values = Get-ItemProperty -LiteralPath $applicationRunKey
            foreach ($property in @($values.PSObject.Properties | Where-Object {
                $_.Name -notlike "PS*" -and ([string]$_.Value).IndexOf(
                    $DesktopExecutable, [System.StringComparison]::OrdinalIgnoreCase
                ) -ge 0
            })) {
                Remove-ItemProperty -LiteralPath $applicationRunKey -Name $property.Name -ErrorAction SilentlyContinue
            }
        }
        foreach ($entry in $originalRunEntries.GetEnumerator()) {
            Set-ItemProperty -LiteralPath $applicationRunKey -Name $entry.Key -Value $entry.Value
        }
    } catch {
        Add-Failure "controller_smoke_cleanup_failed"
        $result = "BLOCKER"
    }
    $env:THREADS_DESKTOP_RUNTIME_DIR = $previousRuntimeDirectory
    $env:PGPASSWORD = $previousPsqlPassword
    $evidenceDirectory = Split-Path -Parent $EvidencePath
    New-Item -ItemType Directory -Path $evidenceDirectory -Force | Out-Null
    $scenarioCheckEvidence = [ordered]@{}
    foreach ($checkName in $scenarioCheckNames) {
        $scenarioCheckEvidence[[string]$checkName] = [bool]$checks[[string]$checkName]
    }
    $evidence = [ordered]@{
        schema_version = 2
        source_revision = $ExpectedSourceRevision
        scenario = $Scenario
        runner = [ordered]@{
            github_hosted = $env:GITHUB_ACTIONS -eq "true" -and $env:RUNNER_ENVIRONMENT -eq "github-hosted"
            windows_x64 = [Environment]::Is64BitOperatingSystem -and $env:RUNNER_OS -eq "Windows" -and
                $env:RUNNER_ARCH -eq "X64"
            non_administrator = -not [Security.Principal.WindowsPrincipal]::new(
                [Security.Principal.WindowsIdentity]::GetCurrent()
            ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
            clean_profile = [bool]$checks.clean_profile
        }
        checked_head = $head
        source_worktree_clean = $worktreeIsClean
        windows_version = [Environment]::OSVersion.Version.ToString()
        windows_x64 = [Environment]::Is64BitOperatingSystem
        current_user_is_administrator = [Security.Principal.WindowsPrincipal]::new(
            [Security.Principal.WindowsIdentity]::GetCurrent()
        ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
        data_root = "CurrentUserLocalAppData"
        runtime_layout = "shared"
        checks = $scenarioCheckEvidence
        process_evidence = $processEvidence
        scenario_evidence = $scenarioEvidence
        operator_session_timeline = @($operatorSessionTimeline)
        operator_login_attempt_timeline = @($operatorLoginAttemptTimeline)
        input_mutation_evidence = @($inputMutationEvidence)
        controller_runtime_identity_timeline = @($controllerRuntimeIdentityTimeline)
        endpoint_collision_evidence = $endpointCollisionEvidence
        result = $result
        failure_code = $failureCode
        failure_codes = @($failureCodes)
        failure_script_line = $failureScriptLine
    }
    [System.IO.File]::WriteAllText(
        $EvidencePath,
        ($evidence | ConvertTo-Json -Depth 8),
        [System.Text.UTF8Encoding]::new($false)
    )
}

if ($result -ne "PASS") { throw "DX-04 Windows Controller lifecycle smoke blocked: $failureCode" }
Write-Output "DX-04 Windows Controller lifecycle smoke PASS for $ExpectedSourceRevision"
