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
$testTempRoot = if ([string]::IsNullOrWhiteSpace($env:RUNNER_TEMP)) {
    [IO.Path]::GetTempPath()
} else {
    $env:RUNNER_TEMP
}
$localAppData = Join-Path $testTempRoot "worker-task-localappdata-$testId"
$testDataRoot = Join-Path $localAppData "ThreadsOperations-TaskSmoke-$testId"
$legacyDefaultDataRoot = Join-Path $localAppData "ThreadsOperations"
$hostDirectory = Join-Path $testDataRoot "host"
$hostConfigPath = Join-Path $hostDirectory "worker-host.json"
$identityDirectory = Join-Path $testDataRoot "worker"
$identityPath = Join-Path $identityDirectory "worker_id"
$previousLocalAppData = [Environment]::GetEnvironmentVariable("LOCALAPPDATA")
$previousWorkerDataRoot = [Environment]::GetEnvironmentVariable("THREADS_WORKER_DATA_ROOT")
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

function Write-TestHostConfig($Config) {
    [IO.File]::WriteAllText(
        $hostConfigPath,
        ($Config | ConvertTo-Json -Compress),
        [Text.UTF8Encoding]::new($false)
    )
}

function Assert-TaskSettings($Task, [string] $ExpectedUserSid) {
    Assert-Condition ($Task.Principal.LogonType.ToString() -eq "Interactive") "TASK_LOGON_TYPE_INVALID"
    Assert-Condition ($Task.Principal.RunLevel.ToString() -eq "Limited") "TASK_RUN_LEVEL_INVALID"
    Assert-Condition ($Task.Settings.MultipleInstances.ToString() -eq "IgnoreNew") "TASK_INSTANCE_POLICY_INVALID"
    Assert-Condition ($Task.Settings.AllowHardTerminate -eq $false) "TASK_HARD_TERMINATE_ENABLED"
    $taskXml = [xml] (Export-ScheduledTask -TaskName $Task.TaskName)
    $executionLimit = [string] $taskXml.Task.Settings.ExecutionTimeLimit
    Assert-Condition ($executionLimit -eq "PT0S") "TASK_LIMIT_INVALID"
    Assert-Condition ($Task.Settings.StopIfGoingOnBatteries -eq $false) "TASK_BATTERY_STOP_ENABLED"
    Assert-Condition (@($Task.Triggers).Count -eq 1) "TASK_TRIGGER_COUNT_INVALID"
    Assert-Condition ($Task.Triggers[0].CimClass.CimClassName -eq "MSFT_TaskLogonTrigger") "TASK_TRIGGER_INVALID"
    Assert-Condition ((Convert-AccountToSid $Task.Principal.UserId) -eq $ExpectedUserSid) "TASK_PRINCIPAL_INVALID"
    Assert-Condition ((Convert-AccountToSid $Task.Triggers[0].UserId) -eq $ExpectedUserSid) "TASK_TRIGGER_USER_INVALID"
}

try {
    New-Item -ItemType Directory -Path $localAppData -Force | Out-Null
    $env:LOCALAPPDATA = $localAppData
    $env:THREADS_WORKER_DATA_ROOT = $testDataRoot
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
        display_name = "Task Scheduler smoke worker"
        max_concurrent_jobs = 1
        max_browser_sessions = 1
    }
    Write-TestHostConfig $hostConfig

    $programFilesRoots = @(
        [Environment]::GetFolderPath([Environment+SpecialFolder]::ProgramFiles),
        [Environment]::GetFolderPath([Environment+SpecialFolder]::ProgramFilesX86)
    ) | Where-Object { ![string]::IsNullOrWhiteSpace($_) } | Select-Object -Unique
    foreach ($programFilesRoot in $programFilesRoots) {
        $hostConfig["data_root"] = Join-Path $programFilesRoot "ThreadsWorker-test-data-$testId"
        Write-TestHostConfig $hostConfig
        $programFilesResult = Invoke-TaskManager @(
            "-Operation", "Install", "-ReleaseDirectory", $firstRelease,
            "-HostConfigPath", $hostConfigPath
        ) 2
        Assert-Condition (
            $programFilesResult -eq "THREADS_WORKER_TASK_OPERATION_REJECTED_DATA_ROOT_POLICY"
        ) "PROGRAM_FILES_DATA_ROOT_ACCEPTED"
        Assert-Condition (!(Test-Path -LiteralPath $hostConfig["data_root"])) "PROGRAM_FILES_DATA_ROOT_CREATED"
    }
    $hostConfig.Remove("data_root") | Out-Null
    Write-TestHostConfig $hostConfig

    $syntheticWorkerId = [guid]::NewGuid().ToString()
    $legacyWorkerDirectory = Join-Path $legacyDefaultDataRoot "worker"
    New-Item -ItemType Directory -Path $legacyWorkerDirectory -Force | Out-Null
    [IO.File]::WriteAllText(
        (Join-Path $legacyWorkerDirectory "worker_id"),
        "$([guid]::NewGuid())`nENROLLED`n",
        [Text.ASCIIEncoding]::new()
    )
    [IO.File]::WriteAllText(
        $identityPath,
        "$syntheticWorkerId`nENROLLED`n",
        [Text.ASCIIEncoding]::new()
    )
    $mismatchResult = Invoke-TaskManager @(
        "-Operation", "Install", "-ReleaseDirectory", $firstRelease,
        "-HostConfigPath", $hostConfigPath
    ) 2
    Assert-Condition (
        $mismatchResult -eq "THREADS_WORKER_TASK_OPERATION_REJECTED_DATA_ROOT_CONFIG_REQUIRED"
    ) "ENV_ONLY_ENROLLED_DATA_ROOT_ACCEPTED"
    Assert-Condition (!(Get-ScheduledTask -TaskName $productionTaskName -ErrorAction SilentlyContinue)) `
        "ENV_ONLY_DATA_ROOT_REGISTERED_TASK"

    $hostConfig["data_root"] = $testDataRoot
    Write-TestHostConfig $hostConfig
    $env:THREADS_WORKER_DATA_ROOT = Join-Path $programFiles "ThreadsWorker-env-data-$testId"

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

    $hostConfig.Remove("data_root") | Out-Null
    Write-TestHostConfig $hostConfig
    $updateMissingDataRootResult = Invoke-TaskManager @(
        "-Operation", "Update", "-ReleaseDirectory", $secondRelease,
        "-ExpectedProjectVersion", $manifest.project_version, "-ExpectedGitSha", $manifest.git_sha,
        "-ConfirmDurableDrainOffline"
    ) 2
    Assert-Condition (
        $updateMissingDataRootResult -eq "THREADS_WORKER_TASK_OPERATION_REJECTED_DATA_ROOT_CONFIG_REQUIRED"
    ) "UPDATE_ACCEPTED_ENV_ONLY_DATA_ROOT"
    $taskAfterRejectedUpdate = Get-ScheduledTask -TaskName $productionTaskName -ErrorAction Stop
    Assert-Condition (
        $taskAfterRejectedUpdate.Actions[0].Execute -eq (Join-Path $firstRelease "threads-worker.exe")
    ) "UPDATE_CHANGED_TASK_WITHOUT_EXPLICIT_DATA_ROOT"
    $hostConfig["data_root"] = $testDataRoot
    Write-TestHostConfig $hostConfig

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
    $smokeOutput = Join-Path $testTempRoot "worker-task-output-$testId.txt"
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
    if ($null -eq $previousLocalAppData) {
        Remove-Item Env:LOCALAPPDATA -ErrorAction SilentlyContinue
    } else {
        $env:LOCALAPPDATA = $previousLocalAppData
    }
    if ($null -eq $previousWorkerDataRoot) {
        Remove-Item Env:THREADS_WORKER_DATA_ROOT -ErrorAction SilentlyContinue
    } else {
        $env:THREADS_WORKER_DATA_ROOT = $previousWorkerDataRoot
    }
    $fullTestLocalAppData = [IO.Path]::GetFullPath($localAppData)
    $expectedTempPrefix = [IO.Path]::GetFullPath($testTempRoot).TrimEnd('\') + '\'
    if ($fullTestLocalAppData.StartsWith($expectedTempPrefix, [StringComparison]::OrdinalIgnoreCase) -and
        $fullTestLocalAppData -like '*worker-task-localappdata-*' -and
        (Test-Path -LiteralPath $fullTestLocalAppData)) {
        Remove-Item -LiteralPath $fullTestLocalAppData -Recurse -Force
    }
    Remove-Item -LiteralPath (Join-Path $testTempRoot "worker-task-output-$testId.txt") `
        -Force -ErrorAction SilentlyContinue
}
