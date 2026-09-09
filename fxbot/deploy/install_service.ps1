<#
.SYNOPSIS
    Install fxbot as an NSSM service (§13.4).

.DESCRIPTION
    Automatic (Delayed Start), because the MT5 terminal has to come up first. The bot
    retries mt5.initialize() with backoff for up to five minutes at boot rather than
    crashing while the terminal is still starting (RuntimeParams.connect_retry_seconds).

    Graceful shutdown matters: the service stop is handled by finishing the current cycle,
    saving risk state and calling mt5.shutdown(). Never kill mid-order.

.PARAMETER Env
    demo or live. Promotion between them is a config change, never a code change (§13.5).
#>
[CmdletBinding()]
param(
    [ValidateSet('demo', 'live')][string]$Env = 'demo',
    [string]$InstallRoot = 'C:\fxbot',
    [string]$ServiceName = 'fxbot'
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$python = Join-Path $InstallRoot '.venv\Scripts\python.exe'
$logs = Join-Path $InstallRoot 'logs'
if (-not (Test-Path $python)) { throw "venv not found at $python; run bootstrap_vps.ps1 first" }
New-Item -ItemType Directory -Force -Path $logs | Out-Null

if (Get-Service -Name $ServiceName -ErrorAction SilentlyContinue) {
    Write-Host "Stopping and removing the existing $ServiceName service"
    nssm stop $ServiceName confirm | Out-Null
    nssm remove $ServiceName confirm | Out-Null
    Start-Sleep -Seconds 2
}

Write-Host "Installing the $ServiceName service for --env $Env"
nssm install $ServiceName $python '-m' 'fxbot.cli' 'live' '--env' $Env
nssm set $ServiceName AppDirectory $InstallRoot
nssm set $ServiceName DisplayName "fxbot ($Env)"
nssm set $ServiceName Description 'Trend-following FX bot for MetaTrader 5'

# Delayed start: the terminal must be up and logged in before the bridge can attach.
nssm set $ServiceName Start SERVICE_DELAYED_AUTO_START

# Restart on exit, 10s delay, 60s throttle - a crash loop must not hammer the broker.
nssm set $ServiceName AppExit Default Restart
nssm set $ServiceName AppRestartDelay 10000
nssm set $ServiceName AppThrottle 60000

# stdout/stderr to rotating files. The JSON journal in logs\fxbot.jsonl is the real record;
# this catches anything that escapes structlog.
nssm set $ServiceName AppStdout (Join-Path $logs 'service.out.log')
nssm set $ServiceName AppStderr (Join-Path $logs 'service.err.log')
nssm set $ServiceName AppRotateFiles 1
nssm set $ServiceName AppRotateOnline 1
nssm set $ServiceName AppRotateBytes 10485760

# Give the current cycle time to finish and the risk state time to persist.
nssm set $ServiceName AppStopMethodConsole 20000
nssm set $ServiceName AppStopMethodWindow 20000
nssm set $ServiceName AppStopMethodThreads 20000

nssm start $ServiceName
Start-Sleep -Seconds 5
nssm status $ServiceName

Write-Host ''
Write-Host "Installed. Check it is alive with:  $python -m fxbot.cli status --env $Env"
