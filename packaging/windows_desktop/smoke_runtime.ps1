[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$BundleRoot,
    [Parameter(Mandatory = $true)]
    [string]$EvidencePath,
    [string]$ExpectedSourceRevision,
    [ValidateSet("local_non_clean_windows_smoke", "github_hosted_windows_x64_isolated")]
    [string]$RunKind = "local_non_clean_windows_smoke",
    [switch]$CleanWindowsEvidence
)

$ErrorActionPreference = "Stop"
$PSNativeCommandUseErrorActionPreference = $false
Add-Type -TypeDefinition @"
using System;
using System.ComponentModel;
using System.Runtime.InteropServices;

public static class ThreadsCurrentUserDpapi
{
    [StructLayout(LayoutKind.Sequential)]
    private struct DataBlob
    {
        public int Length;
        public IntPtr Data;
    }

    [DllImport("Crypt32.dll", SetLastError = true, CharSet = CharSet.Unicode)]
    private static extern bool CryptProtectData(
        ref DataBlob input, string description, IntPtr entropy, IntPtr reserved,
        IntPtr prompt, int flags, out DataBlob output);

    [DllImport("Crypt32.dll", SetLastError = true, CharSet = CharSet.Unicode)]
    private static extern bool CryptUnprotectData(
        ref DataBlob input, IntPtr description, IntPtr entropy, IntPtr reserved,
        IntPtr prompt, int flags, out DataBlob output);

    [DllImport("Kernel32.dll")]
    private static extern IntPtr LocalFree(IntPtr memory);

    public static byte[] Protect(byte[] value) { return Transform(value, true); }
    public static byte[] Unprotect(byte[] value) { return Transform(value, false); }

    private static byte[] Transform(byte[] value, bool protect)
    {
        var input = new DataBlob { Length = value.Length, Data = Marshal.AllocHGlobal(value.Length) };
        Marshal.Copy(value, 0, input.Data, value.Length);
        try
        {
            DataBlob output;
            var success = protect
                ? CryptProtectData(ref input, "Threads Desktop local DB credential", IntPtr.Zero,
                    IntPtr.Zero, IntPtr.Zero, 1, out output)
                : CryptUnprotectData(ref input, IntPtr.Zero, IntPtr.Zero,
                    IntPtr.Zero, IntPtr.Zero, 1, out output);
            if (!success) throw new Win32Exception(Marshal.GetLastWin32Error());
            try
            {
                var result = new byte[output.Length];
                Marshal.Copy(output.Data, result, 0, output.Length);
                return result;
            }
            finally { LocalFree(output.Data); }
        }
        finally { Marshal.FreeHGlobal(input.Data); }
    }
}
"@
$BundleRoot = (Resolve-Path -LiteralPath $BundleRoot).Path
$postgresBin = Join-Path $BundleRoot "postgresql\bin"
$bundleManifest = Get-Content -LiteralPath (Join-Path $BundleRoot "runtime-manifest.json") -Raw | ConvertFrom-Json
$layout = [string]$bundleManifest.layout
$cleanVmStatus = if ($CleanWindowsEvidence) { "PENDING" } else { "BLOCKER" }
$cleanVmReason = if ($CleanWindowsEvidence) {
    "GitHub-hosted Windows x64; runtime prerequisites are removed from PATH and environment before smoke."
} else {
    "Local smoke on a developer Windows host is not clean-machine acceptance evidence."
}
$localSmokeStatus = "FAIL"
$primaryFailureCode = $null
$logScrubStatus = "NOT_RUN"
$logScrubFailureCode = $null
$httpStatus = $null
$apiProcess = $null
$schedulerProcess = $null
$postgresStarted = $false
$databaseUrl = $null
$databasePassword = $null
$secretFile = $null
$pathBefore = $env:PATH
$savedRuntimeEnvironment = @{}
$localRoot = $null
$postgresLog = $null
$rawLogs = [System.Collections.Generic.List[string]]::new()
$safeLogs = [System.Collections.Generic.List[string]]::new()
$evidence = [ordered]@{
    schema_version = 3
    run_kind = $RunKind
    source_revision = [string]$bundleManifest.source_revision
    source_tree_dirty = [bool]$bundleManifest.source_tree_dirty
    layout = $layout
    status = $localSmokeStatus
    clean_windows_runner_status = $cleanVmStatus
    clean_windows_runner_reason = $cleanVmReason
    host = [ordered]@{
        windows_version = [Environment]::OSVersion.Version.ToString()
        architecture_x64 = [Environment]::Is64BitOperatingSystem
        github_runner_image = $env:ImageOS
        current_user_is_administrator = $false
        ambient_path_prerequisites = @{}
        sanitized_path_entries = @()
        sanitized_path_tool_inventory = [ordered]@{}
        runtime_bundle_root = $null
        cleared_runtime_environment_names = @()
        sanitized_path_missing_python_uv_docker = $false
    }
    checks = [ordered]@{}
    timings_ms = [ordered]@{}
    redacted_logs = @()
    primary_failure_code = $null
    failure_code = $null
    log_scrub_status = $logScrubStatus
    log_scrub_failure_code = $null
}

function Get-ResolvedApplicationEvidence([string]$Name) {
    $command = Get-Command -Name $Name -CommandType Application -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($null -eq $command) {
        return [ordered]@{ resolved = $false; path = $null }
    }
    $resolvedPath = [string]$command.Source
    if (-not [System.IO.Path]::IsPathFullyQualified($resolvedPath)) {
        throw "runtime_tool_resolution_not_absolute"
    }
    return [ordered]@{
        resolved = $true
        path = [System.IO.Path]::GetFullPath($resolvedPath)
    }
}

function Get-CommandPresence([string]$Name) {
    return [bool](Get-ResolvedApplicationEvidence $Name).resolved
}

function Clear-RuntimeEnvironment {
    $names = @(
        Get-ChildItem Env: |
            Where-Object {
                $_.Name -match "^(THREADS_PLATFORM_.*|PYTHON.*|PYLAUNCHER.*|UV_.*|VIRTUAL_ENV|PG.*|DOCKER_.*|COMPOSE_.*)$"
            } |
            ForEach-Object { $_.Name }
    )
    foreach ($name in $names) {
        $savedRuntimeEnvironment[$name] = (Get-Item -LiteralPath "Env:$name").Value
        Remove-Item -LiteralPath "Env:$name" -ErrorAction SilentlyContinue
    }
    $evidence.host.cleared_runtime_environment_names = @($names | Sort-Object)
}

function Get-Sha256Hex([string]$Path) {
    $algorithm = [System.Security.Cryptography.SHA256]::Create()
    $stream = [System.IO.File]::OpenRead($Path)
    try { $digest = $algorithm.ComputeHash($stream) }
    finally {
        $stream.Dispose()
        $algorithm.Dispose()
    }
    return [BitConverter]::ToString($digest).Replace("-", "").ToLowerInvariant()
}

function ConvertTo-RedactedRuntimeLog([string]$Content) {
    if ($databaseUrl) { $Content = $Content.Replace($databaseUrl, "<REDACTED_DATABASE_URL>") }
    if ($databasePassword) { $Content = $Content.Replace($databasePassword, "<REDACTED>") }
    $Content = [regex]::Replace(
        $Content,
        "(?i)(password|database_url)\s*[:=]\s*(?!<REDACTED>)[^\s]+",
        '$1=<REDACTED>'
    )
    $Content = [regex]::Replace(
        $Content,
        "(?i)(authorization\s*[:=]\s*Bearer\s+)(?!<REDACTED>)[^\s]+",
        '$1<REDACTED>'
    )
    $Content = [regex]::Replace(
        $Content,
        "(?i)\bBearer\s+(?!<REDACTED>)[A-Za-z0-9._~+/-]{24,}",
        'Bearer <REDACTED>'
    )
    return [regex]::Replace(
        $Content,
        "(?i)(postgres(?:ql)?(?:\+\w+)?://)[^\s:@/]+:[^@\s/]+@",
        '$1<REDACTED>@'
    )
}

function Restrict-TreeToCurrentUser([string]$Path) {
    try { $acl = Get-Acl -LiteralPath $Path -ErrorAction Stop }
    catch { throw "private_data_acl_query_failed" }
    $currentSid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User
    $inheritance = [System.Security.AccessControl.InheritanceFlags]::ContainerInherit -bor
        [System.Security.AccessControl.InheritanceFlags]::ObjectInherit
    $accessRules = @($acl.GetAccessRules($true, $false, [System.Security.Principal.SecurityIdentifier]))
    $alreadyRestricted = $acl.AreAccessRulesProtected -and $accessRules.Count -eq 1 -and
        $accessRules[0].IdentityReference.Value -eq $currentSid.Value -and
        $accessRules[0].AccessControlType -eq [System.Security.AccessControl.AccessControlType]::Allow -and
        $accessRules[0].FileSystemRights -eq [System.Security.AccessControl.FileSystemRights]::FullControl -and
        $accessRules[0].InheritanceFlags -eq $inheritance -and
        $accessRules[0].PropagationFlags -eq [System.Security.AccessControl.PropagationFlags]::None -and
        -not $accessRules[0].IsInherited

    if (-not $alreadyRestricted) {
        $acl.SetAccessRuleProtection($true, $false)
        foreach ($existingRule in $acl.GetAccessRules(
            $true,
            $true,
            [System.Security.Principal.SecurityIdentifier]
        )) {
            [void]$acl.RemoveAccessRuleSpecific($existingRule)
        }
        $rule = [System.Security.AccessControl.FileSystemAccessRule]::new(
            $currentSid,
            [System.Security.AccessControl.FileSystemRights]::FullControl,
            $inheritance,
            [System.Security.AccessControl.PropagationFlags]::None,
            [System.Security.AccessControl.AccessControlType]::Allow
        )
        [void]$acl.AddAccessRule($rule)
        try { Set-Acl -LiteralPath $Path -AclObject $acl -ErrorAction Stop }
        catch { throw ("private_data_acl_write_failed_{0}" -f $_.Exception.HResult.ToString("X8")) }
    }

    try { $verified = Get-Acl -LiteralPath $Path -ErrorAction Stop }
    catch { throw "private_data_acl_verify_failed" }
    $verifiedRules = @(
        $verified.GetAccessRules($true, $false, [System.Security.Principal.SecurityIdentifier])
    )
    $verifiedCorrectly = $verified.AreAccessRulesProtected -and $verifiedRules.Count -eq 1 -and
        $verifiedRules[0].IdentityReference.Value -eq $currentSid.Value -and
        $verifiedRules[0].AccessControlType -eq [System.Security.AccessControl.AccessControlType]::Allow -and
        $verifiedRules[0].FileSystemRights -eq [System.Security.AccessControl.FileSystemRights]::FullControl -and
        $verifiedRules[0].InheritanceFlags -eq $inheritance -and
        $verifiedRules[0].PropagationFlags -eq [System.Security.AccessControl.PropagationFlags]::None -and
        -not $verifiedRules[0].IsInherited
    if (-not $verifiedCorrectly) {
        throw "private_data_acl_mismatch"
    }
}

function Invoke-Bundled([string]$Executable, [string[]]$Arguments, [string]$Label) {
    $timer = [System.Diagnostics.Stopwatch]::StartNew()
    $raw = Join-Path $localRoot "$Label.raw.log"
    $safe = Join-Path $EvidenceDirectory "$Label.redacted.log"
    $rawLogs.Add($raw)
    $safeLogs.Add($safe)
    if ($Label.StartsWith("postgres-", [System.StringComparison]::Ordinal)) {
        $quotedArguments = @($Arguments | ForEach-Object {
            '"' + $_ + '"'
        })
        $nativeProcess = Start-Process -FilePath $Executable `
            -ArgumentList ($quotedArguments -join " ") -PassThru -WindowStyle Hidden
        $deadline = [DateTime]::UtcNow.AddSeconds(30)
        while (-not $nativeProcess.HasExited -and [DateTime]::UtcNow -lt $deadline) {
            Start-Sleep -Milliseconds 100
        }
        if (-not $nativeProcess.HasExited) {
            Stop-Process -Id $nativeProcess.Id -Force -ErrorAction SilentlyContinue
            throw "${Label}_timeout"
        }
        $exitCode = $nativeProcess.ExitCode
        $nativeProcess.Dispose()
        $timer.Stop()
        $evidence.timings_ms[$Label] = [int]$timer.ElapsedMilliseconds
        [System.IO.File]::WriteAllText(
            $raw,
            "pg_ctl $Label exit code: $exitCode`n",
            [System.Text.UTF8Encoding]::new($false)
        )
        if ($exitCode -ne 0) { throw "${Label}_exit_$exitCode" }
        return
    }
    $quotedArguments = @($Arguments | ForEach-Object { '"' + $_ + '"' })
    $nativeProcess = Start-Process -FilePath $Executable `
        -ArgumentList ($quotedArguments -join " ") -PassThru -WindowStyle Hidden
    $deadline = [DateTime]::UtcNow.AddMinutes(2)
    while (-not $nativeProcess.HasExited -and [DateTime]::UtcNow -lt $deadline) {
        Start-Sleep -Milliseconds 100
    }
    if (-not $nativeProcess.HasExited) {
        Stop-Process -Id $nativeProcess.Id -Force -ErrorAction SilentlyContinue
        throw "${Label}_timeout"
    }
    $nativeProcess.WaitForExit()
    $nativeProcess.Refresh()
    $exitCode = $nativeProcess.ExitCode
    $nativeProcess.Dispose()
    $timer.Stop()
    $evidence.timings_ms[$Label] = [int]$timer.ElapsedMilliseconds
    [System.IO.File]::WriteAllText(
        $raw,
        "$Label process exit code: $exitCode`n",
        [System.Text.UTF8Encoding]::new($false)
    )
    $content = [System.IO.File]::ReadAllText($raw)
    if ($databaseUrl) { $content = $content.Replace($databaseUrl, "<REDACTED_DATABASE_URL>") }
    if ($databasePassword) { $content = $content.Replace($databasePassword, "<REDACTED>") }
    [System.IO.File]::WriteAllText($safe, $content, [System.Text.UTF8Encoding]::new($false))
    Remove-Item -LiteralPath $raw -Force -ErrorAction SilentlyContinue
    if ($exitCode -ne 0) {
        throw "${Label}_exit_$exitCode"
    }
}

function Invoke-Psql([string]$Sql, [string]$Label) {
    $output = & (Join-Path $postgresBin "psql.exe") -X -h 127.0.0.1 -p $port -U threads_local -d postgres -v ON_ERROR_STOP=1 -t -A -c $Sql 2>$null
    if ($LASTEXITCODE -ne 0) {
        throw "${Label}_query_failed"
    }
    return (($output | Out-String).Trim())
}

function Start-BackgroundRuntime([string]$Executable, [string[]]$Arguments, [string]$Label) {
    $stdout = Join-Path $localRoot "$Label.stdout.raw.log"
    $stderr = Join-Path $localRoot "$Label.stderr.raw.log"
    $rawLogs.Add($stdout)
    $rawLogs.Add($stderr)
    $safeLogs.Add((Join-Path $EvidenceDirectory "$Label.stdout.redacted.log"))
    $safeLogs.Add((Join-Path $EvidenceDirectory "$Label.stderr.redacted.log"))
    $timer = [System.Diagnostics.Stopwatch]::StartNew()
    if ($Arguments.Count -eq 0) {
        $process = Start-Process -FilePath $Executable -PassThru -WindowStyle Hidden `
            -RedirectStandardOutput $stdout -RedirectStandardError $stderr
    } else {
        $process = Start-Process -FilePath $Executable -ArgumentList $Arguments `
            -PassThru -WindowStyle Hidden -RedirectStandardOutput $stdout -RedirectStandardError $stderr
    }
    $timer.Stop()
    $evidence.timings_ms["${Label}_process_spawn"] = [int]$timer.ElapsedMilliseconds
    return $process
}

function Save-RedactedLogs {
    for ($index = 0; $index -lt $rawLogs.Count; $index++) {
        $raw = $rawLogs[$index]
        $safe = $safeLogs[$index]
        if (-not (Test-Path -LiteralPath $raw)) { continue }
        $content = ConvertTo-RedactedRuntimeLog ([System.IO.File]::ReadAllText($raw))
        [System.IO.File]::WriteAllText($safe, $content, [System.Text.UTF8Encoding]::new($false))
        Remove-Item -LiteralPath $raw -Force -ErrorAction SilentlyContinue
    }
}

$EvidenceDirectory = Split-Path -Parent $EvidencePath
New-Item -ItemType Directory -Path $EvidenceDirectory -Force | Out-Null
$identity = [System.Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [System.Security.Principal.WindowsPrincipal]::new($identity)
$isAdministrator = $principal.IsInRole([System.Security.Principal.WindowsBuiltInRole]::Administrator)
$evidence.host.current_user_is_administrator = $isAdministrator
$evidence.host.ambient_path_prerequisites = [ordered]@{
    python = (Get-CommandPresence "python.exe") -or (Get-CommandPresence "python3.exe")
    python_launcher = Get-CommandPresence "py.exe"
    uv = Get-CommandPresence "uv.exe"
    docker = Get-CommandPresence "docker.exe"
    postgresql = Get-CommandPresence "pg_ctl.exe"
}

try {
    if ($CleanWindowsEvidence -and (
        $env:GITHUB_ACTIONS -ne "true" -or
        $env:RUNNER_ENVIRONMENT -ne "github-hosted" -or
        $env:RUNNER_OS -ne "Windows" -or
        $env:RUNNER_ARCH -ne "X64" -or
        $env:GITHUB_SHA -ne $ExpectedSourceRevision
    )) {
        throw "clean_windows_evidence_requires_exact_sha_github_hosted_windows_x64"
    }
    if ($isAdministrator) { throw "smoke_requires_non_admin_user" }
    if ($layout -notin @("shared", "split")) { throw "unknown_runtime_layout" }
    if ([string]$bundleManifest.source_tree_dirty -ne "False") { throw "runtime_source_tree_dirty" }
    if ($ExpectedSourceRevision -and [string]$bundleManifest.source_revision -ne $ExpectedSourceRevision) {
        throw "runtime_source_revision_mismatch"
    }
    $manifestChecksumPath = Join-Path $BundleRoot "runtime-manifest.json.sha256"
    $manifestChecksum = (Get-Content -LiteralPath $manifestChecksumPath -Raw).Split([char[]]@(" ", "`t", "`r", "`n"), [StringSplitOptions]::RemoveEmptyEntries)
    if ($manifestChecksum.Count -ne 2 -or $manifestChecksum[1] -cne "runtime-manifest.json" -or
        $manifestChecksum[0] -cne (Get-Sha256Hex (Join-Path $BundleRoot "runtime-manifest.json"))) {
        throw "runtime_manifest_checksum_mismatch"
    }
    $evidence.checks.runtime_manifest_checksum_valid = $true
    if ($layout -eq "shared") {
        $httpExe = Join-Path $BundleRoot "threads-runtime\threads-runtime.exe"
        $httpPrefix = @("http")
        $migratePrefix = @("migrate")
        $schedulerPrefix = @("scheduler")
    } else {
        $httpExe = Join-Path $BundleRoot "threads-http\threads-http.exe"
        $httpPrefix = @("http")
        $migratePrefix = @("migrate")
        $schedulerPrefix = @()
    }
    $schedulerExe = if ($layout -eq "shared") {
        $httpExe
    } else {
        Join-Path $BundleRoot "threads-scheduler\threads-scheduler.exe"
    }
    foreach ($exe in @($httpExe, $schedulerExe, (Join-Path $postgresBin "initdb.exe"), (Join-Path $postgresBin "pg_ctl.exe"), (Join-Path $postgresBin "psql.exe"))) {
        if (-not (Test-Path -LiteralPath $exe)) { throw "bundle_file_missing" }
    }

    Clear-RuntimeEnvironment
    $env:PATH = "$postgresBin;$env:SystemRoot\System32"
    $evidence.host.sanitized_path_entries = @("bundled-postgresql/bin", "Windows/System32")
    $evidence.host.runtime_bundle_root = $BundleRoot
    $toolInventory = [ordered]@{}
    $hostToolNames = @("python.exe", "python3.exe", "py.exe", "uv.exe", "docker.exe")
    foreach ($toolName in ($hostToolNames + @("pg_ctl.exe"))) {
        $toolInventory[$toolName] = Get-ResolvedApplicationEvidence $toolName
    }
    $evidence.host.sanitized_path_tool_inventory = $toolInventory
    $resolvedHostTools = @($hostToolNames | Where-Object { $toolInventory[$_].resolved })
    $evidence.host.sanitized_path_missing_python_uv_docker = $resolvedHostTools.Count -eq 0
    if (-not $evidence.host.sanitized_path_missing_python_uv_docker) { throw "sanitized_path_leaked_build_tool" }
    $expectedPgCtl = [System.IO.Path]::GetFullPath((Join-Path $postgresBin "pg_ctl.exe"))
    $resolvedPgCtl = [string]$toolInventory["pg_ctl.exe"].path
    $evidence.checks.runtime_uses_only_packaged_postgresql = (
        $toolInventory["pg_ctl.exe"].resolved -and
        [string]::Equals(
            $resolvedPgCtl,
            $expectedPgCtl,
            [System.StringComparison]::OrdinalIgnoreCase
        )
    )
    if (-not $evidence.checks.runtime_uses_only_packaged_postgresql) { throw "sanitized_path_leaked_host_postgresql" }

    $runtimeVersion = & $httpExe version 2>$null
    if ($LASTEXITCODE -ne 0 -or $runtimeVersion -notmatch "Python 3\.14\.") {
        throw "packaged_python_version_check_failed"
    }
    $evidence.checks.packaged_python_version = $runtimeVersion

    $dataRoot = [Environment]::GetFolderPath([Environment+SpecialFolder]::LocalApplicationData)
    $localRoot = Join-Path $dataRoot ("ThreadsDesktopDx03Smoke\" + [guid]::NewGuid().ToString("N"))
    New-Item -ItemType Directory -Path $localRoot -Force | Out-Null
    Restrict-TreeToCurrentUser $localRoot
    $evidence.checks.private_data_acl_current_user_only = $true
    $dataDirectory = Join-Path $localRoot "postgres-data"
    $postgresLog = Join-Path $localRoot "postgres.redacted.log"
    $protectedCredential = Join-Path $localRoot "database-credential.dpapi"
    $secretFile = Join-Path $localRoot "initdb-password.transient"

    $randomBytes = [byte[]]::new(32)
    $randomGenerator = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try { $randomGenerator.GetBytes($randomBytes) }
    finally { $randomGenerator.Dispose() }
    $databasePassword = [BitConverter]::ToString($randomBytes).Replace("-", "").ToLowerInvariant()
    $plainBytes = [System.Text.Encoding]::UTF8.GetBytes($databasePassword)
    $protectedValue = [Convert]::ToBase64String([ThreadsCurrentUserDpapi]::Protect($plainBytes))
    [System.IO.File]::WriteAllText($protectedCredential, $protectedValue, [System.Text.UTF8Encoding]::new($false))
    $protectedBytes = [Convert]::FromBase64String((Get-Content -LiteralPath $protectedCredential -Raw))
    $unprotectedBytes = [ThreadsCurrentUserDpapi]::Unprotect($protectedBytes)
    $unprotected = [System.Text.Encoding]::UTF8.GetString($unprotectedBytes)
    if ($unprotected -cne $databasePassword) { throw "dpapi_credential_round_trip_failed" }
    $evidence.checks.dpapi_current_user_credential_round_trip = $true
    [System.IO.File]::WriteAllText($secretFile, $databasePassword, [System.Text.UTF8Encoding]::new($false))
    Restrict-TreeToCurrentUser $localRoot

    Invoke-Bundled (Join-Path $postgresBin "initdb.exe") @(
        "--pgdata=$dataDirectory", "--username=threads_local", "--auth=scram-sha-256",
        "--pwfile=$secretFile", "--encoding=UTF8", "--locale=C"
    ) "initdb"
    Remove-Item -LiteralPath $secretFile -Force
    $secretFile = $null

    $listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, 0)
    $listener.Start()
    $port = ([System.Net.IPEndPoint]$listener.LocalEndpoint).Port
    $listener.Stop()
    $databaseUrl = "postgresql+asyncpg://threads_local:$databasePassword@127.0.0.1:$port/postgres"
    $env:THREADS_PLATFORM_DATABASE_URL = $databaseUrl
    $env:PGPASSWORD = $databasePassword
    $postgresOptions = "-h 127.0.0.1 -p $port -c listen_addresses=127.0.0.1"
    Invoke-Bundled (Join-Path $postgresBin "pg_ctl.exe") @(
        "--pgdata=$dataDirectory", "--log=$postgresLog", "--options=$postgresOptions", "--wait", "start"
    ) "postgres-start"
    $postgresStarted = $true
    if ((Invoke-Psql "SHOW listen_addresses") -ne "127.0.0.1") { throw "postgres_not_loopback_only" }
    $evidence.checks.postgres_loopback_only = $true

    Invoke-Bundled $httpExe ($migratePrefix + @()) "migration-first"
    $firstRevision = Invoke-Psql "SELECT version_num FROM alembic_version"
    if (-not $firstRevision) { throw "migration_revision_missing" }
    Invoke-Bundled $httpExe ($migratePrefix + @()) "migration-repeat"
    $secondRevision = Invoke-Psql "SELECT version_num FROM alembic_version"
    if ($secondRevision -ne $firstRevision) { throw "repeat_migration_changed_revision" }
    $evidence.checks.migration_idempotent = $true

    $listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, 0)
    $listener.Start()
    $httpPort = ([System.Net.IPEndPoint]$listener.LocalEndpoint).Port
    $listener.Stop()
    $httpStartedAt = [DateTime]::UtcNow
    $apiProcess = Start-BackgroundRuntime $httpExe ($httpPrefix + @("--host", "127.0.0.1", "--port", "$httpPort")) "http"
    $schedulerProcess = Start-BackgroundRuntime $schedulerExe $schedulerPrefix "scheduler"
    $deadline = [DateTime]::UtcNow.AddSeconds(40)
    while ([DateTime]::UtcNow -lt $deadline) {
        if ($apiProcess.HasExited) { throw "packaged_http_exited_before_ready" }
        if ($schedulerProcess.HasExited) { throw "packaged_scheduler_exited_during_startup" }
        try {
            $request = [System.Net.HttpWebRequest]::Create("http://127.0.0.1:$httpPort/health")
            $request.Proxy = $null
            $request.Timeout = 2000
            $response = $request.GetResponse()
            $httpStatus = [int]$response.StatusCode
            $response.Close()
            if ($httpStatus -eq 200) {
                break
            }
        } catch {
            Start-Sleep -Milliseconds 500
        }
    }
    if (-not $httpStatus) { throw "packaged_http_health_timeout" }
    $evidence.timings_ms.http_health_ready = [int]([DateTime]::UtcNow - $httpStartedAt).TotalMilliseconds
    Start-Sleep -Seconds 3
    if ($schedulerProcess.HasExited) { throw "packaged_scheduler_did_not_remain_running" }
    $evidence.checks.http_health_status = $httpStatus
    $evidence.checks.scheduler_process_started = $true

    Stop-Process -Id $apiProcess.Id -Force
    Stop-Process -Id $schedulerProcess.Id -Force
    $apiProcess.WaitForExit()
    $schedulerProcess.WaitForExit()
    $apiProcess = $null
    $schedulerProcess = $null
    Invoke-Bundled (Join-Path $postgresBin "pg_ctl.exe") @(
        "--pgdata=$dataDirectory", "--mode=fast", "--wait", "stop"
    ) "postgres-stop-first"
    $postgresStarted = $false
    Invoke-Bundled (Join-Path $postgresBin "pg_ctl.exe") @(
        "--pgdata=$dataDirectory", "--log=$postgresLog", "--options=$postgresOptions", "--wait", "start"
    ) "postgres-start-again"
    $postgresStarted = $true
    if ((Invoke-Psql "SELECT version_num FROM alembic_version") -ne $firstRevision) {
        throw "database_state_did_not_persist_after_restart"
    }
    $evidence.checks.postgres_state_survives_restart = $true

    Invoke-Psql "UPDATE alembic_version SET version_num = 'unknown_revision_for_smoke'" | Out-Null
    $incompatibleLog = Join-Path $localRoot "migration-incompatible.raw.log"
    $safeIncompatibleLog = Join-Path $EvidenceDirectory "migration-incompatible.redacted.log"
    $migrationArguments = @($migratePrefix | ForEach-Object { '"' + $_ + '"' })
    $incompatibleProcess = Start-Process -FilePath $httpExe `
        -ArgumentList ($migrationArguments -join " ") -PassThru -WindowStyle Hidden
    $incompatibleDeadline = [DateTime]::UtcNow.AddMinutes(2)
    while (-not $incompatibleProcess.HasExited -and [DateTime]::UtcNow -lt $incompatibleDeadline) {
        Start-Sleep -Milliseconds 100
    }
    if (-not $incompatibleProcess.HasExited) {
        Stop-Process -Id $incompatibleProcess.Id -Force -ErrorAction SilentlyContinue
        throw "migration_incompatible_timeout"
    }
    $incompatibleProcess.WaitForExit()
    $incompatibleProcess.Refresh()
    $incompatibleExit = $incompatibleProcess.ExitCode
    $incompatibleProcess.Dispose()
    [System.IO.File]::WriteAllText(
        $incompatibleLog,
        "Unknown revision migration process exit code: $incompatibleExit`n",
        [System.Text.UTF8Encoding]::new($false)
    )
    [System.IO.File]::WriteAllText(
        $safeIncompatibleLog,
        [System.IO.File]::ReadAllText($incompatibleLog),
        [System.Text.UTF8Encoding]::new($false)
    )
    Remove-Item -LiteralPath $incompatibleLog -Force -ErrorAction SilentlyContinue
    $safeLogs.Add($safeIncompatibleLog)
    if ($incompatibleExit -eq 0) { throw "migration_accepted_unknown_revision" }
    if ((Invoke-Psql "SELECT version_num FROM alembic_version") -ne "unknown_revision_for_smoke") {
        throw "incompatible_migration_mutated_schema_version"
    }
    $evidence.checks.incompatible_revision_refused_without_rewrite = $true
    Invoke-Psql "UPDATE alembic_version SET version_num = '$firstRevision'" | Out-Null

    $localSmokeStatus = "PASS"
} catch {
    $safeFailureText = ConvertTo-RedactedRuntimeLog ([string]$_.Exception.Message)
    $primaryFailureCode = [regex]::Replace($safeFailureText, "[^A-Za-z0-9_.-]", "_")
} finally {
    if ($apiProcess -and -not $apiProcess.HasExited) { Stop-Process -Id $apiProcess.Id -Force -ErrorAction SilentlyContinue }
    if ($schedulerProcess -and -not $schedulerProcess.HasExited) { Stop-Process -Id $schedulerProcess.Id -Force -ErrorAction SilentlyContinue }
    if ($localRoot -and (Test-Path -LiteralPath (Join-Path $localRoot "postgres-data"))) {
        $stopData = Join-Path $localRoot "postgres-data"
        $stopArguments = @(
            "--pgdata=$stopData", "--mode=fast", "--wait", "stop"
        ) | ForEach-Object { '"' + $_ + '"' }
        $stopProcess = Start-Process -FilePath (Join-Path $postgresBin "pg_ctl.exe") `
            -ArgumentList ($stopArguments -join " ") -PassThru -WindowStyle Hidden `
            -ErrorAction SilentlyContinue
        if ($stopProcess) {
            $stopDeadline = [DateTime]::UtcNow.AddSeconds(30)
            while (-not $stopProcess.HasExited -and [DateTime]::UtcNow -lt $stopDeadline) {
                Start-Sleep -Milliseconds 100
            }
            if (-not $stopProcess.HasExited) { Stop-Process -Id $stopProcess.Id -Force -ErrorAction SilentlyContinue }
            $stopProcess.Dispose()
        }
    }
    $redactionVerified = $false
    $runtimeLogsCreated =
        (@($rawLogs | Where-Object { Test-Path -LiteralPath $_ }).Count -gt 0) -or
        (@($safeLogs | Where-Object { Test-Path -LiteralPath $_ }).Count -gt 0) -or
        ($postgresLog -and (Test-Path -LiteralPath $postgresLog))
    try {
        if ($localRoot) {
            Save-RedactedLogs
        }
        if ($localRoot -and $postgresLog -and (Test-Path -LiteralPath $postgresLog)) {
            $postgresContent = ConvertTo-RedactedRuntimeLog ([System.IO.File]::ReadAllText($postgresLog))
            $safePostgresLog = Join-Path $EvidenceDirectory "postgresql-startup.redacted.log"
            [System.IO.File]::WriteAllText($safePostgresLog, $postgresContent, [System.Text.UTF8Encoding]::new($false))
            $safeLogs.Add($safePostgresLog)
            Remove-Item -LiteralPath $postgresLog -Force
        }
        if ($localRoot) {
            $incompatibleRaw = Join-Path $localRoot "migration-incompatible.raw.log"
            $incompatibleSafe = Join-Path $EvidenceDirectory "migration-incompatible.redacted.log"
            if ((Test-Path -LiteralPath $incompatibleRaw) -and -not (Test-Path -LiteralPath $incompatibleSafe)) {
                $incompatibleText = ConvertTo-RedactedRuntimeLog ([System.IO.File]::ReadAllText($incompatibleRaw))
                [System.IO.File]::WriteAllText($incompatibleSafe, $incompatibleText, [System.Text.UTF8Encoding]::new($false))
                $safeLogs.Add($incompatibleSafe)
                Remove-Item -LiteralPath $incompatibleRaw -Force -ErrorAction SilentlyContinue
            }
            if ($secretFile -and (Test-Path -LiteralPath $secretFile)) { Remove-Item -LiteralPath $secretFile -Force }
            $credentialPath = Join-Path $localRoot "database-credential.dpapi"
            if (Test-Path -LiteralPath $credentialPath) { Remove-Item -LiteralPath $credentialPath -Force }
        }
        $remainingRawLogs = @($rawLogs | Where-Object { Test-Path -LiteralPath $_ })
        $existingSafeLogs = @($safeLogs | Where-Object { Test-Path -LiteralPath $_ })
        $runtimeLogsCreated = $runtimeLogsCreated -or $existingSafeLogs.Count -gt 0
        $redactionVerified = $runtimeLogsCreated -and
            $remainingRawLogs.Count -eq 0 -and
            $existingSafeLogs.Count -gt 0
        foreach ($safeLog in $existingSafeLogs) {
            $content = [System.IO.File]::ReadAllText($safeLog)
            if (($databaseUrl -and $content.Contains($databaseUrl)) -or
                ($databasePassword -and $content.Contains($databasePassword)) -or
                [regex]::IsMatch($content, "(?i)(password|database_url)\s*[:=]\s*(?!<REDACTED>)[^\s]+") -or
                [regex]::IsMatch($content, "(?i)(authorization\s*[:=]\s*Bearer\s+)(?!<REDACTED>)[^\s]+") -or
                [regex]::IsMatch($content, "(?i)\bBearer\s+(?!<REDACTED>)[A-Za-z0-9._~+/-]{24,}") -or
                [regex]::IsMatch($content, "(?i)postgres(?:ql)?(?:\+\w+)?://[^\s:@/]+:[^@\s/]+@")) {
                $redactionVerified = $false
            }
        }
    } catch {
        $runtimeLogsCreated = $runtimeLogsCreated -or
            (@($rawLogs | Where-Object { Test-Path -LiteralPath $_ }).Count -gt 0) -or
            (@($safeLogs | Where-Object { Test-Path -LiteralPath $_ }).Count -gt 0) -or
            ($postgresLog -and (Test-Path -LiteralPath $postgresLog))
        if ($runtimeLogsCreated) {
            $redactionVerified = $false
            $logScrubFailureCode = "runtime_log_scrub_verification_failed"
        }
    }
    if ($runtimeLogsCreated) {
        if (-not $redactionVerified) {
            $localSmokeStatus = "FAIL"
            if (-not $logScrubFailureCode) {
                $logScrubFailureCode = "runtime_log_scrub_verification_failed"
            }
            if (-not $primaryFailureCode) { $primaryFailureCode = $logScrubFailureCode }
        }
        $logScrubStatus = if ($redactionVerified) { "PASS" } else { "FAIL" }
    } else {
        $logScrubFailureCode = $null
        $logScrubStatus = "NOT_RUN"
    }
    if (-not $runtimeLogsCreated -and $localSmokeStatus -eq "PASS") {
        $localSmokeStatus = "FAIL"
        $primaryFailureCode = "runtime_logs_missing"
    }
    if ($localRoot) { Remove-Item -LiteralPath $localRoot -Recurse -Force -ErrorAction SilentlyContinue }
    $env:PATH = $pathBefore
    foreach ($key in $savedRuntimeEnvironment.Keys) { Set-Item -LiteralPath "Env:$key" -Value $savedRuntimeEnvironment[$key] }
    $evidence.status = $localSmokeStatus
    $evidence.primary_failure_code = $primaryFailureCode
    $evidence.failure_code = $primaryFailureCode
    $evidence.log_scrub_status = $logScrubStatus
    $evidence.log_scrub_failure_code = $logScrubFailureCode
    $evidence.checks["redacted_runtime_logs"] = if ($logScrubStatus -eq "PASS") {
        $true
    } elseif ($logScrubStatus -eq "FAIL") {
        $false
    } else {
        $null
    }
    $evidence.clean_windows_runner_status = $cleanVmStatus
    if ($CleanWindowsEvidence) {
        $evidence.clean_windows_runner_status = if ($localSmokeStatus -eq "PASS") { "PASS" } else { "BLOCKER" }
    }
    $evidence.host.sanitized_path_missing_python_uv_docker = [bool]$evidence.host.sanitized_path_missing_python_uv_docker
    $evidence.redacted_logs = @($safeLogs | Where-Object { Test-Path -LiteralPath $_ } | ForEach-Object {
        [ordered]@{
            name = [System.IO.Path]::GetFileName($_)
            size_bytes = (Get-Item -LiteralPath $_).Length
            sha256 = Get-Sha256Hex $_
        }
    })
    [System.IO.File]::WriteAllText(
        $EvidencePath,
        ($evidence | ConvertTo-Json -Depth 8),
        [System.Text.UTF8Encoding]::new($false)
    )
}

if ($localSmokeStatus -ne "PASS") {
    throw "DX-03 runtime smoke failed: $primaryFailureCode"
}
if ($CleanWindowsEvidence) {
    Write-Output "DX-03 GitHub-hosted Windows x64 runtime smoke PASS. Evidence: $EvidencePath"
} else {
    Write-Output "DX-03 local runtime smoke PASS; clean Windows runner evidence remains BLOCKER. Evidence: $EvidencePath"
}
