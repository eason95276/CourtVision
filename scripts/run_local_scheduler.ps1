param(
    [Parameter(Mandatory = $true)][string]$PythonPath,
    [Parameter(Mandatory = $true)][string]$ProjectRoot,
    [Parameter(Mandatory = $true)][string]$Database,
    [Parameter(Mandatory = $true)][string]$ActivationReceipt,
    [switch]$EnableRealExecution,
    [string]$Data,
    [string]$RuntimeRoot,
    [string]$StopFile,
    [string]$StopAt,
    [switch]$PreventIdleSleep,
    [ValidateRange(1, 300)][int]$StopWaitSeconds = 8
)

$ErrorActionPreference = 'Stop'
$stopDeadline = if ($StopAt) { [DateTimeOffset]::Parse($StopAt) } else { $null }
if ($stopDeadline -and $stopDeadline -le [DateTimeOffset]::Now) { throw 'Scheduler stop deadline has already passed.' }
if ($PreventIdleSleep) {
    Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class CourtVisionSchedulerPower {
  [DllImport("kernel32.dll")] public static extern uint SetThreadExecutionState(uint flags);
}
'@
}

# Constructing the Python store may run migrations. Reject an unapproved launch first.
if (-not $EnableRealExecution) {
    throw 'Real execution is disabled. This launcher requires -EnableRealExecution and a separately authorized activation receipt.'
}
$resolvedPython = (Resolve-Path -LiteralPath $PythonPath -ErrorAction Stop).Path
$resolvedProject = (Resolve-Path -LiteralPath $ProjectRoot -ErrorAction Stop).Path
$resolvedDatabase = (Resolve-Path -LiteralPath $Database -ErrorAction Stop).Path
$resolvedReceipt = (Resolve-Path -LiteralPath $ActivationReceipt -ErrorAction Stop).Path
foreach ($filePath in @($resolvedPython, $resolvedDatabase, $resolvedReceipt)) {
    if (-not (Test-Path -LiteralPath $filePath -PathType Leaf)) {
        throw "Required file is unavailable: $filePath"
    }
}
if (-not (Test-Path -LiteralPath (Join-Path $resolvedProject 'v2\frontend\scheduler_runtime.py') -PathType Leaf)) {
    throw 'The selected project does not contain v2/frontend/scheduler_runtime.py.'
}
$receipt = Get-Content -LiteralPath $resolvedReceipt -Raw -Encoding UTF8 | ConvertFrom-Json
if ($receipt.authorized -isnot [bool] -or $receipt.authorized -ne $true -or
    $receipt.read_acceptance_passed -isnot [bool] -or $receipt.read_acceptance_passed -ne $true -or
    [string]::IsNullOrWhiteSpace([string]$receipt.validated_version_id)) {
    throw 'The activation receipt must confirm authorization, successful read acceptance and a published version ID.'
}
# The Python CLI independently validates this version in the selected database before opening a writable store.
$resolvedData = $null
if ($Data) {
    $resolvedData = (Resolve-Path -LiteralPath $Data -ErrorAction Stop).Path
    if (-not (Test-Path -LiteralPath $resolvedData -PathType Leaf)) {
        throw 'The selected data path is not a file.'
    }
}
$hasher = [System.Security.Cryptography.SHA256]::Create()
try {
    $databaseKey = [BitConverter]::ToString($hasher.ComputeHash(
        [System.Text.Encoding]::UTF8.GetBytes($resolvedDatabase.ToLowerInvariant())
    )).Replace('-', '').Substring(0, 24)
}
finally { $hasher.Dispose() }
if (-not $RuntimeRoot) {
    $RuntimeRoot = Join-Path $env:LOCALAPPDATA "25th-second\scheduler\$databaseKey"
}
$resolvedRuntime = [System.IO.Path]::GetFullPath($RuntimeRoot)
if (-not $StopFile) { $StopFile = Join-Path $resolvedRuntime 'stop.request' }
$resolvedStopFile = [System.IO.Path]::GetFullPath($StopFile)
if (Test-Path -LiteralPath $resolvedStopFile) {
    throw "A stop request already exists: $resolvedStopFile. Review it before explicitly removing it for a new launch."
}

$mutex = [System.Threading.Mutex]::new($false, "Local\CourtVisionScheduler-$databaseKey")
$ownsMutex = $false
$childProcess = $null
$logs = Join-Path $resolvedRuntime 'logs'

function Write-SchedulerLog([string]$Message) {
    Add-Content -LiteralPath (Join-Path $logs 'supervisor.log') -Encoding UTF8 -Value "$(Get-Date -Format o) $Message"
}
function Quote-NativePath([string]$Value) {
    # Windows filenames cannot contain quotes. Arguments are paths or fixed tokens, never shell expressions.
    if ($Value.Contains('"')) { throw 'A launch argument contains an invalid quotation mark.' }
    return '"' + $Value + '"'
}
function Wait-RetryOrStop([int]$Seconds) {
    for ($waitIndex = 0; $waitIndex -lt $Seconds; $waitIndex++) {
        if (Test-Path -LiteralPath $resolvedStopFile) { return $true }
        if ($stopDeadline -and [DateTimeOffset]::Now -ge $stopDeadline) { return $true }
        Start-Sleep -Seconds 1
    }
    return (Test-Path -LiteralPath $resolvedStopFile)
}
function Stop-OwnedChild {
    if ($null -eq $childProcess) { return }
    try {
        if (-not $childProcess.HasExited) {
            Write-SchedulerLog "Waiting for scheduler graceful shutdown; child_pid=$($childProcess.Id)"
            if (-not $childProcess.WaitForExit($StopWaitSeconds * 1000)) {
                Write-SchedulerLog "Shutdown grace elapsed; terminating this supervisor's child_pid=$($childProcess.Id)"
                Stop-Process -Id $childProcess.Id -ErrorAction SilentlyContinue
                $childProcess.WaitForExit(3000) | Out-Null
            }
        }
    }
    finally { $childProcess.Dispose() }
}

try {
    try { $ownsMutex = $mutex.WaitOne(0) }
    catch [System.Threading.AbandonedMutexException] { $ownsMutex = $true }
    if (-not $ownsMutex) {
        Write-Output "A scheduler supervisor already owns this database: $resolvedDatabase"
        exit 0
    }
    New-Item -ItemType Directory -Path $logs -Force | Out-Null
    New-Item -ItemType Directory -Path (Split-Path -Parent $resolvedStopFile) -Force | Out-Null
    Write-SchedulerLog "Supervisor started; supervisor_pid=$PID; database=$resolvedDatabase; stop_file=$resolvedStopFile"
    $retrySeconds = 3
    while (-not (Test-Path -LiteralPath $resolvedStopFile)) {
        if ($stopDeadline -and [DateTimeOffset]::Now -ge $stopDeadline) { break }
        if ($PreventIdleSleep) {
            if ([CourtVisionSchedulerPower]::SetThreadExecutionState([uint32]2147483649) -eq 0) {
                throw 'Temporary scheduler idle-sleep request failed.'
            }
        }
        $startedAt = Get-Date
        $exitCode = $null
        try {
            $stamp = Get-Date -Format 'yyyyMMdd-HHmmss-fff'
            $arguments = @('-u', '-m', 'v2.frontend.scheduler_runtime', '--daemon',
                '--database', (Quote-NativePath $resolvedDatabase),
                '--allow-real-execution', '--activation-receipt', (Quote-NativePath $resolvedReceipt),
                '--stop-file', (Quote-NativePath $resolvedStopFile))
            if ($resolvedData) { $arguments += @('--data', (Quote-NativePath $resolvedData)) }
            $childProcess = Start-Process -FilePath $resolvedPython -ArgumentList $arguments `
                -WorkingDirectory $resolvedProject -WindowStyle Hidden -PassThru `
                -RedirectStandardOutput (Join-Path $logs "$stamp-runner.log") `
                -RedirectStandardError (Join-Path $logs "$stamp-error.log")
            Write-SchedulerLog "Scheduler started; child_pid=$($childProcess.Id)"
            while (-not $childProcess.WaitForExit(1000)) {
                if (Test-Path -LiteralPath $resolvedStopFile) { break }
                if ($stopDeadline -and [DateTimeOffset]::Now -ge $stopDeadline) {
                    New-Item -ItemType File -Path $resolvedStopFile -Force | Out-Null
                    break
                }
            }
            if (Test-Path -LiteralPath $resolvedStopFile) { break }
            $exitCode = $childProcess.ExitCode
            Write-SchedulerLog "Scheduler exited; code=$exitCode"
            if (((Get-Date) - $startedAt).TotalSeconds -ge 60) { $retrySeconds = 3 }
        }
        catch {
            Write-SchedulerLog "Launch failed: $($_.Exception.Message)"
        }
        finally {
            Stop-OwnedChild
            $childProcess = $null
        }
        if ($exitCode -eq 2) {
            Write-SchedulerLog 'Startup validation rejected the configuration; supervisor will not retry automatically.'
            break
        }
        if (Wait-RetryOrStop $retrySeconds) { break }
        $retrySeconds = [Math]::Min(60, $retrySeconds * 2)
    }
    Write-SchedulerLog 'Supervisor stopped; no further scheduler restart will be attempted.'
}
finally {
    if ($PreventIdleSleep) { [CourtVisionSchedulerPower]::SetThreadExecutionState([uint32]2147483648) | Out-Null }
    # Handle Ctrl+C / terminating exceptions without stopping any unrelated Python process.
    if ($null -ne $childProcess) {
        if (-not (Test-Path -LiteralPath $resolvedStopFile)) {
            New-Item -ItemType File -Path $resolvedStopFile -Force | Out-Null
        }
        Stop-OwnedChild
    }
    if ($ownsMutex) { $mutex.ReleaseMutex() }
    $mutex.Dispose()
}
