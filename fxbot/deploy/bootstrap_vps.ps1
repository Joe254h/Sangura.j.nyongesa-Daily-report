<#
.SYNOPSIS
    Idempotent Windows Server 2022 provisioning for fxbot (§13.2).

.DESCRIPTION
    Run once on a fresh London VPS, as Administrator. Re-running is safe: every step
    checks before it acts.

    What this does NOT do, deliberately:
      * it does not write a .env  - secrets are placed by hand, readable by the fxbot user
        only (§13.6);
      * it does not enable auto-logon - that stores a credential in the registry, so it is
        a conscious decision documented in the RUNBOOK, not a side effect of provisioning;
      * it does not start trading.

.PARAMETER RepoUrl
    Git URL of the fxbot repository.

.PARAMETER RdpAllowFrom
    Your static IP. RDP is opened to this address only; everything else inbound is denied.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$RepoUrl,
    [Parameter(Mandatory = $true)][string]$RdpAllowFrom,
    [string]$InstallRoot = 'C:\fxbot',
    [string]$PythonVersion = '3.11.9',
    [string]$Mt5Path = 'C:\Program Files\MetaTrader 5\terminal64.exe'
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

function Write-Step { param([string]$Message) Write-Host "==> $Message" -ForegroundColor Cyan }

# ---------------------------------------------------------------- 1. timezone
# The VPS runs UTC. Broker server time is derived from tick timestamps, never from the OS
# clock (§0.6) - but a UTC host keeps log correlation sane.
Write-Step 'Setting the system timezone to UTC'
tzutil /s 'UTC'

# ---------------------------------------------------------------- 2. Windows Update
# A forced reboot mid-session closes the terminal and every MT5 call fails. Patch
# deliberately at the weekend market close instead (§13.3 step 4).
Write-Step 'Disabling automatic reboots for Windows Update'
$auPath = 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate\AU'
New-Item -Path $auPath -Force | Out-Null
Set-ItemProperty -Path $auPath -Name 'NoAutoRebootWithLoggedOnUsers' -Value 1 -Type DWord
Set-ItemProperty -Path $auPath -Name 'AUOptions' -Value 2 -Type DWord   # notify, do not install

# ---------------------------------------------------------------- 3. package manager
if (-not (Get-Command choco -ErrorAction SilentlyContinue)) {
    Write-Step 'Installing Chocolatey'
    Set-ExecutionPolicy Bypass -Scope Process -Force
    [System.Net.ServicePointManager]::SecurityProtocol = 3072
    Invoke-Expression ((New-Object System.Net.WebClient).DownloadString(
        'https://community.chocolatey.org/install.ps1'))
    $env:Path = [Environment]::GetEnvironmentVariable('Path', 'Machine')
}

# ---------------------------------------------------------------- 4. Python 3.11
# Not 3.12+: Backtrader is frozen and breaks on newer toolchains (§1.2).
Write-Step "Installing Python $PythonVersion (per-machine, on PATH)"
choco install python311 --version=$PythonVersion -y --no-progress `
    --params '"/InstallDir:C:\Python311"'

Write-Step 'Installing Git and NSSM'
choco install git nssm -y --no-progress

Write-Step 'Installing MetaTrader 5'
if (-not (Test-Path $Mt5Path)) { choco install metatrader5 -y --no-progress }

$env:Path = [Environment]::GetEnvironmentVariable('Path', 'Machine')

# ---------------------------------------------------------------- 5. the tree
Write-Step "Preparing $InstallRoot"
New-Item -ItemType Directory -Force -Path $InstallRoot | Out-Null
if (-not (Test-Path (Join-Path $InstallRoot '.git'))) {
    git clone $RepoUrl $InstallRoot
} else {
    git -C $InstallRoot pull --ff-only
}
foreach ($dir in 'logs', 'state', 'data', 'reports') {
    New-Item -ItemType Directory -Force -Path (Join-Path $InstallRoot $dir) | Out-Null
}

Write-Step 'Creating the virtualenv and installing the package'
$venv = Join-Path $InstallRoot '.venv'
if (-not (Test-Path $venv)) { & 'C:\Python311\python.exe' -m venv $venv }
& (Join-Path $venv 'Scripts\python.exe') -m pip install --upgrade pip
& (Join-Path $venv 'Scripts\python.exe') -m pip install -e "$InstallRoot[dev]"

# ---------------------------------------------------------------- 6. MT5 chart history
# MT5 returns only bars within the terminal's "Max. bars in chart" setting. Target history
# is >= 8 years of H1 so walk-forward has enough folds (§6.5).
Write-Step 'Raising the terminal history limit to unlimited'
$commonIni = Join-Path $env:APPDATA 'MetaQuotes\Terminal\Common\common.ini'
if (Test-Path $commonIni) {
    (Get-Content $commonIni) -replace '^MaxBars=.*', 'MaxBars=2147483647' |
        Set-Content $commonIni
} else {
    Write-Warning 'common.ini not found: set Tools -> Options -> Charts -> Max bars = Unlimited by hand.'
}

# ---------------------------------------------------------------- 7. firewall
Write-Step "Denying all inbound except RDP from $RdpAllowFrom"
Set-NetFirewallProfile -Profile Domain, Public, Private `
    -DefaultInboundAction Block -DefaultOutboundAction Allow -Enabled True
Get-NetFirewallRule -DisplayName 'fxbot-rdp' -ErrorAction SilentlyContinue |
    Remove-NetFirewallRule
New-NetFirewallRule -DisplayName 'fxbot-rdp' -Direction Inbound -Action Allow `
    -Protocol TCP -LocalPort 3389 -RemoteAddress $RdpAllowFrom | Out-Null

Write-Host ''
Write-Host 'Bootstrap complete. Still to do by hand, in this order:' -ForegroundColor Green
Write-Host '  1. Log into MT5 and confirm the ping to the trade server is < 20 ms (§13.1).'
Write-Host '  2. Tools -> Options -> Expert Advisors -> Allow Algo Trading, and check the'
Write-Host '     toolbar button is GREEN. If it is off, every order returns retcode 10027.'
Write-Host "  3. Place $InstallRoot\.env from .env.example; restrict it to the fxbot user."
Write-Host '  4. Enable auto-logon for the fxbot user (netplwiz) - see RUNBOOK.md §13.3.'
Write-Host "  5. python -m scripts.download_history --env demo --years 8 --dump-specs"
Write-Host '  6. deploy\install_service.ps1 -Env demo'
