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
    [Parameter()]
    [string]$VerifiedSourceRevision,
    [Parameter()]
    [switch]$VerifiedWorktreeClean
)

$ErrorActionPreference = "Stop"
$PSNativeCommandUseErrorActionPreference = $false
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
    endpoint_running_collision_rolls_back = $false
    endpoint_running_transition_preserves_postgres = $false
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

function Set-LoginInput([int]$ProcessId, [string]$Name, [string]$Value) {
    $fieldId = [regex]::Replace($Name.Trim().ToLowerInvariant(), "[^a-z0-9]+", "_").Trim("_")
    if ([string]::IsNullOrWhiteSpace($fieldId)) { $fieldId = "unknown" }
    $inputUnavailableCode = "desktop_input_unavailable_$fieldId"
    $resolvedInput = [pscustomobject]@{ Control = $null }
    Wait-Until {
        $candidate = Find-Element (Get-Window $ProcessId) $Name `
            ([System.Windows.Automation.ControlType]::Edit)
        if (-not $candidate) { return $false }
        $resolvedInput.Control = $candidate
        return $true
    } 20 $inputUnavailableCode

    $inputControl = $resolvedInput.Control
    $inputControl.SetFocus()
    [System.Windows.Forms.SendKeys]::SendWait("^a")
    [System.Windows.Forms.SendKeys]::SendWait($Value)
    if ($fieldId -ne "password") {
        Wait-Until {
            $current = Find-Element (Get-Window $ProcessId) $Name `
                ([System.Windows.Automation.ControlType]::Edit)
            if (-not $current) { return $false }
            try {
                $valuePattern = $current.GetCurrentPattern(
                    [System.Windows.Automation.ValuePattern]::Pattern
                )
                return $valuePattern.Current.Value -ceq $Value
            } catch { return $false }
        } 5 "desktop_input_value_not_populated_$fieldId"
    }
}

function Get-ActiveOwnerSessionCount([object]$Config) {
    $username = $script:smokeOwnerUsername
    $count = Invoke-Psql $Config `
        "SELECT COUNT(*) FROM public.operator_sessions s JOIN public.operator_users u ON u.id = s.operator_user_id WHERE u.username = '$username' AND s.revoked_at IS NULL AND s.expires_at > now();"
    if ($count -notmatch '^\d+$') { throw "controller_operator_session_count_invalid" }
    return [int]$count
}

function Assert-OneActiveOwnerSession([object]$Config) {
    $activeSessions = Get-ActiveOwnerSessionCount $Config
    $processEvidence.operator_session_preflight = [ordered]@{
        active_sessions = [string]$activeSessions
    }
    if ($activeSessions -ne 1) {
        throw "controller_active_owner_session_count_not_one_$activeSessions"
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

function Ensure-ControllerOwner([int]$ProcessId, [switch]$ForceReauthentication) {
    $config = Get-ControllerConfig
    if ($ForceReauthentication) {
        Wait-Until { (Get-ActiveOwnerSessionCount $config) -eq 0 } 15 `
            "controller_owner_session_not_revoked_on_lock"
    } else {
        $activeSessions = Get-ActiveOwnerSessionCount $config
        if ($activeSessions -eq 1) { return }
        if ($activeSessions -ne 0) {
            throw "controller_active_owner_session_count_not_one"
        }
    }
    Wait-Until {
        $window = Get-Window $ProcessId
        return $null -ne (Find-Element $window "Username" ([System.Windows.Automation.ControlType]::Edit)) -and
            $null -ne (Find-Element $window "Password" ([System.Windows.Automation.ControlType]::Edit)) -and
            $null -ne (Find-Element $window "Sign in" ([System.Windows.Automation.ControlType]::Button))
    } 15 "controller_operator_reauthentication_not_ready"
    Set-LoginInput $ProcessId "Username" $script:smokeOwnerUsername
    Set-LoginInput $ProcessId "Password" $script:smokeOwnerPassword
    Invoke-Button $ProcessId "Sign in"
    Wait-Until { (Get-ActiveOwnerSessionCount $config) -eq 1 } 30 "controller_owner_login_failed"
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

function Wait-ForDesktopParentExit([int]$ProcessId) {
    Wait-Until {
        $desktopProcess = Get-ProcessIfPresent $ProcessId
        if (-not $desktopProcess) { return $true }
        $desktopProcess.Dispose()
        return $false
    } 55 "desktop_graceful_quit_timeout"
}

function Quit-Desktop([int]$ProcessId) {
    $owned = Get-ControllerProcesses
    if ($owned.postgres.Count -eq 1 -and $owned.http.Count -eq 1 -and $owned.scheduler.Count -eq 1) {
        Ensure-ControllerOwner $ProcessId
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
    Set-LoginInput $ProcessId "Stable LAN IPv4 address" $Address
    Set-LoginInput $ProcessId "HTTPS port" ([string]$Port)
}

function Get-FileSha256([string]$Path) {
    (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

function Test-ControllerHttps([object]$Config) {
    try {
        $rootPath = Join-Path (Join-Path $controllerRoot "tls") "root-cert.der"
        $rootCertificate = [System.Security.Cryptography.X509Certificates.X509Certificate2]::new(
            [System.IO.File]::ReadAllBytes($rootPath)
        )
        $handler = [System.Net.Http.HttpClientHandler]::new()
        $handler.UseProxy = $false
        $handler.ServerCertificateCustomValidationCallback = {
            param($request, $certificate, $chain, $errors)
            $nameOrMissing = [System.Net.Security.SslPolicyErrors]::RemoteCertificateNameMismatch -bor
                [System.Net.Security.SslPolicyErrors]::RemoteCertificateNotAvailable
            if (($errors -band $nameOrMissing) -ne 0) { return $false }
            $chain.ChainPolicy.TrustMode =
                [System.Security.Cryptography.X509Certificates.X509ChainTrustMode]::CustomRootTrust
            $chain.ChainPolicy.VerificationFlags =
                [System.Security.Cryptography.X509Certificates.X509VerificationFlags]::NoFlag
            $chain.ChainPolicy.RevocationMode =
                [System.Security.Cryptography.X509Certificates.X509RevocationMode]::NoCheck
            $null = $chain.ChainPolicy.CustomTrustStore.Add($rootCertificate)
            return $chain.Build($certificate)
        }.GetNewClosure()
        $client = [System.Net.Http.HttpClient]::new($handler)
        $client.Timeout = [TimeSpan]::FromSeconds(3)
        try {
            $response = $client.GetAsync(
                "https://127.0.0.1:$($Config.endpointPort)/ready"
            ).GetAwaiter().GetResult()
            try { return [int]$response.StatusCode -eq 200 }
            finally { $response.Dispose() }
        } finally {
            $client.Dispose()
            $handler.Dispose()
            $rootCertificate.Dispose()
        }
    } catch {
        return $false
    }
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
    Set-LoginInput $desktop.Id "Stable LAN IPv4 address" $lanAddress
    Set-LoginInput $desktop.Id "HTTPS port" ([string]$selectedHttpsPort)
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

    $databaseSystemIdentifier = Invoke-Psql $config "SELECT system_identifier FROM pg_control_system();"
    $operatorUsersTable = Invoke-Psql $config "SELECT to_regclass('public.operator_users') IS NOT NULL;"
    $operatorUserCount = Invoke-Psql $config "SELECT COUNT(*) FROM public.operator_users;"
    $checks.no_owner_or_lan_bootstrap = $operatorUsersTable -eq "t" -and $operatorUserCount -eq "0" -and
        $config.endpointPort -gt 0 -and $config.databasePort -gt 0
    if (-not $checks.no_owner_or_lan_bootstrap) { throw "controller_m1_owner_boundary_invalid" }
    Bootstrap-ControllerOwner $desktop.Id
    $enabledOwnerCount = Invoke-Psql $config "SELECT COUNT(*) FROM public.operator_users WHERE role = 'OWNER' AND enabled;"
    $checks.local_first_owner_bootstrap = $enabledOwnerCount -eq "1"
    if (-not $checks.local_first_owner_bootstrap) { throw "controller_local_first_owner_bootstrap_invalid" }
    Wait-Until { Test-ControllerHttps $config } 60 "controller_tls_readiness_failed"
    $checks.local_readiness_uses_private_root = $true
    if (-not (Test-PlaintextHttpRejected $config)) {
        throw "controller_plaintext_health_listener_present"
    }
    $checks.plaintext_health_rejected = $true
    $owned = Assert-ControllerProcesses
    $httpPid = [int]$owned.http[0].ProcessId
    $schedulerPid = [int]$owned.scheduler[0].ProcessId
    $postgresPid = [int]$owned.postgres[0].ProcessId
    if ($httpPid -eq $schedulerPid) { throw "controller_http_scheduler_process_collapsed" }
    $checks.separate_http_and_scheduler_processes = $true
    $dbListeners = @(Get-ListenerAddresses ([int]$config.databasePort))
    $httpListeners = @(Get-ListenerAddresses ([int]$config.endpointPort))
    $privateDatabaseAndWildcardTls =
        $dbListeners.Count -eq 1 -and $dbListeners[0] -eq "127.0.0.1" -and
        $httpListeners.Count -eq 1 -and $httpListeners[0] -eq "0.0.0.0" -and
        (Test-Path -LiteralPath $servingKeyPath -PathType Leaf)
    Set-Check "loopback_postgres_wildcard_https_listener" $privateDatabaseAndWildcardTls `
        "controller_listener_topology_invalid"
    if (-not $privateDatabaseAndWildcardTls) { throw "controller_listener_topology_invalid" }
    Invoke-Psql $config "CREATE TABLE dx04_runtime_evidence (id integer PRIMARY KEY, marker text NOT NULL); INSERT INTO dx04_runtime_evidence (id, marker) VALUES (1, '$sentinel');" | Out-Null
    Assert-DatabaseValue $config $sentinel

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
    if ((Get-ControllerConfig).endpointPort -ne $oldHttpsPort -or
        (Get-FileSha256 $rootCertificatePath) -cne $rootBeforeEndpointChange -or
        (Get-FileSha256 $leafCertificatePath) -cne $leafBeforeEndpointChange -or
        -not (Test-ControllerHttps $config)) {
        throw "controller_running_unavailable_ip_changed_state"
    }
    $checks.endpoint_running_unavailable_ip_rolls_back = $true

    $endpointReservation = [System.Net.Sockets.TcpListener]::new([Net.IPAddress]::Any, 0)
    $endpointReservation.Start()
    $collisionPort = ([System.Net.IPEndPoint]$endpointReservation.LocalEndpoint).Port
    try {
        Set-ControllerEndpointFields $desktop.Id $lanAddress $collisionPort
        Invoke-Button $desktop.Id "Apply endpoint change"
        Wait-Until {
            $null -ne (Find-TextContaining (Get-Window $desktop.Id) "already in use")
        } 15 "controller_running_endpoint_collision_error_not_visible"
        $afterCollisionProcesses = Assert-ControllerProcesses
        if ((Get-ControllerConfig).endpointPort -ne $oldHttpsPort -or
            (Get-FileSha256 $rootCertificatePath) -cne $rootBeforeEndpointChange -or
            (Get-FileSha256 $leafCertificatePath) -cne $leafBeforeEndpointChange -or
            [int]$afterCollisionProcesses.postgres[0].ProcessId -ne $oldPostgresPid -or
            [int]$afterCollisionProcesses.http[0].ProcessId -ne $oldHttpPid -or
            [int]$afterCollisionProcesses.scheduler[0].ProcessId -ne $oldSchedulerPid -or
            -not (Test-ControllerHttps $config)) {
            throw "controller_running_endpoint_collision_changed_runtime_or_identity"
        }
        $checks.endpoint_running_collision_rolls_back = $true
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
        Wait-Until { Test-ControllerHttps $config } 60 "controller_reconfigured_https_readiness_failed"
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
            [int]$newProcesses.postgres[0].ProcessId -ne $oldPostgresPid -or
            [int]$newProcesses.http[0].ProcessId -eq $oldHttpPid -or
            [int]$newProcesses.scheduler[0].ProcessId -eq $oldSchedulerPid -or
            $schedulerExited -ge $httpExited -or $newSchedulerStart -lt $newHttpStart -or
            @(Get-ListenerAddresses $oldHttpsPort).Count -ne 0 -or
            (Get-ListenerAddresses $newHttpsPort).Count -ne 1 -or
            -not (Test-PlaintextHttpRejected $config)) {
            throw "controller_running_endpoint_transition_invalid"
        }
        $checks.endpoint_running_transition_preserves_postgres = $true
        $checks.plaintext_health_rejected = $true
        Ensure-ControllerOwner $desktop.Id -ForceReauthentication
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
            $current.scheduler.Count -eq 1 -and (Test-ControllerHttps $config)
    } 15 "controller_runtime_stopped_when_window_hidden"
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
    $checks.reopen_requires_operator_sign_in = $true
    Ensure-ControllerOwner $desktop.Id -ForceReauthentication
    $checks.owner_reauthenticated_after_reopen = $true
    $owned = Assert-ControllerProcesses
    if ([int]$owned.postgres[0].ProcessId -ne $postgresPid -or
        [int]$owned.http[0].ProcessId -ne $httpPid -or
        [int]$owned.scheduler[0].ProcessId -ne $schedulerPid -or
        (Get-ControllerConfig).controllerId -cne $controllerIdentity) {
        throw "controller_reopen_changed_runtime_identity"
    }
    $checks.reopen_keeps_one_runtime_and_database_identity = $true

    $shutdownProcesses = @{
        scheduler = Get-TrackedProcess $schedulerPid $runtimeExecutable
        http = Get-TrackedProcess $httpPid $runtimeExecutable
        postgres = Get-TrackedProcess $postgresPid (Join-Path $postgresBin "postgres.exe")
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
        Quit-Desktop $desktop.Id
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
    Wait-Until { Test-ControllerHttps $config } 60 "controller_leaf_renewal_https_not_ready"
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
    $checks.relaunch_preserves_database_and_endpoint = $true

    $owned = Assert-ControllerProcesses
    $databaseCrashPid = [int]$owned.postgres[0].ProcessId
    $databaseCrashProcess = Get-TrackedProcess $databaseCrashPid (Join-Path $postgresBin "postgres.exe")
    if (-not $databaseCrashProcess) { throw "controller_database_process_unavailable_before_crash" }
    $databaseCrashProcess.Kill()
    $databaseCrashProcess.WaitForExit()
    $databaseCrashProcess.Dispose()
    Wait-ControllerState $desktop.Id "Failed" "controller_database_process_exited" 25
    Wait-Until { (Get-ControllerProcesses).http.Count -eq 0 -and (Get-ControllerProcesses).scheduler.Count -eq 0 } `
        15 "controller_dependents_survived_database_crash"
    Quit-Desktop $desktop.Id
    $desktop = Start-ExistingController
    $config = Get-ControllerConfig
    Wait-Until { Test-ControllerHttps $config } 60 "controller_database_crash_recovery_failed"
    Assert-DatabaseValue $config $sentinel
    if ([string](Invoke-Psql $config "SELECT system_identifier FROM pg_control_system();") -cne $databaseSystemIdentifier) {
        throw "controller_database_identity_changed_after_crash"
    }
    $checks.database_crash_fails_closed_and_recovers_wal = $true

    $beforeParentCrash = Assert-ControllerProcesses
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
    Wait-Until { Test-ControllerHttps $config } 60 "controller_parent_crash_wal_recovery_failed"
    Assert-DatabaseValue $config $sentinel
    $owned = Assert-ControllerProcesses
    $rootFingerprintAfterParentCrash =
        (Get-FileHash -LiteralPath $rootCertificatePath -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($owned.scheduler.Count -ne 1 -or
        [string](Invoke-Psql $config "SELECT system_identifier FROM pg_control_system();") -cne $databaseSystemIdentifier -or
        $rootFingerprintAfterParentCrash -cne $rootFingerprintBeforeRestart) {
        throw "controller_scheduler_or_database_duplicated_after_parent_crash"
    }
    $checks.controller_root_identity_survives_crash_restart = $true
    $checks.desktop_parent_crash_owns_process_tree_and_recovers_wal = $true

    $currentMigration = Invoke-Psql $config "SELECT version_num FROM alembic_version;"
    Invoke-Psql $config "UPDATE alembic_version SET version_num = 'dx04_missing_revision';" | Out-Null
    Quit-Desktop $desktop.Id
    $desktop = Start-Desktop 260
    Wait-ControllerState $desktop.Id "Failed" "controller_migration_failed" 150
    $config = Get-ControllerConfig
    Assert-DatabaseValue $config $sentinel
    if ([string](Invoke-Psql $config "SELECT system_identifier FROM pg_control_system();") -cne $databaseSystemIdentifier) {
        throw "controller_migration_failure_reinitialized_database"
    }
    Invoke-Psql $config "UPDATE alembic_version SET version_num = '$currentMigration';" | Out-Null
    Quit-Desktop $desktop.Id
    $desktop = Start-ExistingController
    $config = Get-ControllerConfig
    Wait-Until { Test-ControllerHttps $config } 60 "controller_migration_recovery_failed"
    Assert-DatabaseValue $config $sentinel
    $checks.failed_migration_preserves_existing_cluster = $true

    Quit-Desktop $desktop.Id
    $config = Get-ControllerConfig
    $databaseReservation = [System.Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, [int]$config.databasePort)
    $databaseReservation.Start()
    try {
        $desktop = Start-Desktop
        Wait-ControllerState $desktop.Id "Failed" "controller_database_port_in_use" 40
        if ((Get-ControllerConfig).databasePort -ne $config.databasePort -or
            (Get-ControllerProcesses).all.Count -ne 0) { throw "controller_database_port_silently_rotated" }
        $checks.database_port_collision_does_not_rotate = $true
        Quit-Desktop $desktop.Id
    } finally {
        $databaseReservation.Stop()
    }

    $endpointReservation = [System.Net.Sockets.TcpListener]::new([Net.IPAddress]::Any, [int]$config.endpointPort)
    $endpointReservation.Start()
    try {
        $desktop = Start-Desktop
        Wait-ControllerState $desktop.Id "Failed" "controller_endpoint_port_in_use" 40
        if ((Get-ControllerConfig).endpointPort -ne $config.endpointPort -or
            (Get-ControllerProcesses).all.Count -ne 0) { throw "controller_endpoint_silently_rotated" }
        $checks.endpoint_port_collision_does_not_rotate = $true
        Quit-Desktop $desktop.Id
    } finally {
        $endpointReservation.Stop()
    }

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
    Quit-Desktop $desktop.Id
    Move-Item -LiteralPath $controllerRoot -Destination $unownedRoot
    Move-Item -LiteralPath $savedRoot -Destination $controllerRoot
    $rootWasMoved = $false

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
    Quit-Desktop $desktop.Id
    Set-Acl -LiteralPath $controllerRoot -AclObject $aclBeforeDeny
    $aclBeforeDeny = $null

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
    Quit-Desktop $desktop.Id
    [System.IO.File]::WriteAllText($pgVersionPath, $pgVersionBackup)

    $processEvidence = [ordered]@{
        initial_postgres_pid = $postgresPid
        initial_http_pid = $httpPid
        initial_scheduler_pid = $schedulerPid
        parent_crash_runtime_pids = $parentCrashPids
        controller_id = $controllerIdentity
        database_system_identifier = $databaseSystemIdentifier
        persisted_database_port = $config.databasePort
        persisted_endpoint_port = $config.endpointPort
        graceful_quit_exit_order = $shutdownExitOrder
    }
    $result = if (@($checks.Values | Where-Object { -not $_ }).Count -eq 0 -and $failureCodes.Count -eq 0) {
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
    $evidence = [ordered]@{
        schema_version = 1
        source_revision = $ExpectedSourceRevision
        checked_head = $head
        source_worktree_clean = $worktreeIsClean
        windows_version = [Environment]::OSVersion.Version.ToString()
        windows_x64 = [Environment]::Is64BitOperatingSystem
        current_user_is_administrator = [Security.Principal.WindowsPrincipal]::new(
            [Security.Principal.WindowsIdentity]::GetCurrent()
        ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
        data_root = "CurrentUserLocalAppData"
        runtime_layout = "shared"
        checks = $checks
        process_evidence = $processEvidence
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
