[CmdletBinding()]
param(
    [Parameter()]
    [string]$BundleRoot,
    [Parameter()]
    [switch]$KeepArtifacts
)

$ErrorActionPreference = "Stop"
$PSNativeCommandUseErrorActionPreference = $false

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
if ([string]::IsNullOrWhiteSpace($BundleRoot)) {
    $BundleRoot = Join-Path $repoRoot "build/windows-desktop/candidates/shared"
}
$BundleRoot = [System.IO.Path]::GetFullPath($BundleRoot)
$postgresBin = Join-Path $BundleRoot "postgresql/bin"
$initdb = Join-Path $postgresBin "initdb.exe"
$pgCtl = Join-Path $postgresBin "pg_ctl.exe"
$createdb = Join-Path $postgresBin "createdb.exe"

foreach ($required in @($initdb, $pgCtl, $createdb)) {
    if (-not (Test-Path -LiteralPath $required -PathType Leaf)) {
        throw @"
windows_local_preflight_requires_bundled_postgresql
Missing: $required

Build the accepted shared candidate first:
  uv sync --locked --no-dev --group packaging
  uv run --locked --no-dev --group packaging python packaging/windows_desktop/build_runtime.py --layout shared

Then rerun:
  powershell -NoProfile -File scripts/windows_local_preflight.ps1
"@
    }
}

function Invoke-Checked([string]$Executable, [string[]]$Arguments, [string]$FailureCode) {
    & $Executable @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw ("{0}_exit_{1}" -f $FailureCode, $LASTEXITCODE)
    }
}

function Get-FreeLoopbackPort {
    $listener = [System.Net.Sockets.TcpListener]::new(
        [System.Net.IPAddress]::Loopback,
        0
    )
    $listener.Start()
    try {
        return ([System.Net.IPEndPoint]$listener.LocalEndpoint).Port
    } finally {
        $listener.Stop()
    }
}

$runId = [guid]::NewGuid().ToString("N")
$shortRunId = $runId.Substring(0, 12)
$localAppData = [Environment]::GetFolderPath([Environment+SpecialFolder]::LocalApplicationData)
if ([string]::IsNullOrWhiteSpace($localAppData)) {
    throw "windows_local_preflight_local_app_data_unavailable"
}
$runRoot = Join-Path $localAppData "TOCI/$shortRunId"
$tempRoot = Join-Path $runRoot "temp"
$pytestBaseTemp = Join-Path $runRoot "pytest"
$dataRoot = Join-Path $runRoot "postgres-data"
$postgresLog = Join-Path $runRoot "postgres.log"
$passwordFile = Join-Path $runRoot "postgres-password.txt"
$databaseName = "threads_repo_gate_test"
$port = Get-FreeLoopbackPort
$databasePassword = "localci" + [guid]::NewGuid().ToString("N")
$databaseUrl = "postgresql+asyncpg://threads_platform:$databasePassword@127.0.0.1:$port/$databaseName"

New-Item -ItemType Directory -Path $tempRoot -Force | Out-Null
[System.IO.File]::WriteAllText(
    $passwordFile,
    $databasePassword,
    [System.Text.UTF8Encoding]::new($false)
)

$previous = [ordered]@{
    TEMP = $env:TEMP
    TMP = $env:TMP
    PGPASSWORD = $env:PGPASSWORD
    THREADS_PLATFORM_DATABASE_URL = $env:THREADS_PLATFORM_DATABASE_URL
    THREADS_PLATFORM_TEST_DATABASE_URL = $env:THREADS_PLATFORM_TEST_DATABASE_URL
}

$postgresStarted = $false
$cleanupFailed = $false
$passed = $false

try {
    Push-Location $repoRoot

    $env:TEMP = $tempRoot
    $env:TMP = $tempRoot
    $env:PGPASSWORD = $databasePassword
    $env:THREADS_PLATFORM_DATABASE_URL = $databaseUrl
    $env:THREADS_PLATFORM_TEST_DATABASE_URL = $databaseUrl

    Invoke-Checked $initdb @(
        "--pgdata=$dataRoot",
        "--username=threads_platform",
        "--auth=scram-sha-256",
        "--pwfile=$passwordFile",
        "--encoding=UTF8",
        "--locale=C"
    ) "local_preflight_initdb_failed"
    Remove-Item -LiteralPath $passwordFile -Force -ErrorAction SilentlyContinue

    Invoke-Checked $pgCtl @(
        "start",
        "-D", $dataRoot,
        "-l", $postgresLog,
        "-o", "-h 127.0.0.1 -p $port",
        "-w"
    ) "local_preflight_postgres_start_failed"
    $postgresStarted = $true

    Invoke-Checked $createdb @(
        "-h", "127.0.0.1",
        "-p", [string]$port,
        "-U", "threads_platform",
        $databaseName
    ) "local_preflight_createdb_failed"

    Write-Host "LOCAL_CI database=$databaseName host=127.0.0.1 port=$port"
    Write-Host "LOCAL_CI temp_root=$tempRoot"
    Write-Host "LOCAL_CI pytest_basetemp=$pytestBaseTemp"
    Write-Host "LOCAL_CI CurrentUser Root mutation test is excluded from unattended preflight; use the accepted manual interactive PR #128 contract for that test."

    Invoke-Checked "uv" @("sync", "--locked") "local_preflight_uv_sync_failed"
    Invoke-Checked "uv" @("run", "--locked", "playwright", "install", "chromium") "local_preflight_playwright_install_failed"
    Invoke-Checked "uv" @("run", "--locked", "ruff", "check", ".") "local_preflight_ruff_check_failed"
    Invoke-Checked "uv" @("run", "--locked", "ruff", "format", "--check", ".") "local_preflight_ruff_format_failed"
    Invoke-Checked "uv" @("run", "--locked", "pyright") "local_preflight_pyright_failed"
    Invoke-Checked "uv" @("run", "--locked", "alembic", "upgrade", "head") "local_preflight_alembic_upgrade_failed"
    Invoke-Checked "uv" @("run", "--locked", "alembic", "check") "local_preflight_alembic_check_failed"

    Invoke-Checked "uv" @(
        "run", "--locked", "pytest", "-ra",
        "--basetemp=$pytestBaseTemp",
        "--deselect=tests/unit/test_worker_control_client_httpx2.py::test_control_client_trusts_only_windows_current_user_root_ca"
    ) "local_preflight_pytest_failed"

    $passed = $true
    Write-Host "LOCAL_CI_RESULT=PASS"
} finally {
    if ($postgresStarted) {
        try {
            & $pgCtl stop -D $dataRoot -m fast -w
            if ($LASTEXITCODE -ne 0) {
                Write-Warning "Disposable PostgreSQL fast stop returned exit code $LASTEXITCODE; retrying immediate stop."
                & $pgCtl stop -D $dataRoot -m immediate -w
                if ($LASTEXITCODE -ne 0) {
                    $cleanupFailed = $true
                    Write-Warning "Disposable PostgreSQL immediate stop returned exit code $LASTEXITCODE"
                }
            }
        } catch {
            Write-Warning "Disposable PostgreSQL fast stop raised: $($_.Exception.Message); retrying immediate stop."
            try {
                & $pgCtl stop -D $dataRoot -m immediate -w
                if ($LASTEXITCODE -ne 0) {
                    $cleanupFailed = $true
                    Write-Warning "Disposable PostgreSQL immediate stop returned exit code $LASTEXITCODE"
                }
            } catch {
                $cleanupFailed = $true
                Write-Warning "Disposable PostgreSQL immediate cleanup failed: $($_.Exception.Message)"
            }
        }
    }

    Pop-Location -ErrorAction SilentlyContinue

    $env:TEMP = $previous.TEMP
    $env:TMP = $previous.TMP
    $env:PGPASSWORD = $previous.PGPASSWORD
    $env:THREADS_PLATFORM_DATABASE_URL = $previous.THREADS_PLATFORM_DATABASE_URL
    $env:THREADS_PLATFORM_TEST_DATABASE_URL = $previous.THREADS_PLATFORM_TEST_DATABASE_URL

    Remove-Item -LiteralPath $passwordFile -Force -ErrorAction SilentlyContinue

    if ($passed -and -not $cleanupFailed -and -not $KeepArtifacts) {
        Remove-Item -LiteralPath $runRoot -Recurse -Force -ErrorAction SilentlyContinue
    } else {
        Write-Host "LOCAL_CI_ARTIFACTS=$runRoot"
    }

    if ($passed -and $cleanupFailed) {
        throw "windows_local_preflight_postgres_cleanup_failed"
    }
}
