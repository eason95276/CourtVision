param(
    [Parameter(Mandatory=$true)][string]$PythonPath,
    [Parameter(Mandatory=$true)][string]$ProjectRoot,
    [Parameter(Mandatory=$true)][string]$Database,
    [Parameter(Mandatory=$true)][string]$Data,
    [Parameter(Mandatory=$true)][string]$ActivationReceipt,
    [Parameter(Mandatory=$true)][string]$RuntimeRoot,
    [Parameter(Mandatory=$true)][string]$PublicRoot,
    [Parameter(Mandatory=$true)][string]$StopAt
)
$ErrorActionPreference='Stop'
$env:COURTVISION_PUBLIC_PUBLISH_ROOT=(Resolve-Path -LiteralPath $PublicRoot).Path
# Existing local site supervisor is installed independently. Its read APIs are
# cache-only; this task never grants the site permission to generate.
& (Join-Path $PSScriptRoot 'run_local_scheduler.ps1') -PythonPath $PythonPath -ProjectRoot $ProjectRoot `
    -Database $Database -ActivationReceipt $ActivationReceipt -Data $Data -RuntimeRoot $RuntimeRoot `
    -StopAt $StopAt -PreventIdleSleep -EnableRealExecution
