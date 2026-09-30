[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string] $ReleaseDirectory,

    [string] $ControlPlaneUrl,

    [string] $DisplayName = "Windows Worker",

    [string] $AgentVersion = "0.1.0",

    [ValidateRange(1, 1000)]
    [int] $MaxConcurrentJobs = 1,

    [ValidateRange(1, 1000)]
    [int] $MaxBrowserSessions = 1,

    [switch] $EnableFeedBrowse,

    [switch] $EnableThreadOpen,

    [switch] $EnableProfileOpen,

    [switch] $EnableMediaLocalUpload,

    [switch] $PromptForEnrollment,

    [switch] $WindowsServiceCheck
)

$ErrorActionPreference = "Stop"
$serviceName = "ThreadsOperationsWorker"
$serviceAccount = "NT AUTHORITY\LocalService"
$programData = [Environment]::GetFolderPath([Environment+SpecialFolder]::CommonApplicationData)
$programFiles = [Environment]::GetFolderPath([Environment+SpecialFolder]::ProgramFiles)
if ([string]::IsNullOrWhiteSpace($programData)) {
    throw "ProgramData is unavailable."
}

$releasePath = [IO.Path]::GetFullPath($ReleaseDirectory)
$immutableReleaseRoot = [IO.Path]::GetFullPath((Join-Path $programFiles "ThreadsWorker\releases"))
if (!$releasePath.StartsWith($immutableReleaseRoot.TrimEnd('\') + '\', [StringComparison]::OrdinalIgnoreCase)) {
    throw "ReleaseDirectory must point inside Program Files ThreadsWorker releases."
}
$executablePath = Join-Path $releasePath "threads-worker.exe"
if (!(Test-Path -LiteralPath $executablePath -PathType Leaf)) {
    throw "The immutable release executable is missing."
}
$releaseItem = Get-Item -LiteralPath $releasePath -Force
if ($releaseItem.Attributes -band [IO.FileAttributes]::ReparsePoint) {
    throw "ReleaseDirectory must not be a junction or reparse point."
}
$releaseAcl = Get-Acl -LiteralPath $releasePath
$releaseWriteMask = [int64](
    [Security.AccessControl.FileSystemRights]::WriteData -bor
    [Security.AccessControl.FileSystemRights]::AppendData -bor
    [Security.AccessControl.FileSystemRights]::WriteExtendedAttributes -bor
    [Security.AccessControl.FileSystemRights]::WriteAttributes -bor
    [Security.AccessControl.FileSystemRights]::Delete -bor
    [Security.AccessControl.FileSystemRights]::DeleteSubdirectoriesAndFiles -bor
    [Security.AccessControl.FileSystemRights]::ChangePermissions -bor
    [Security.AccessControl.FileSystemRights]::TakeOwnership
)
$broadWriterSids = @("S-1-5-19", "S-1-5-11", "S-1-1-0", "S-1-5-32-545")
foreach ($rule in $releaseAcl.Access) {
    try {
        $ruleSid = $rule.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value
    } catch {
        continue
    }
    if (
        $ruleSid -in $broadWriterSids -and
        $rule.AccessControlType -eq [Security.AccessControl.AccessControlType]::Allow -and
        (([int64] $rule.FileSystemRights -band $releaseWriteMask) -ne 0)
    ) {
        throw "LocalService or a broad principal can write the immutable release."
    }
}

if ($WindowsServiceCheck) {
    if ($PromptForEnrollment) { throw "Service-check mode cannot enroll a Worker." }
    $dataRoot = Join-Path $programData ("ThreadsOperations-SCMCheck-" + [guid]::NewGuid().ToString("N"))
    $serviceMode = "--windows-service-check"
} else {
    if (![string]::IsNullOrWhiteSpace($ControlPlaneUrl)) {
        $parsedControlPlaneUrl = $null
        if (
            ![uri]::TryCreate($ControlPlaneUrl, [UriKind]::Absolute, [ref] $parsedControlPlaneUrl) -or
            $parsedControlPlaneUrl.Scheme -ne "https" -or
            [string]::IsNullOrWhiteSpace($parsedControlPlaneUrl.Host) -or
            ![string]::IsNullOrEmpty($parsedControlPlaneUrl.UserInfo) -or
            ![string]::IsNullOrEmpty($parsedControlPlaneUrl.Query) -or
            ![string]::IsNullOrEmpty($parsedControlPlaneUrl.Fragment)
        ) {
            throw "ControlPlaneUrl must be an HTTPS URL without credentials, query, or fragment."
        }
    }
    $dataRoot = Join-Path $programData "ThreadsOperations"
    $serviceMode = "--windows-service"
}

$serviceRegistryPath = "SYSTEM\CurrentControlSet\Services\$serviceName"
$existingService = [Microsoft.Win32.Registry]::LocalMachine.OpenSubKey($serviceRegistryPath, $true)
$existingEnvironmentEntries = @()
if ($null -ne $existingService) {
    $existingImagePath = [string] $existingService.GetValue("ImagePath", "")
    $existingObjectName = [string] $existingService.GetValue("ObjectName", "")
    if ($WindowsServiceCheck) {
        $existingService.Dispose()
        throw "A service-check instance is already installed. Uninstall it before creating another."
    }
    if (
        $existingObjectName -ne $serviceAccount -or
        $existingImagePath -notmatch '(?i)threads-worker\.exe'
    ) {
        $existingService.Dispose()
        throw "An unrelated service already uses the Worker service name."
    }
    if ($existingImagePath -match '(?i)--windows-service-check') {
        $existingService.Dispose()
        throw "Remove the service-check instance before installing production service mode."
    }
    $existingEnvironmentEntries = @($existingService.GetValue("Environment", @()))
    $oldDataRoot = $existingEnvironmentEntries |
        Where-Object { $_ -like "THREADS_WORKER_DATA_ROOT=*" } |
        Select-Object -First 1
    if ($null -eq $oldDataRoot) {
        $existingService.Dispose()
        throw "The existing service data root is unknown. Refusing an unsafe identity change."
    }
    if ($oldDataRoot.Substring("THREADS_WORKER_DATA_ROOT=".Length) -ne $dataRoot) {
        $existingService.Dispose()
        throw "The existing service data root changed. Identity continuity would be unsafe."
    }
    $allowedNames = @(
        "THREADS_WORKER_DATA_ROOT",
        "THREADS_WORKER_CONTROL_PLANE_URL",
        "THREADS_WORKER_DISPLAY_NAME",
        "THREADS_WORKER_AGENT_VERSION",
        "THREADS_WORKER_MAX_CONCURRENT_JOBS",
        "THREADS_WORKER_MAX_BROWSER_SESSIONS",
        "THREADS_WORKER_FEED_BROWSE_ENABLED",
        "THREADS_WORKER_THREAD_OPEN_ENABLED",
        "THREADS_WORKER_PROFILE_OPEN_ENABLED",
        "THREADS_WORKER_MEDIA_LOCAL_UPLOAD_ENABLED"
    )
    foreach ($entry in $existingEnvironmentEntries) {
        $name = ($entry -split "=", 2)[0]
        if ($name -notin $allowedNames -or $entry -match '[\r\n\0]') {
            $existingService.Dispose()
            throw "The existing service environment is not safe to preserve."
        }
    }
    $runningService = Get-Service -Name $serviceName -ErrorAction Stop
    if ($runningService.Status.ToString() -ne "Stopped") {
        $existingService.Dispose()
        throw "Drain and stop the existing service before changing its release path."
    }
    $existingService.Dispose()
    $isUpdate = $true
} else {
    $isUpdate = $false
}
if (!$isUpdate -and !$WindowsServiceCheck -and [string]::IsNullOrWhiteSpace($ControlPlaneUrl)) {
    throw "ControlPlaneUrl is required for a production service."
}
if ($isUpdate) {
    $configurationArguments = @(
        "ControlPlaneUrl",
        "DisplayName",
        "AgentVersion",
        "MaxConcurrentJobs",
        "MaxBrowserSessions",
        "EnableFeedBrowse",
        "EnableThreadOpen",
        "EnableProfileOpen",
        "EnableMediaLocalUpload",
        "PromptForEnrollment"
    )
    if ($configurationArguments | Where-Object { $PSBoundParameters.ContainsKey($_) }) {
        throw "An update preserves service configuration. Change non-secret settings separately after review."
    }
}

New-Item -ItemType Directory -Path $dataRoot -Force | Out-Null
$managedDirectories = @("worker", "profiles", "media", "journal", "cache", "logs", "updates", "bootstrap")
foreach ($directoryName in $managedDirectories) {
    New-Item -ItemType Directory -Path (Join-Path $dataRoot $directoryName) -Force | Out-Null
}
foreach ($directoryPath in @($dataRoot) + @($managedDirectories | ForEach-Object { Join-Path $dataRoot $_ })) {
    $item = Get-Item -LiteralPath $directoryPath -Force
    if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
        throw "The service data root contains an unsafe reparse point."
    }
}

$localServiceSid = [Security.Principal.SecurityIdentifier]::new("S-1-5-19")
$systemSid = [Security.Principal.SecurityIdentifier]::new("S-1-5-18")
$administratorsSid = [Security.Principal.SecurityIdentifier]::new("S-1-5-32-544")
$inheritance = [Security.AccessControl.InheritanceFlags]::ContainerInherit -bor
    [Security.AccessControl.InheritanceFlags]::ObjectInherit
$propagation = [Security.AccessControl.PropagationFlags]::None
$allow = [Security.AccessControl.AccessControlType]::Allow
foreach ($directoryPath in @($dataRoot) + @($managedDirectories | ForEach-Object { Join-Path $dataRoot $_ })) {
    $acl = Get-Acl -LiteralPath $directoryPath
    $acl.SetAccessRuleProtection($true, $false)
    foreach ($existingRule in @($acl.Access)) {
        [void] $acl.RemoveAccessRuleSpecific($existingRule)
    }
    $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new(
        $localServiceSid,
        [Security.AccessControl.FileSystemRights]::Modify,
        $inheritance,
        $propagation,
        $allow
    ))
    $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new(
        $systemSid,
        [Security.AccessControl.FileSystemRights]::FullControl,
        $inheritance,
        $propagation,
        $allow
    ))
    $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new(
        $administratorsSid,
        [Security.AccessControl.FileSystemRights]::FullControl,
        $inheritance,
        $propagation,
        $allow
    ))
    Set-Acl -LiteralPath $directoryPath -AclObject $acl
}

$allowedEnvironment = @("THREADS_WORKER_SERVICE_CHECK_ROOT=$dataRoot")
if ($isUpdate) {
    $allowedEnvironment = $existingEnvironmentEntries
} elseif (!$WindowsServiceCheck) {
    $allowedEnvironment = @(
        "THREADS_WORKER_DATA_ROOT=$dataRoot",
        "THREADS_WORKER_CONTROL_PLANE_URL=$ControlPlaneUrl",
        "THREADS_WORKER_DISPLAY_NAME=$DisplayName",
        "THREADS_WORKER_AGENT_VERSION=$AgentVersion",
        "THREADS_WORKER_MAX_CONCURRENT_JOBS=$MaxConcurrentJobs",
        "THREADS_WORKER_MAX_BROWSER_SESSIONS=$MaxBrowserSessions",
        "THREADS_WORKER_FEED_BROWSE_ENABLED=$($EnableFeedBrowse.IsPresent.ToString().ToLowerInvariant())",
        "THREADS_WORKER_THREAD_OPEN_ENABLED=$($EnableThreadOpen.IsPresent.ToString().ToLowerInvariant())",
        "THREADS_WORKER_PROFILE_OPEN_ENABLED=$($EnableProfileOpen.IsPresent.ToString().ToLowerInvariant())",
        "THREADS_WORKER_MEDIA_LOCAL_UPLOAD_ENABLED=$($EnableMediaLocalUpload.IsPresent.ToString().ToLowerInvariant())"
    )
}
foreach ($entry in $allowedEnvironment) {
    if ($entry.Length -gt 4096 -or $entry -match '[\r\n\0]') {
        throw "Service configuration contains an invalid value."
    }
}

$imagePath = '"{0}" {1}' -f $executablePath, $serviceMode
if ($isUpdate) {
    & sc.exe config $serviceName "binPath= $imagePath" "obj= $serviceAccount" "start= auto" | Out-Null
} else {
    & sc.exe create $serviceName "binPath= $imagePath" "start= auto" "obj= $serviceAccount" | Out-Null
}
if ($LASTEXITCODE -ne 0) { throw "SCM service registration failed." }
& sc.exe description $serviceName "Threads Operations Worker Agent" | Out-Null
if ($LASTEXITCODE -ne 0) { throw "SCM service description update failed." }

if (!$isUpdate) {
    $serviceKey = [Microsoft.Win32.Registry]::LocalMachine.OpenSubKey($serviceRegistryPath, $true)
    if ($null -eq $serviceKey) { throw "The SCM service registry entry is unavailable." }
    try {
        $serviceKey.SetValue(
            "Environment",
            [string[]] $allowedEnvironment,
            [Microsoft.Win32.RegistryValueKind]::MultiString
        )
    } finally {
        $serviceKey.Dispose()
    }
}

if ($PromptForEnrollment) {
    if ($WindowsServiceCheck) { throw "Service-check mode cannot enroll a Worker." }
    $bootstrapPath = Join-Path (Join-Path $dataRoot "bootstrap") "enrollment-code"
    if (Test-Path -LiteralPath $bootstrapPath) {
        throw "An enrollment bootstrap file already exists. Resolve it before prompting again."
    }
    $secureCode = Read-Host -AsSecureString "One-time Worker enrollment code"
    $codePointer = [IntPtr]::Zero
    $codeBytes = $null
    try {
        $codePointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureCode)
        $plainCode = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($codePointer)
        if ($plainCode -notmatch '^[A-Za-z0-9_-]{1,256}$') {
            throw "The enrollment code format is invalid."
        }
        $codeBytes = [Text.Encoding]::ASCII.GetBytes($plainCode + "`n")
        $fileStream = [IO.File]::Open(
            $bootstrapPath,
            [IO.FileMode]::CreateNew,
            [IO.FileAccess]::Write,
            [IO.FileShare]::None
        )
        try {
            $fileStream.Write($codeBytes, 0, $codeBytes.Length)
            $fileStream.Flush($true)
        } finally {
            $fileStream.Dispose()
        }
        [Array]::Clear($codeBytes, 0, $codeBytes.Length)
    } finally {
        if ($codeBytes -is [byte[]]) { [Array]::Clear($codeBytes, 0, $codeBytes.Length) }
        if ($codePointer -ne [IntPtr]::Zero) {
            [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($codePointer)
        }
        $secureCode.Dispose()
        Remove-Variable plainCode -ErrorAction SilentlyContinue
    }
}

if ($WindowsServiceCheck) {
    Write-Output "SCM_SERVICE_CHECK_INSTALLED"
} elseif ($isUpdate) {
    Write-Output "WORKER_SERVICE_UPDATED"
} else {
    Write-Output "WORKER_SERVICE_INSTALLED"
}
