param(
    [string]$ConfigPath = (Join-Path $env:LOCALAPPDATA "25th-second\runtime.json"),
    [string]$PythonPath,
    [string]$ProjectRoot,
    [int]$Port = 8625
)

$ErrorActionPreference = "Stop"
$runtimeRoot = Split-Path -Parent $ConfigPath
$logs = Join-Path $runtimeRoot "logs"
New-Item -ItemType Directory -Path $logs -Force | Out-Null
function Write-LauncherLog([string]$Message) {
    Add-Content -LiteralPath (Join-Path $logs "launcher.log") -Encoding UTF8 -Value "$(Get-Date -Format o) $Message"
}

$mutex = [System.Threading.Mutex]::new($false, "Local\25thSecondLocalSite8625")
$ownsMutex = $false
Write-LauncherLog "Launch requested; pid=$PID"
try {
    try { $ownsMutex = $mutex.WaitOne(0) }
    catch [System.Threading.AbandonedMutexException] { $ownsMutex = $true }
    if (-not $ownsMutex) { Write-LauncherLog 'Existing site mutex held; duplicate launch skipped.'; exit 0 }
    if ($PythonPath -and $ProjectRoot) {
        $config = [pscustomobject]@{ python_path = $PythonPath; project_root = $ProjectRoot; port = $Port }
    }
    else {
        $config = Get-Content -LiteralPath $ConfigPath -Raw | ConvertFrom-Json
    }
    if (-not (Test-Path -LiteralPath $config.python_path -PathType Leaf)) {
        throw "Configured Python does not exist: $($config.python_path)"
    }
    if (-not (Test-Path -LiteralPath (Join-Path $config.project_root 'v2\frontend\server.py') -PathType Leaf)) {
        throw "Project path is unavailable: $($config.project_root)"
    }
    Write-LauncherLog "Supervisor started; port=$($config.port); pid=$PID"
    $env:COURTVISION_ENV = 'production'
    if ($config.PSObject.Properties.Name -contains 'public_root') { $env:COURTVISION_PUBLIC_PUBLISH_ROOT = $config.public_root }
    if ($config.PSObject.Properties.Name -contains 'report_game_ids') { $env:COURTVISION_REPORT_GAME_IDS = ($config.report_game_ids -join ',') }
    $env:COURTVISION_SCHEDULE_SYNC_ENABLED = if ($config.PSObject.Properties.Name -contains 'schedule_sync_enabled' -and -not $config.schedule_sync_enabled) { 'false' } else { 'true' }
    Write-LauncherLog "Schedule sync enabled=$env:COURTVISION_SCHEDULE_SYNC_ENABLED"
    $retrySeconds = 3
    while ($true) {
        $process = $null
        $startedAt = Get-Date
        try {
            $listener = Get-NetTCPConnection -State Listen -LocalPort $config.port -ErrorAction SilentlyContinue
            if ($listener) {
                Write-LauncherLog "Port $($config.port) is already in use; waiting without terminating its owner."
                Start-Sleep -Seconds 30
                continue
            }
            $stamp = Get-Date -Format 'yyyyMMdd-HHmmss-fff'
            $process = Start-Process -FilePath $config.python_path -ArgumentList @(
                '-u', '-m', 'v2.frontend.server', '--host', '127.0.0.1', '--port', [string]$config.port
            ) -WorkingDirectory $config.project_root -WindowStyle Hidden -PassThru `
                -RedirectStandardOutput (Join-Path $logs "$stamp-server.log") `
                -RedirectStandardError (Join-Path $logs "$stamp-error.log")
            Write-LauncherLog "Server started; pid=$($process.Id)"
            $process.WaitForExit()
            Write-LauncherLog "Server exited; code=$($process.ExitCode); restarting."
            if (((Get-Date) - $startedAt).TotalSeconds -ge 60) { $retrySeconds = 3 }
        }
        catch {
            Write-LauncherLog "Launch failed: $($_.Exception.Message)"
        }
        finally {
            if ($null -ne $process) { $process.Dispose() }
        }
        Start-Sleep -Seconds $retrySeconds
        $retrySeconds = [Math]::Min(60, $retrySeconds * 2)
    }
}
catch {
    Write-LauncherLog "Supervisor failed: $($_.Exception.Message)"
    exit 1
}
finally {
    if ($ownsMutex) { $mutex.ReleaseMutex() }
    $mutex.Dispose()
}
