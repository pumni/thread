[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$DesktopExecutable,
    [Parameter(Mandatory = $true)]
    [string]$RuntimeRoot,
    [Parameter(Mandatory = $true)]
    [string]$ExpectedSourceRevision,
    [Parameter(Mandatory = $true)]
    [string]$EvidencePath
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
if (-not ("ThreadsControllerSmoke.NativeMethods" -as [type])) {
    Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;

namespace ThreadsControllerSmoke {
    public static class NativeMethods {
        [DllImport("user32.dll", SetLastError = true)]
        public static extern bool PostMessage(IntPtr window, uint message, IntPtr wParam, IntPtr lParam);
        [DllImport("user32.dll", SetLastError = true)]
        public static extern bool IsWindowVisible(IntPtr window);
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
    loopback_only_database_and_endpoint = $false
    no_owner_or_lan_bootstrap = $false
    separate_http_and_scheduler_processes = $false
    x_hides_and_runtime_continues = $false
    reopen_keeps_one_runtime_and_database_identity = $false
    graceful_quit_stops_scheduler_http_then_postgres = $false
    relaunch_preserves_database_and_endpoint = $false
    database_crash_fails_closed_and_recovers_wal = $false
    desktop_parent_crash_owns_process_tree_and_recovers_wal = $false
    failed_migration_preserves_existing_cluster = $false
    database_port_collision_does_not_rotate = $false
    endpoint_port_collision_does_not_rotate = $false
    unowned_root_is_preserved_and_rejected = $false
    unwritable_root_is_rejected = $false
    corrupt_cluster_is_preserved_and_rejected = $false
}
$result = "BLOCKER"
$failureCode = $null
$head = $null
$worktreeIsClean = $false
$controllerIdentity = $null
$databaseSystemIdentifier = $null
$shutdownExitOrder = $null
$sentinel = [Guid]::NewGuid().ToString("N")
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

function Get-Window([int]$ProcessId) {
    $process = Get-Process -Id $ProcessId -ErrorAction SilentlyContinue
    if (-not $process) { return $null }
    $process.Refresh()
    if ($process.MainWindowHandle -eq [IntPtr]::Zero) { return $null }
    return [System.Windows.Automation.AutomationElement]::FromHandle($process.MainWindowHandle)
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
    return $Root.FindFirst([System.Windows.Automation.TreeScope]::Descendants, $condition)
}

function Find-TextContaining([System.Windows.Automation.AutomationElement]$Root, [string]$Text) {
    if (-not $Root) { return $null }
    $elements = $Root.FindAll(
        [System.Windows.Automation.TreeScope]::Descendants,
        [System.Windows.Automation.Condition]::TrueCondition
    )
    foreach ($element in $elements) {
        if ($element.Current.Name.IndexOf($Text, [System.StringComparison]::OrdinalIgnoreCase) -ge 0) {
            return $element
        }
    }
    return $null
}

function Invoke-Button([int]$ProcessId, [string]$Name) {
    Wait-Until {
        $button = Find-Element (Get-Window $ProcessId) $Name `
            ([System.Windows.Automation.ControlType]::Button)
        if (-not $button) { return $false }
        $button.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern).Invoke()
        return $true
    } 20 "desktop_button_unavailable_$($Name -replace '\W+', '_')"
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
            return $null -ne (Find-TextContaining $window $Diagnostic)
        }
        return $null -ne (Find-Element $window $State ([System.Windows.Automation.ControlType]::Text))
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

function Quit-Desktop([int]$ProcessId) {
    Invoke-Button $ProcessId "Quit…"
    Invoke-Button $ProcessId "Stop node and quit"
    Wait-Until { -not (Get-Process -Id $ProcessId -ErrorAction SilentlyContinue) } `
        55 "desktop_graceful_quit_timeout"
}

function Get-ProcessExitTime([System.Diagnostics.Process]$Process, [string]$Failure) {
    Wait-Until {
        try {
            $Process.Refresh()
            return $Process.HasExited
        } catch { return $false }
    } 20 $Failure
    return $Process.ExitTime.ToUniversalTime()
}

function Test-Http([object]$Config) {
    try {
        $response = Invoke-WebRequest -Uri "http://127.0.0.1:$($Config.endpointPort)/ready" `
            -TimeoutSec 3 -UseBasicParsing
        return $response.StatusCode -eq 200
    } catch {
        return $false
    }
}

function Assert-DatabaseValue([object]$Config, [string]$Expected) {
    $actual = Invoke-Psql $Config "SELECT marker FROM dx04_runtime_evidence WHERE id = 1;"
    if ($actual -cne $Expected) { throw "controller_durable_sentinel_mismatch" }
}

try {
    $headLines = @(& git -C $repoRoot rev-parse HEAD)
    if ($LASTEXITCODE -ne 0 -or $headLines.Count -eq 0) { throw "controller_git_head_unavailable" }
    $head = ($headLines -join "`n").Trim()
    $dirtyLines = @(& git -C $repoRoot status --porcelain)
    if ($LASTEXITCODE -ne 0) { throw "controller_git_status_unavailable" }
    $dirty = [string]::Join("`n", [string[]]$dirtyLines)
    if ($LASTEXITCODE -ne 0 -or $head -ne $ExpectedSourceRevision -or $dirty.Length -ne 0) {
        throw "controller_smoke_requires_exact_clean_source_revision"
    }
    $worktreeIsClean = $true
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
    Wait-ControllerState $desktop.Id "Controller runtime is running" $null 360
    $config = Get-ControllerConfig
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
        $config.schemaVersion -eq 1 -and $config.clusterInitialized -eq $true
    if (-not $checks.atomic_non_secret_config) { throw "controller_config_contains_secret_or_invalid_state" }

    Wait-Until { Test-Http $config } 60 "controller_http_readiness_failed"
    $owned = Assert-ControllerProcesses
    $httpPid = [int]$owned.http[0].ProcessId
    $schedulerPid = [int]$owned.scheduler[0].ProcessId
    $postgresPid = [int]$owned.postgres[0].ProcessId
    if ($httpPid -eq $schedulerPid) { throw "controller_http_scheduler_process_collapsed" }
    $checks.separate_http_and_scheduler_processes = $true
    $dbListeners = @(Get-ListenerAddresses ([int]$config.databasePort))
    $httpListeners = @(Get-ListenerAddresses ([int]$config.endpointPort))
    $loopbackOnly = $dbListeners.Count -eq 1 -and $dbListeners[0] -eq "127.0.0.1" -and
        $httpListeners.Count -eq 1 -and $httpListeners[0] -eq "127.0.0.1"
    Set-Check "loopback_only_database_and_endpoint" $loopbackOnly "controller_listener_not_loopback_only"
    if (-not $loopbackOnly) { throw "controller_listener_not_loopback_only" }

    $databaseSystemIdentifier = Invoke-Psql $config "SELECT system_identifier FROM pg_control_system();"
    $ownerTable = Invoke-Psql $config "SELECT to_regclass('public.operator_users') IS NULL;"
    $checks.no_owner_or_lan_bootstrap = $ownerTable -eq "t" -and
        $config.endpointPort -gt 0 -and $config.databasePort -gt 0
    if (-not $checks.no_owner_or_lan_bootstrap) { throw "controller_m1_owner_boundary_invalid" }
    Invoke-Psql $config "CREATE TABLE dx04_runtime_evidence (id integer PRIMARY KEY, marker text NOT NULL); INSERT INTO dx04_runtime_evidence (id, marker) VALUES (1, '$sentinel');" | Out-Null
    Assert-DatabaseValue $config $sentinel

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
            $current.scheduler.Count -eq 1 -and (Test-Http $config)
    } 15 "controller_runtime_stopped_when_window_hidden"
    Assert-DatabaseValue $config $sentinel
    $checks.x_hides_and_runtime_continues = $true

    $second = Start-Process -FilePath $DesktopExecutable -PassThru
    $desktopPids.Add([int]$second.Id)
    Wait-Until { $second.HasExited } 20 "second_desktop_launch_did_not_converge"
    Wait-Until { [ThreadsControllerSmoke.NativeMethods]::IsWindowVisible($window.MainWindowHandle) } `
        10 "controller_reopen_did_not_restore_window"
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
    try {
        Quit-Desktop $desktop.Id
        $schedulerExit = Get-ProcessExitTime $shutdownProcesses.scheduler "controller_scheduler_did_not_stop_first"
        $httpExit = Get-ProcessExitTime $shutdownProcesses.http "controller_http_did_not_stop_after_scheduler"
        $postgresExit = Get-ProcessExitTime $shutdownProcesses.postgres "controller_database_did_not_stop_after_http"
        if (-not ($schedulerExit -lt $httpExit -and $httpExit -lt $postgresExit)) {
            throw "controller_shutdown_order_invalid"
        }
        $shutdownExitOrder = @(
            [ordered]@{ process = "scheduler"; exited_utc = $schedulerExit.ToString("o") },
            [ordered]@{ process = "http"; exited_utc = $httpExit.ToString("o") },
            [ordered]@{ process = "postgres"; exited_utc = $postgresExit.ToString("o") }
        )
    } finally {
        foreach ($process in $shutdownProcesses.Values) { $process.Dispose() }
    }
    Wait-Until { (Get-ControllerProcesses).all.Count -eq 0 } 15 "controller_quit_left_runtime_processes"
    $checks.graceful_quit_stops_scheduler_http_then_postgres = $true

    $desktop = Start-ExistingController
    $config = Get-ControllerConfig
    Wait-Until { Test-Http $config } 60 "controller_relaunch_http_not_ready"
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
    Wait-Until { Test-Http $config } 60 "controller_database_crash_recovery_failed"
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
    Wait-Until { Test-Http $config } 60 "controller_parent_crash_wal_recovery_failed"
    Assert-DatabaseValue $config $sentinel
    $owned = Assert-ControllerProcesses
    if ($owned.scheduler.Count -ne 1 -or
        [string](Invoke-Psql $config "SELECT system_identifier FROM pg_control_system();") -cne $databaseSystemIdentifier) {
        throw "controller_scheduler_or_database_duplicated_after_parent_crash"
    }
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
    Wait-Until { Test-Http $config } 60 "controller_migration_recovery_failed"
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

    $endpointReservation = [System.Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, [int]$config.endpointPort)
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
        [System.Security.AccessControl.FileSystemRights]::Write -bor
            [System.Security.AccessControl.FileSystemRights]::Modify,
        [System.Security.AccessControl.InheritanceFlags]::ContainerInherit -bor
            [System.Security.AccessControl.InheritanceFlags]::ObjectInherit,
        [System.Security.AccessControl.PropagationFlags]::None,
        [System.Security.AccessControl.AccessControlType]::Deny
    )
    $aclWithDeny.AddAccessRule($denyWriteRule)
    Set-Acl -LiteralPath $controllerRoot -AclObject $aclWithDeny
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
    }
    [System.IO.File]::WriteAllText(
        $EvidencePath,
        ($evidence | ConvertTo-Json -Depth 8),
        [System.Text.UTF8Encoding]::new($false)
    )
}

if ($result -ne "PASS") { throw "DX-04 Windows Controller lifecycle smoke blocked: $failureCode" }
Write-Output "DX-04 Windows Controller lifecycle smoke PASS for $ExpectedSourceRevision"
