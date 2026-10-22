param(
    [Parameter(Mandatory=$true)][string]$ConfigPath,
    [string]$TaskName='CourtVision Opening Night FINAL 20261021'
)
$ErrorActionPreference='Stop'
$project=(Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$config=(Resolve-Path -LiteralPath $ConfigPath).Path
$settings=Get-Content -LiteralPath $config -Raw -Encoding UTF8 | ConvertFrom-Json
foreach($name in @('python_path','database','data','activation_receipt','runtime_root','public_root')) {
    if(-not $settings.$name) {throw "Missing configured field: $name"}
}
$prior=Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if($prior) {
    if($prior.State -eq 'Running') {throw 'Gracefully stop the idle existing scheduler before replacing its launcher.'}
    if(-not ($prior.Actions.Arguments -like "*$project*")) {throw 'The existing task does not belong to this project.'}
}
function Quote-Path([string]$value) {
    if($value.Contains('"')) {throw 'Invalid configuration path'}
    return '"'+$value+'"'
}
$arguments='-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File '+
    (Quote-Path (Join-Path $PSScriptRoot 'run_continuous_scheduler.ps1'))+' -ConfigPath '+(Quote-Path $config)
$action=New-ScheduledTaskAction -Execute (Get-Command powershell.exe).Source -Argument $arguments -WorkingDirectory $project
$principal=New-ScheduledTaskPrincipal -UserId ([Security.Principal.WindowsIdentity]::GetCurrent().Name) -LogonType Interactive -RunLevel Limited
$trigger=New-ScheduledTaskTrigger -AtLogOn -User $principal.UserId
$policy=New-ScheduledTaskSettingsSet -WakeToRun -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Principal $principal -Settings $policy -Force | Out-Null
Write-Output 'Continuous scheduler installed in the existing task slot; no model call was started.'
