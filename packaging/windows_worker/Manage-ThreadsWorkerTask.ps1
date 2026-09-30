[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("Install", "Inspect", "Update", "Uninstall")]
    [string] $Operation,

    [string] $ReleaseDirectory,

    [string] $HostConfigPath,

    [string] $ExpectedProjectVersion,

    [ValidatePattern("^[0-9a-f]{40}$")]
    [string] $ExpectedGitSha,

    [switch] $ConfirmDurableDrainOffline
)

$ErrorActionPreference = "Stop"
$taskName = "ThreadsPlatformWorker"
$releaseRoot = Join-Path ([Environment]::GetFolderPath([Environment+SpecialFolder]::ProgramFiles)) `
    "ThreadsWorker\releases"
$writeMask = [int64](
    [Security.AccessControl.FileSystemRights]::WriteData -bor
    [Security.AccessControl.FileSystemRights]::AppendData -bor
    [Security.AccessControl.FileSystemRights]::WriteExtendedAttributes -bor
    [Security.AccessControl.FileSystemRights]::WriteAttributes -bor
    [Security.AccessControl.FileSystemRights]::Delete -bor
    [Security.AccessControl.FileSystemRights]::DeleteSubdirectoriesAndFiles -bor
    [Security.AccessControl.FileSystemRights]::ChangePermissions -bor
    [Security.AccessControl.FileSystemRights]::TakeOwnership
)
$trustedReleaseWriterSids = @(
    "S-1-5-18",
    "S-1-5-32-544",
    "S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464"
)

function Get-CurrentInteractiveIdentity {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    if (!$identity.User -or [string]::IsNullOrWhiteSpace($identity.Name)) {
        throw "invalid principal"
    }
    if ($identity.IsSystem -or $identity.Name -match "^NT AUTHORITY\\(LOCAL SERVICE|NETWORK SERVICE)$") {
        throw "invalid principal"
    }
    return $identity
}

function Convert-AccountToSid([string] $AccountName) {
    try {
        if ($AccountName -match "^S-1-") {
            return [Security.Principal.SecurityIdentifier]::new($AccountName).Value
        }
        return ([Security.Principal.NTAccount]::new($AccountName)).Translate(
            [Security.Principal.SecurityIdentifier]
        ).Value
    } catch {
        throw "invalid principal"
    }
}

function Assert-ImmutableRelease([string] $Path) {
    if ([string]::IsNullOrWhiteSpace($Path)) { throw "invalid release" }
    $fullPath = [IO.Path]::GetFullPath($Path)
    $rootPath = [IO.Path]::GetFullPath($releaseRoot).TrimEnd('\') + '\'
    if (!$fullPath.StartsWith($rootPath, [StringComparison]::OrdinalIgnoreCase)) {
        throw "invalid release"
    }
    $release = Get-Item -LiteralPath $fullPath -Force
    if (!$release.PSIsContainer -or
        ($release.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "invalid release"
    }

    $manifestPath = Join-Path $fullPath "BUILD-MANIFEST.json"
    $executablePath = Join-Path $fullPath "threads-worker.exe"
    $internalPath = Join-Path $fullPath "_internal"
    if (!(Test-Path -LiteralPath $manifestPath -PathType Leaf) -or
        !(Test-Path -LiteralPath $executablePath -PathType Leaf) -or
        !(Test-Path -LiteralPath $internalPath -PathType Container)) {
        throw "invalid release"
    }
    $topLevel = @(Get-ChildItem -LiteralPath $fullPath -Force | ForEach-Object Name | Sort-Object)
    $expectedTopLevel = @("BUILD-MANIFEST.json", "_internal", "threads-worker.exe") | Sort-Object
    if (Compare-Object $topLevel $expectedTopLevel) { throw "invalid release" }

    try {
        $manifest = Get-Content -Raw -LiteralPath $manifestPath | ConvertFrom-Json
    } catch {
        throw "invalid release"
    }
    $expectedFields = @(
        "artifact_schema", "project_version", "git_sha", "python_version",
        "playwright_version", "pyinstaller_version", "target_os", "target_arch",
        "browser", "created_at_utc"
    ) | Sort-Object
    $actualFields = @($manifest.PSObject.Properties.Name | Sort-Object)
    if (Compare-Object $actualFields $expectedFields) { throw "invalid release" }
    if ($manifest.artifact_schema -ne "threads-worker-package-v1" -or
        $manifest.target_os -ne "windows" -or $manifest.target_arch -ne "x64" -or
        $manifest.browser -ne "chromium" -or
        $manifest.project_version -notmatch '^\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.-]+)?$' -or
        $manifest.git_sha -notmatch '^[0-9a-f]{40}$' -or
        $manifest.playwright_version -ne "1.63.0" -or
        ($ExpectedProjectVersion -and $manifest.project_version -ne $ExpectedProjectVersion) -or
        ($ExpectedGitSha -and $manifest.git_sha -ne $ExpectedGitSha)) {
        throw "invalid release"
    }

    $items = @($release) + @(Get-ChildItem -LiteralPath $fullPath -Force -Recurse)
    foreach ($item in $items) {
        if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "invalid release"
        }
        $acl = Get-Acl -LiteralPath $item.FullName
        foreach ($rule in $acl.Access) {
            if ($rule.AccessControlType -ne [Security.AccessControl.AccessControlType]::Allow -or
                ([int64] $rule.FileSystemRights -band $writeMask) -eq 0) {
                continue
            }
            try {
                $sid = $rule.IdentityReference.Translate(
                    [Security.Principal.SecurityIdentifier]
                ).Value
            } catch {
                throw "invalid release"
            }
            if ($sid -notin $trustedReleaseWriterSids) { throw "invalid release" }
        }
    }
    return $manifest
}

function Assert-EnrolledIdentity([string] $ConfigPath, [string] $ExecutablePath) {
    $fullConfigPath = [IO.Path]::GetFullPath($ConfigPath)
    $configFile = Get-Item -LiteralPath $fullConfigPath -Force
    if ($configFile.PSIsContainer -or
        ($configFile.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "invalid host config"
    }
    & $ExecutablePath --validate-host-config $fullConfigPath | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "invalid host config" }
    try {
        $config = Get-Content -Raw -LiteralPath $fullConfigPath | ConvertFrom-Json
    } catch {
        throw "invalid host config"
    }

    $localAppData = [Environment]::GetFolderPath([Environment+SpecialFolder]::LocalApplicationData)
    if ([string]::IsNullOrWhiteSpace($localAppData)) { throw "identity unavailable" }
    $dataRoot = if ($null -ne $config.data_root) {
        [IO.Path]::GetFullPath([string] $config.data_root)
    } else {
        Join-Path $localAppData "ThreadsOperations"
    }
    $dataRoot = $dataRoot.TrimEnd('\')
    $releasePrefix = [IO.Path]::GetFullPath($releaseRoot).TrimEnd('\') + '\'
    if ($dataRoot.StartsWith($releasePrefix, [StringComparison]::OrdinalIgnoreCase) -or
        $fullConfigPath.StartsWith($releasePrefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw "invalid host config"
    }
    $workerDirectory = Join-Path $dataRoot "worker"
    $markerPath = Join-Path $workerDirectory "worker_id"
    $workerItem = Get-Item -LiteralPath $workerDirectory -Force
    $markerItem = Get-Item -LiteralPath $markerPath -Force
    if (($workerItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0 -or
        ($markerItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "identity unavailable"
    }
    $marker = [IO.File]::ReadAllText($markerPath)
    $lines = $marker -split "`r?`n"
    if ($lines.Count -ne 3 -or
        $lines[0] -notmatch '^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$' -or
        $lines[1] -ne "ENROLLED" -or $lines[2] -ne "") {
        throw "identity not enrolled"
    }
    return $fullConfigPath
}

function New-WorkerTaskDefinition(
    [Security.Principal.WindowsIdentity] $Identity,
    [string] $ExecutablePath,
    [string] $ConfigPath,
    [string] $WorkingDirectory
) {
    $action = New-ScheduledTaskAction `
        -Execute $ExecutablePath `
        -Argument ('--host-config "{0}"' -f $ConfigPath) `
        -WorkingDirectory $WorkingDirectory
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $Identity.Name
    $principal = New-ScheduledTaskPrincipal `
        -UserId $Identity.Name `
        -LogonType Interactive `
        -RunLevel Limited
    $settings = New-ScheduledTaskSettingsSet `
        -MultipleInstances IgnoreNew `
        -ExecutionTimeLimit ([TimeSpan]::Zero) `
        -DisallowHardTerminate `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -StartWhenAvailable
    return New-ScheduledTask -Action $action -Trigger $trigger -Principal $principal -Settings $settings
}

function Assert-TaskContract($Task, [string] $ExpectedUserSid) {
    if ($Task.Principal.LogonType.ToString() -ne "Interactive" -or
        $Task.Principal.RunLevel.ToString() -ne "Limited" -or
        $Task.Settings.MultipleInstances.ToString() -ne "IgnoreNew" -or
        $Task.Settings.AllowHardTerminate -ne $false -or
        $Task.Settings.ExecutionTimeLimit -ne [TimeSpan]::Zero -or
        $Task.Settings.StopIfGoingOnBatteries -ne $false -or
        @($Task.Triggers).Count -ne 1 -or
        $Task.Triggers[0].CimClass.CimClassName -ne "MSFT_TaskLogonTrigger") {
        throw "invalid task"
    }
    $principalSid = Convert-AccountToSid $Task.Principal.UserId
    $triggerSid = Convert-AccountToSid $Task.Triggers[0].UserId
    if ($principalSid -ne $ExpectedUserSid -or $triggerSid -ne $ExpectedUserSid) {
        throw "invalid task"
    }
    if (@($Task.Actions).Count -ne 1 -or
        $Task.Actions[0].Arguments -notmatch '^--host-config "([^"\r\n]+)"$') {
        throw "invalid task"
    }
    $configPath = $Matches[1]
    $executablePath = [IO.Path]::GetFullPath($Task.Actions[0].Execute)
    $releasePath = [IO.Path]::GetFullPath((Split-Path -Parent $executablePath))
    $releasePathPrefix = [IO.Path]::GetFullPath($releaseRoot).TrimEnd('\') + '\'
    if ([IO.Path]::GetFileName($executablePath) -ne "threads-worker.exe" -or
        !$releasePath.StartsWith($releasePathPrefix, [StringComparison]::OrdinalIgnoreCase) -or
        !(Test-Path -LiteralPath $executablePath -PathType Leaf) -or
        ($Task.Actions[0].WorkingDirectory -ne $releasePath)) {
        throw "invalid task"
    }
    if (![IO.Path]::IsPathRooted($configPath)) { throw "invalid task" }
    $taskXml = Export-ScheduledTask -TaskName $taskName
    if ($taskXml -match '<LogonType>Password</LogonType>|<Password>' -or
        $taskXml -match 'THREADS_WORKER_ENROLLMENT_CODE|access_token|session_token|private_key|proxy_credential|credential_ref') {
        throw "invalid task"
    }
    return $configPath
}

$failureStage = "INITIALIZE"
try {
    $failureStage = "LOAD_TASK_SCHEDULER"
    Import-Module ScheduledTasks -ErrorAction Stop
    $failureStage = "READ_TASK"
    $task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    if ($Operation -eq "Inspect") {
        if ($null -eq $task) {
            Write-Output "THREADS_WORKER_TASK_NOT_REGISTERED"
            exit 0
        }
        try {
            $null = Assert-TaskContract $task (Convert-AccountToSid $task.Principal.UserId)
        } catch {
            Write-Output "THREADS_WORKER_TASK_INVALID"
            exit 0
        }
        if ($task.State.ToString() -eq "Running") {
            Write-Output "THREADS_WORKER_TASK_RUNNING"
        } elseif ($task.State.ToString() -eq "Ready") {
            Write-Output "THREADS_WORKER_TASK_READY"
        } else {
            Write-Output "THREADS_WORKER_TASK_NOT_READY"
        }
        exit 0
    }

    $failureStage = "VALIDATE_PRINCIPAL"
    $identity = Get-CurrentInteractiveIdentity
    $currentUserSid = $identity.User.Value
    if ($Operation -eq "Uninstall") {
        if (!$ConfirmDurableDrainOffline) { throw "drain confirmation required" }
        if ($null -eq $task) {
            Write-Output "THREADS_WORKER_TASK_NOT_REGISTERED"
            exit 0
        }
        $null = Assert-TaskContract $task $currentUserSid
        if ($task.State.ToString() -eq "Running") { throw "task is running" }
        Unregister-ScheduledTask -TaskName $taskName -Confirm:$false | Out-Null
        Write-Output "THREADS_WORKER_TASK_UNINSTALLED"
        exit 0
    }

    if ([string]::IsNullOrWhiteSpace($ReleaseDirectory)) { throw "release required" }
    $failureStage = "RELEASE_VALIDATION"
    $manifest = Assert-ImmutableRelease $ReleaseDirectory
    $fullReleasePath = [IO.Path]::GetFullPath($ReleaseDirectory)
    $executablePath = Join-Path $fullReleasePath "threads-worker.exe"
    if ($Operation -eq "Install") {
        if ($null -ne $task) { throw "task already registered" }
        if ([string]::IsNullOrWhiteSpace($HostConfigPath)) { throw "host config required" }
        $failureStage = "IDENTITY_VALIDATION"
        $fullConfigPath = Assert-EnrolledIdentity $HostConfigPath $executablePath
    } else {
        if (!$ConfirmDurableDrainOffline) { throw "drain confirmation required" }
        if ($null -eq $task) { throw "task is not registered" }
        if ($task.State.ToString() -eq "Running") { throw "task is running" }
        $failureStage = "EXISTING_TASK_VALIDATION"
        $existingConfigPath = Assert-TaskContract $task $currentUserSid
        $failureStage = "IDENTITY_VALIDATION"
        $fullConfigPath = Assert-EnrolledIdentity $existingConfigPath $executablePath
        if (![string]::IsNullOrWhiteSpace($HostConfigPath)) { throw "host config cannot change" }
    }

    $failureStage = "BUILD_TASK"
    $definition = New-WorkerTaskDefinition `
        $identity $executablePath $fullConfigPath $fullReleasePath
    $failureStage = "REGISTER_TASK"
    Register-ScheduledTask -TaskName $taskName -InputObject $definition -Force | Out-Null
    $failureStage = "VERIFY_TASK"
    $registeredTask = Get-ScheduledTask -TaskName $taskName -ErrorAction Stop
    $null = Assert-TaskContract $registeredTask $currentUserSid
    if ($Operation -eq "Install") {
        Write-Output "THREADS_WORKER_TASK_REGISTERED"
    } else {
        Write-Output "THREADS_WORKER_TASK_UPDATED"
    }
} catch {
    [Console]::Error.WriteLine(
        ("THREADS_WORKER_TASK_OPERATION_REJECTED_{0}" -f $failureStage)
    )
    exit 2
}
