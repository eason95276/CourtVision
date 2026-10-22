param([Parameter(Mandatory=$true)][string]$ConfigPath)
$ErrorActionPreference='Stop'
$config=Get-Content -LiteralPath $ConfigPath -Raw -Encoding UTF8 | ConvertFrom-Json
$project=(Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$env:COURTVISION_PUBLIC_PUBLISH_ROOT=(Resolve-Path -LiteralPath $config.public_root).Path
& (Join-Path $PSScriptRoot 'run_local_scheduler.ps1') -PythonPath $config.python_path -ProjectRoot $project `
    -Database $config.database -ActivationReceipt $config.activation_receipt -Data $config.data `
    -RuntimeRoot $config.runtime_root -PreventIdleSleep -EnableRealExecution
