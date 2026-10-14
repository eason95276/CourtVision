param(
    [Parameter(Mandatory=$true)][string]$ActivationReceipt,
    [Parameter(Mandatory=$true)][string]$PythonPath,
    [Parameter(Mandatory=$true)][string]$Database,
    [Parameter(Mandatory=$true)][string]$Data,
    [Parameter(Mandatory=$true)][string]$RuntimeRoot,
    [Parameter(Mandatory=$true)][string]$PublicRoot,
    [string]$WakeAt='2026-10-21T02:35:00+08:00',
    [string]$StopAt='2026-10-21T09:40:00+08:00'
)
$ErrorActionPreference='Stop'
$project=(Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$receiptPath=(Resolve-Path -LiteralPath $ActivationReceipt).Path
$receipt=Get-Content -LiteralPath $receiptPath -Raw -Encoding UTF8 | ConvertFrom-Json
$scope=@('espn:401909088','espn:401909089','espn:401909090')
if ($receipt.authorized -ne $true -or $receipt.read_acceptance_passed -ne $true -or
    @($receipt.allowed_game_ids).Count -ne 3 -or @(Compare-Object $scope @($receipt.allowed_game_ids)).Count -ne 0 -or
    @($receipt.allowed_stages).Count -ne 1 -or $receipt.allowed_stages[0] -ne 'FINAL_PREGAME') {throw 'Requires verified three-game FINAL-only activation receipt.'}
$python=(Resolve-Path -LiteralPath $PythonPath).Path
$db=(Resolve-Path -LiteralPath $Database).Path
$dataPath=(Resolve-Path -LiteralPath $Data).Path
$public=(Resolve-Path -LiteralPath $PublicRoot).Path
function Quote-Cv([string]$value){if($value.Contains('"')){throw 'Invalid argument quote'};return '"'+$value+'"'}
$name='CourtVision Opening Night FINAL 20261021'
if(Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue){throw 'Existing project task must be reviewed, not silently overwritten.'}
$argsText='-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File '+(Quote-Cv (Join-Path $PSScriptRoot 'run_opening_final.ps1'))+
    ' -PythonPath '+(Quote-Cv $python)+' -ProjectRoot '+(Quote-Cv $project)+' -Database '+(Quote-Cv $db)+
    ' -Data '+(Quote-Cv $dataPath)+' -ActivationReceipt '+(Quote-Cv $receiptPath)+
    ' -RuntimeRoot '+(Quote-Cv ([IO.Path]::GetFullPath($RuntimeRoot)))+' -PublicRoot '+(Quote-Cv $public)+' -StopAt '+(Quote-Cv $StopAt)
$action=New-ScheduledTaskAction -Execute (Get-Command powershell.exe).Source -Argument $argsText -WorkingDirectory $project
$trigger=New-ScheduledTaskTrigger -Once -At ([DateTimeOffset]::Parse($WakeAt).LocalDateTime)
$settings=New-ScheduledTaskSettingsSet -WakeToRun -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 8)
$principal=New-ScheduledTaskPrincipal -UserId ([Security.Principal.WindowsIdentity]::GetCurrent().Name) -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger -Settings $settings -Principal $principal | Out-Null
Get-ScheduledTask -TaskName $name | Select-Object TaskName,State
Write-Output 'Registered only; no seven-day idle-sleep prevention and no premature model call.'
