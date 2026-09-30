[CmdletBinding()]
param(
    [ValidateRange(1, 3600)]
    [int] $StopWaitSeconds = 300
)

$ErrorActionPreference = "Stop"
$serviceName = "ThreadsOperationsWorker"
$service = Get-Service -Name $serviceName -ErrorAction SilentlyContinue
if ($null -eq $service) {
    Write-Output "WORKER_SERVICE_NOT_INSTALLED"
    exit 0
}

$serviceKeyPath = "SYSTEM\CurrentControlSet\Services\$serviceName"
$serviceKey = [Microsoft.Win32.Registry]::LocalMachine.OpenSubKey($serviceKeyPath)
if ($null -eq $serviceKey) { throw "The SCM service registry entry is unavailable." }
try {
    $objectName = [string] $serviceKey.GetValue("ObjectName", "")
    $imagePath = [string] $serviceKey.GetValue("ImagePath", "")
} finally {
    $serviceKey.Dispose()
}
if (
    $objectName -ne "NT AUTHORITY\LocalService" -or
    $imagePath -notmatch '(?i)threads-worker\.exe'
) {
    throw "The registered service does not match the Threads Worker service contract."
}

if ($service.Status.ToString() -ne "Stopped") {
    if ($service.Status.ToString() -ne "StopPending") {
        & sc.exe stop $serviceName | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "SCM stop request failed; the service was not removed." }
    }
    $deadline = [DateTime]::UtcNow.AddSeconds($StopWaitSeconds)
    do {
        Start-Sleep -Seconds 1
        $service.Refresh()
        if ($service.Status.ToString() -eq "Stopped") { break }
    } while ([DateTime]::UtcNow -lt $deadline)
    if ($service.Status.ToString() -ne "Stopped") {
        throw "The Worker has not confirmed graceful STOPPED state; service and data were preserved."
    }
}

& sc.exe delete $serviceName | Out-Null
if ($LASTEXITCODE -ne 0) { throw "SCM service removal failed." }
$deleteDeadline = [DateTime]::UtcNow.AddSeconds(30)
do {
    $remainingService = Get-Service -Name $serviceName -ErrorAction SilentlyContinue
    if ($null -eq $remainingService) { break }
    Start-Sleep -Seconds 1
} while ([DateTime]::UtcNow -lt $deleteDeadline)
if ($null -ne $remainingService) { throw "SCM service removal is pending; durable data was preserved." }
Write-Output "WORKER_SERVICE_UNINSTALLED_DATA_PRESERVED"
