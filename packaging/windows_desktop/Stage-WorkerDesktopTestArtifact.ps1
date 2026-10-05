[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string] $DesktopExecutable,

    [Parameter(Mandatory = $true)]
    [string] $Destination
)

$ErrorActionPreference = "Stop"
$desktopName = "threads-desktop.exe"
$taskHelperName = "Manage-ThreadsWorkerTask.ps1"

function Stop-ArtifactStage([string] $Code) {
    [Console]::Error.WriteLine($Code)
    exit 1
}

try {
    $source = Get-Item -LiteralPath $DesktopExecutable -Force
    $helperSource = Get-Item -LiteralPath (
        Join-Path $PSScriptRoot "..\windows_worker\$taskHelperName"
    ) -Force
    if ($source.PSIsContainer -or $source.Name -ne $desktopName -or
        ($source.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0 -or
        $helperSource.PSIsContainer -or
        ($helperSource.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
        Stop-ArtifactStage "worker_desktop_artifact_source_invalid"
    }

    $destinationPath = [IO.Path]::GetFullPath($Destination)
    if (Test-Path -LiteralPath $destinationPath) {
        $destinationItem = Get-Item -LiteralPath $destinationPath -Force
        if (!$destinationItem.PSIsContainer -or
            ($destinationItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0 -or
            @(Get-ChildItem -LiteralPath $destinationPath -Force).Count -ne 0) {
            Stop-ArtifactStage "worker_desktop_artifact_destination_invalid"
        }
    } else {
        New-Item -ItemType Directory -Path $destinationPath | Out-Null
    }

    Copy-Item -LiteralPath $source.FullName `
        -Destination (Join-Path $destinationPath $desktopName)
    Copy-Item -LiteralPath $helperSource.FullName `
        -Destination (Join-Path $destinationPath $taskHelperName)

    $stagedExecutable = Get-Item -LiteralPath (Join-Path $destinationPath $desktopName) -Force
    $stagedHelper = Get-Item -LiteralPath (Join-Path $destinationPath $taskHelperName) -Force
    $stagedNames = @(Get-ChildItem -LiteralPath $destinationPath -Force | ForEach-Object Name | Sort-Object)
    $expectedNames = @($desktopName, $taskHelperName) | Sort-Object
    if ($stagedExecutable.PSIsContainer -or $stagedHelper.PSIsContainer -or
        ($stagedExecutable.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0 -or
        ($stagedHelper.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0 -or
        (Compare-Object $stagedNames $expectedNames)) {
        Stop-ArtifactStage "worker_desktop_artifact_output_invalid"
    }

    [ordered]@{
        schema = "threads-worker-desktop-test-artifact-v1"
        executable = $desktopName
        resource_directory = "."
        task_helper = $taskHelperName
    } | ConvertTo-Json -Compress | ForEach-Object { [Console]::Out.WriteLine($_) }
} catch {
    Stop-ArtifactStage "worker_desktop_artifact_stage_failed"
}
