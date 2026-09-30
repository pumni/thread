[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string] $PackageRoot
)

$ErrorActionPreference = "Stop"
$productionTaskName = "ThreadsPlatformWorker"
$smokeTaskName = "ThreadsWorkerPackageSmoke-$([guid]::NewGuid().ToString('N'))"
$packagePath = [IO.Path]::GetFullPath($PackageRoot)
$packageExecutable = Join-Path $packagePath "threads-worker.exe"
$manifestPath = Join-Path $packagePath "BUILD-MANIFEST.json"
$manifest = Get-Content -Raw -LiteralPath $manifestPath | ConvertFrom-Json
$installScript = Join-Path $PSScriptRoot "Manage-ThreadsWorkerTask.ps1"
$programFiles = [Environment]::GetFolderPath([Environment+SpecialFolder]::ProgramFiles)
$releaseRoot = Join-Path $programFiles "ThreadsWorker\releases"
$testId = [guid]::NewGuid().ToString("N")
$firstRelease = Join-Path $releaseRoot "task-smoke-$testId-a"
$secondRelease = Join-Path $releaseRoot "task-smoke-$testId-b"
$localAppData = [Environment]::GetFolderPath([Environment+SpecialFolder]::LocalApplicationData)
$testDataRoot = Join-Path $localAppData "ThreadsOperations-TaskSmoke-$testId"
$hostDirectory = Join-Path $testDataRoot "host"
$hostConfigPath = Join-Path $hostDirectory "worker-host.json"
$identityDirectory = Join-Path $testDataRoot "worker"
$identityPath = Join-Path $identityDirectory "worker_id"
$productionTaskOwned = $false

function Assert-Condition([bool] $Condition, [string] $FailureCode) {
    if (!$Condition) { throw $FailureCode }
}

function Convert-AccountToSid([string] $AccountName) {
    if ($AccountName -match "^S-1-") {
        return [Security.Principal.SecurityIdentifier]::new($AccountName).Value
    }
    return ([Security.Principal.NTAccount]::new($AccountName)).Translate(
        [Security.Principal.SecurityIdentifier]
    ).Value
}

function Invoke-TaskManager([string[]] $Arguments, [int] $ExpectedExitCode = 0) {
    $powerShell = Join-Path $PSHOME "pwsh.exe"
    $result = @(& $powerShell -NoLogo -NoProfile -File $installScript @Arguments 2>&1)
    $exitCode = $LASTEXITCODE
    $joined = ($result | ForEach-Object { "$_" }) -join "`n"
    $operationIndex = [Array]::IndexOf($Arguments, "-Operation")
    $operationName = if ($operationIndex -ge 0 -and $operationIndex + 1 -lt $Arguments.Count) {
        [string] $Arguments[$operationIndex + 1]
    } else {
        "UNKNOWN"
    }
    $failureMatch = [regex]::Match(
        $joined,
        "THREADS_WORKER_TASK_OPERATION_REJECTED_([A-Z_]+)"
    )
    $failureStage = if ($failureMatch.Success) {
        $failureMatch.Groups[1].Value
    } else {
        "NO_SAFE_CODE"
    }
    Assert-Condition ($exitCode -eq $ExpectedExitCode) `
        ("TASK_MANAGER_{0}_EXIT_{1}_EXPECTED_{2}_{3}" -f `
            $operationName, $exitCode, $ExpectedExitCode, $failureStage)
    if ($ExpectedExitCode -eq 0 -and
        ($joined -match [regex]::Escape([Security.Principal.WindowsIdentity]::GetCurrent().Name) -or
            $joined -match [regex]::Escape([Security.Principal.WindowsIdentity]::GetCurrent().User.Value))) {
        throw "TASK_MANAGER_OUTPUT_EXPOSED_PRINCIPAL"
    }
    return $joined.Trim()
}

function Copy-TestRelease([string] $Destination) {
    New-Item -ItemType Directory -Path $Destination -Force | Out-Null
    Copy-Item -Path (Join-Path $packagePath "*") -Destination $Destination -Recurse -Force
}

function Assert-TaskSettings($Task, [string] $ExpectedUserSid) {
    Assert-Condition ($Task.Principal.LogonType.ToString() -eq "Interactive") "TASK_LOGON_TYPE_INVALID"
    Assert-Condition ($Task.Principal.RunLevel.ToString() -eq "Limited") "TASK_RUN_LEVEL_INVALID"
    Assert-Condition ($Task.Settings.MultipleInstances.ToString() -eq "IgnoreNew") "TASK_INSTANCE_POLICY_INVALID"
    Assert-Condition ($Task.Settings.AllowHardTerminate -eq $false) "TASK_HARD_TERMINATE_ENABLED"
    Assert-Condition ($Task.Settings.ExecutionTimeLimit -eq [TimeSpan]::Zero) "TASK_LIMIT_INVALID"
    Assert-Condition ($Task.Settings.StopIfGoingOnBatteries -eq $false) "TASK_BATTERY_STOP_ENABLED"
    Assert-Condition (@($Task.Triggers).Count -eq 1) "TASK_TRIGGER_COUNT_INVALID"
    Assert-Condition ($Task.Triggers[0].CimClass.CimClassName -eq "MSFT_TaskLogonTrigger") "TASK_TRIGGER_INVALID"
    Assert-Condition ((Convert-AccountToSid $Task.Principal.UserId) -eq $ExpectedUserSid) "TASK_PRINCIPAL_INVALID"
    Assert-Condition ((Convert-AccountToSid $Task.Triggers[0].UserId) -eq $ExpectedUserSid) "TASK_TRIGGER_USER_INVALID"
}

try {
    Import-Module ScheduledTasks -ErrorAction Stop
    Assert-Condition (Test-Path -LiteralPath $packageExecutable -PathType Leaf) "PACKAGE_EXECUTABLE_MISSING"
    Assert-Condition (Test-Path -LiteralPath $manifestPath -PathType Leaf) "PACKAGE_MANIFEST_MISSING"
    if (Get-ScheduledTask -TaskName $productionTaskName -ErrorAction SilentlyContinue) {
        throw "PRODUCTION_TASK_ALREADY_EXISTS"
    }
    $productionTaskOwned = $true
    Copy-TestRelease $firstRelease
    Copy-TestRelease $secondRelease

    New-Item -ItemType Directory -Path $hostDirectory -Force | Out-Null
    New-Item -ItemType Directory -Path $identityDirectory -Force | Out-Null
    $hostConfig = [ordered]@{
        schema = "threads-worker-host-v1"
        control_plane_url = "https://control.example.invalid"
        data_root = $testDataRoot
        display_name = "Task Scheduler smoke worker"
        max_concurrent_jobs = 1
        max_browser_sessions = 1
    }
    [IO.File]::WriteAllText(
        $hostConfigPath,
        ($hostConfig | ConvertTo-Json -Compress),
        [Text.UTF8Encoding]::new($false)
    )

    $syntheticWorkerId = [guid]::NewGuid().ToString()
    [IO.File]::WriteAllText(
        $identityPath,
        "$syntheticWorkerId`nPENDING`n",
        [Text.ASCIIEncoding]::new()
    )
    $pendingResult = Invoke-TaskManager @(
        "-Operation", "Install", "-ReleaseDirectory", $firstRelease,
        "-HostConfigPath", $hostConfigPath, "-ExpectedProjectVersion", $manifest.project_version,
        "-ExpectedGitSha", $manifest.git_sha
    ) 2
    Assert-Condition (
        $pendingResult -eq "THREADS_WORKER_TASK_OPERATION_REJECTED_IDENTITY_VALIDATION"
    ) "PENDING_IDENTITY_ACCEPTED"
    Assert-Condition ($pendingResult -notmatch [regex]::Escape($syntheticWorkerId)) "PENDING_IDENTITY_LEAKED"

    Remove-Item -LiteralPath $identityPath -Force
    $missingResult = Invoke-TaskManager @(
        "-Operation", "Install", "-ReleaseDirectory", $firstRelease,
        "-HostConfigPath", $hostConfigPath
    ) 2
    Assert-Condition (
        $missingResult -eq "THREADS_WORKER_TASK_OPERATION_REJECTED_IDENTITY_VALIDATION"
    ) "MISSING_IDENTITY_ACCEPTED"

    [IO.File]::WriteAllText(
        $identityPath,
        "$syntheticWorkerId`nENROLLED`n",
        [Text.ASCIIEncoding]::new()
    )
    $currentIdentity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $currentUserSid = $currentIdentity.User.Value
    $installResult = Invoke-TaskManager @(
        "-Operation", "Install", "-ReleaseDirectory", $firstRelease,
        "-HostConfigPath", $hostConfigPath, "-ExpectedProjectVersion", $manifest.project_version,
        "-ExpectedGitSha", $manifest.git_sha
    )
    Assert-Condition ($installResult -eq "THREADS_WORKER_TASK_REGISTERED") "TASK_INSTALL_RESULT_INVALID"

    $productionTask = Get-ScheduledTask -TaskName $productionTaskName -ErrorAction Stop
    Assert-TaskSettings $productionTask $currentUserSid
    Assert-Condition ($productionTask.Actions[0].Execute -eq (Join-Path $firstRelease "threads-worker.exe")) "TASK_ACTION_INVALID"
    Assert-Condition ($productionTask.Actions[0].Arguments -eq ('--host-config "{0}"' -f $hostConfigPath)) "TASK_ARGUMENTS_INVALID"
    $productionXml = Export-ScheduledTask -TaskName $productionTaskName
    Assert-Condition ($productionXml -notmatch '<Password>|<LogonType>Password</LogonType>') "TASK_PASSWORD_PERSISTED"
    Assert-Condition ($productionXml -notmatch 'THREADS_WORKER_ENROLLMENT_CODE|access_token|private_key|proxy_credential') "TASK_SECRET_PERSISTED"

    $inspectResult = Invoke-TaskManager @("-Operation", "Inspect")
    Assert-Condition ($inspectResult -eq "THREADS_WORKER_TASK_READY") "TASK_INSPECT_RESULT_INVALID"

    $updateResult = Invoke-TaskManager @(
        "-Operation", "Update", "-ReleaseDirectory", $secondRelease,
        "-ExpectedProjectVersion", $manifest.project_version, "-ExpectedGitSha", $manifest.git_sha,
        "-ConfirmDurableDrainOffline"
    )
    Assert-Condition ($updateResult -eq "THREADS_WORKER_TASK_UPDATED") "TASK_UPDATE_RESULT_INVALID"
    $updatedTask = Get-ScheduledTask -TaskName $productionTaskName -ErrorAction Stop
    Assert-TaskSettings $updatedTask $currentUserSid
    Assert-Condition ($updatedTask.Actions[0].Execute -eq (Join-Path $secondRelease "threads-worker.exe")) "UPDATED_TASK_ACTION_INVALID"
    Assert-Condition (Test-Path -LiteralPath (Join-Path $firstRelease "threads-worker.exe")) "PREVIOUS_RELEASE_REMOVED"

    $uninstallResult = Invoke-TaskManager @("-Operation", "Uninstall", "-ConfirmDurableDrainOffline")
    Assert-Condition ($uninstallResult -eq "THREADS_WORKER_TASK_UNINSTALLED") "TASK_UNINSTALL_RESULT_INVALID"
    Assert-Condition (!(Get-ScheduledTask -TaskName $productionTaskName -ErrorAction SilentlyContinue)) "TASK_REMAINS_REGISTERED"
    Assert-Condition (Test-Path -LiteralPath $identityPath) "UNINSTALL_REMOVED_WORKER_DATA"
    Assert-Condition (Test-Path -LiteralPath (Join-Path $firstRelease "threads-worker.exe")) "UNINSTALL_REMOVED_RELEASE"

    $identityName = $currentIdentity.Name
    $smokeOutput = Join-Path $env:RUNNER_TEMP "worker-task-output-$testId.txt"
    $commandArguments = '/d /c ""{0}" --package-check > "{1}" 2>&1"' -f `
        $packageExecutable, $smokeOutput
    $smokeAction = New-ScheduledTaskAction `
        -Execute $env:ComSpec `
        -Argument $commandArguments `
        -WorkingDirectory $packagePath
    $smokeTrigger = New-ScheduledTaskTrigger -AtLogOn -User $identityName
    $smokePrincipal = New-ScheduledTaskPrincipal `
        -UserId $identityName `
        -LogonType Interactive `
        -RunLevel Limited
    $smokeSettings = New-ScheduledTaskSettingsSet `
        -MultipleInstances IgnoreNew `
        -ExecutionTimeLimit ([TimeSpan]::Zero) `
        -DisallowHardTerminate `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -StartWhenAvailable
    $smokeTask = New-ScheduledTask `
        -Action $smokeAction `
        -Trigger $smokeTrigger `
        -Principal $smokePrincipal `
        -Settings $smokeSettings
    Register-ScheduledTask -TaskName $smokeTaskName -InputObject $smokeTask | Out-Null
    $registeredSmokeTask = Get-ScheduledTask -TaskName $smokeTaskName -ErrorAction Stop
    Assert-TaskSettings $registeredSmokeTask $currentUserSid
    $smokeXml = Export-ScheduledTask -TaskName $smokeTaskName
    Assert-Condition ($smokeXml -notmatch '<Password>|<LogonType>Password</LogonType>') "SMOKE_TASK_PASSWORD_PERSISTED"

    $startTime = [DateTime]::Now
    Start-ScheduledTask -TaskName $smokeTaskName
    $deadline = [DateTime]::UtcNow.AddSeconds(90)
    do {
        Start-Sleep -Milliseconds 500
        $smokeState = (Get-ScheduledTask -TaskName $smokeTaskName).State.ToString()
        $smokeInfo = Get-ScheduledTaskInfo -TaskName $smokeTaskName
        if ($smokeState -eq "Ready" -and
            $smokeInfo.LastRunTime -ge $startTime -and
            (Test-Path -LiteralPath $smokeOutput -PathType Leaf)) {
            break
        }
    } while ([DateTime]::UtcNow -lt $deadline)
    Assert-Condition ($smokeState -eq "Ready") "SMOKE_TASK_DID_NOT_COMPLETE"
    Assert-Condition ($smokeInfo.LastTaskResult -eq 0) "SMOKE_TASK_EXITED_UNSUCCESSFULLY"
    $smokeOutputText = [IO.File]::ReadAllText($smokeOutput).Trim()
    Assert-Condition ($smokeOutputText -eq "WORKER_PACKAGE_CHECK_OK") "SMOKE_PACKAGE_CHECK_RESULT_INVALID"
    Write-Output "TASK_SCHEDULER_PACKAGE_SMOKE_OK"
} finally {
    if (Get-ScheduledTask -TaskName $smokeTaskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $smokeTaskName -Confirm:$false -ErrorAction SilentlyContinue | Out-Null
    }
    if ($productionTaskOwned -and
        (Get-ScheduledTask -TaskName $productionTaskName -ErrorAction SilentlyContinue)) {
        Unregister-ScheduledTask -TaskName $productionTaskName -Confirm:$false -ErrorAction SilentlyContinue | Out-Null
    }
    foreach ($path in @($firstRelease, $secondRelease)) {
        $fullPath = [IO.Path]::GetFullPath($path)
        $expectedPrefix = [IO.Path]::GetFullPath($releaseRoot).TrimEnd('\') + '\'
        if ($fullPath.StartsWith($expectedPrefix, [StringComparison]::OrdinalIgnoreCase) -and
            (Test-Path -LiteralPath $fullPath)) {
            Remove-Item -LiteralPath $fullPath -Recurse -Force
        }
    }
    $fullTestDataRoot = [IO.Path]::GetFullPath($testDataRoot)
    $expectedDataPrefix = [IO.Path]::GetFullPath($localAppData).TrimEnd('\') + '\'
    if ($fullTestDataRoot.StartsWith($expectedDataPrefix, [StringComparison]::OrdinalIgnoreCase) -and
        $fullTestDataRoot -like '*ThreadsOperations-TaskSmoke-*' -and
        (Test-Path -LiteralPath $fullTestDataRoot)) {
        Remove-Item -LiteralPath $fullTestDataRoot -Recurse -Force
    }
    Remove-Item -LiteralPath (Join-Path $env:RUNNER_TEMP "worker-task-output-$testId.txt") `
        -Force -ErrorAction SilentlyContinue
}
