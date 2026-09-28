#Requires -RunAsAdministrator
<#
.SYNOPSIS
    Install or update caudal-bot as a Windows service using NSSM.

.DESCRIPTION
    Creates the virtualenv if needed, runs an offline --dry-run, then registers
    "python bot.py --run" with NSSM:
      * restarts automatically after a crash (10s delay, throttled if it keeps failing),
      * stops gracefully with Ctrl+C (the bot's SIGINT shutdown), waiting up to 30s,
      * writes stdout/stderr to logs\caudal-bot.log, rotated daily or at 10 MB,
      * runs as NT AUTHORITY\LocalService with write access to the data folders only.
    It does not start the service unless -Start is given (starting connects to Discord).

.EXAMPLE
    # From an elevated PowerShell in the project folder:
    .\deploy\windows\install-service.ps1
    .\deploy\windows\install-service.ps1 -Start
#>
[CmdletBinding()]
param(
    [string]$ServiceName = "CaudalBot",
    [string]$ProjectDir = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path,
    [string]$Nssm = "nssm.exe",
    # Used only to create .venv if it doesn't exist yet.
    [string]$BasePython = "",
    # NT AUTHORITY\LocalService needs no password. For a dedicated local account pass
    # -Account ".\caudal-svc" -Credential (Get-Credential .\caudal-svc).
    [string]$Account = "NT AUTHORITY\LocalService",
    [pscredential]$Credential,
    [switch]$Start
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

function Step([string]$Message) { Write-Host "`n==> $Message" -ForegroundColor Cyan }

$nssmCmd = Get-Command $Nssm -ErrorAction SilentlyContinue
if (-not $nssmCmd) {
    throw "NSSM not found. Install it (winget install NSSM.NSSM, or choco install nssm, or https://nssm.cc) or pass -Nssm C:\path\to\nssm.exe"
}
$NssmExe = $nssmCmd.Source

function Invoke-Nssm {
    & $NssmExe @args
    if ($LASTEXITCODE -ne 0) { throw "nssm $($args -join ' ') failed (exit $LASTEXITCODE)" }
}

foreach ($required in "bot.py", "caudal_bot\main.py", ".env") {
    if (-not (Test-Path (Join-Path $ProjectDir $required))) { throw "$required not found in $ProjectDir" }
}

# Same data layout as the Docker image and the systemd unit.
$ServiceEnv = @(
    "PYTHONUNBUFFERED=1",
    "PYTHONUTF8=1",
    "DATABASE_PATH=data\tickets.db",
    "HEALTH_HOST=127.0.0.1",
    "HEALTH_PORT=8081"
)
$LogDir = Join-Path $ProjectDir "logs"
$LogFile = Join-Path $LogDir "caudal-bot.log"
$VenvPython = Join-Path $ProjectDir ".venv\Scripts\python.exe"

Step "Virtualenv"
if (-not (Test-Path $VenvPython)) {
    if ($BasePython) { & $BasePython -m venv (Join-Path $ProjectDir ".venv") }
    elseif (Get-Command py -ErrorAction SilentlyContinue) { & py -3.11 -m venv (Join-Path $ProjectDir ".venv") }
    else { & python -m venv (Join-Path $ProjectDir ".venv") }
    if ($LASTEXITCODE -ne 0) { throw "could not create .venv (need Python 3.11+)" }
}
& $VenvPython -c "import sys; sys.exit(sys.version_info < (3, 11))"
if ($LASTEXITCODE -ne 0) { throw ".venv uses Python older than 3.11" }
& $VenvPython -m pip install --disable-pip-version-check --quiet --upgrade -r (Join-Path $ProjectDir "caudal_bot\requirements.txt")
if ($LASTEXITCODE -ne 0) { throw "pip install failed" }

Step "Data folders"
foreach ($dir in "data", "backups", "transcripts", "logs") {
    New-Item -ItemType Directory -Force -Path (Join-Path $ProjectDir $dir) | Out-Null
}
$oldDb = Join-Path $ProjectDir "tickets.db"
$newDb = Join-Path $ProjectDir "data\tickets.db"
if ((Test-Path $oldDb) -and -not (Test-Path $newDb)) {
    try {
        foreach ($suffix in "", "-wal", "-shm") {
            if (Test-Path "$oldDb$suffix") { Move-Item "$oldDb$suffix" "$newDb$suffix" }
        }
        Write-Host "moved tickets.db into data\"
    } catch {
        Write-Warning "Could not move tickets.db into data\ (is a bot still running?): $_"
    }
}

Step "Other copies of the bot"
$others = Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" |
    Where-Object { $_.CommandLine -match "bot\.py|caudal_bot" }
foreach ($p in $others) {
    Write-Warning "PID $($p.ProcessId) is already running the bot: $($p.CommandLine). Stop it before starting the service, or both will answer every message."
}

Step "Offline dry run (never connects to Discord)"
$saved = @{}
Push-Location $ProjectDir
try {
    foreach ($pair in $ServiceEnv) {
        $name, $value = $pair.Split("=", 2)
        $saved[$name] = [Environment]::GetEnvironmentVariable($name, "Process")
        [Environment]::SetEnvironmentVariable($name, $value, "Process")
    }
    & $VenvPython bot.py --dry-run
    if ($LASTEXITCODE -ne 0) { throw "dry run failed; fix the problems above and re-run" }
} finally {
    foreach ($name in $saved.Keys) { [Environment]::SetEnvironmentVariable($name, $saved[$name], "Process") }
    Pop-Location
}

Step "Service $ServiceName"
if (Get-Service -Name $ServiceName -ErrorAction SilentlyContinue) {
    Write-Host "updating the existing service"
    & $NssmExe stop $ServiceName | Out-Null     # graceful (Ctrl+C); fine if already stopped
    Invoke-Nssm set $ServiceName Application $VenvPython
} else {
    Invoke-Nssm install $ServiceName $VenvPython
}
# bot.py is relative to AppDirectory, so paths with spaces need no quoting.
Invoke-Nssm set $ServiceName AppDirectory $ProjectDir
Invoke-Nssm set $ServiceName AppParameters "bot.py --run"
Invoke-Nssm set $ServiceName DisplayName "Caudal Support Bot"
Invoke-Nssm set $ServiceName Description "Discord FAQ auto-responder with ticket escalation (caudal_bot)."
Invoke-Nssm set $ServiceName Start SERVICE_DELAYED_AUTO_START
Invoke-Nssm set $ServiceName AppEnvironmentExtra @ServiceEnv

# Restart on any exit after 10s; if it keeps dying within 60s of starting, NSSM backs off.
Invoke-Nssm set $ServiceName AppExit Default Restart
Invoke-Nssm set $ServiceName AppRestartDelay 10000
Invoke-Nssm set $ServiceName AppThrottle 60000

# Graceful stop: Ctrl+C reaches the bot as SIGINT; give it 30s before harsher methods.
# AppNoConsole must stay 0, or there is no console to send Ctrl+C to.
Invoke-Nssm set $ServiceName AppNoConsole 0
Invoke-Nssm set $ServiceName AppStopMethodSkip 0
Invoke-Nssm set $ServiceName AppStopMethodConsole 30000
Invoke-Nssm set $ServiceName AppStopMethodWindow 5000
Invoke-Nssm set $ServiceName AppStopMethodThreads 5000
Invoke-Nssm set $ServiceName AppKillProcessTree 1

# stdout and stderr share one log (the bot logs to stderr), appended and rotated
# daily or at 10 MB, also while running.
Invoke-Nssm set $ServiceName AppStdout $LogFile
Invoke-Nssm set $ServiceName AppStderr $LogFile
Invoke-Nssm set $ServiceName AppStdoutCreationDisposition 4
Invoke-Nssm set $ServiceName AppStderrCreationDisposition 4
Invoke-Nssm set $ServiceName AppRotateFiles 1
Invoke-Nssm set $ServiceName AppRotateOnline 1
Invoke-Nssm set $ServiceName AppRotateSeconds 86400
Invoke-Nssm set $ServiceName AppRotateBytes 10485760

Step "Service account and file permissions"
if ($Credential) {
    Invoke-Nssm set $ServiceName ObjectName $Account $Credential.GetNetworkCredential().Password
} elseif ($Account -like "NT AUTHORITY\*") {
    Invoke-Nssm set $ServiceName ObjectName $Account
} else {
    throw "-Account $Account needs -Credential (Get-Credential $Account)"
}
# Grant by SID for LocalService (S-1-5-19) so it works on non-English Windows.
$grantee = if ($Account -eq "NT AUTHORITY\LocalService") { "*S-1-5-19" } else { $Account }
& icacls $ProjectDir /grant "${grantee}:(OI)(CI)RX" /Q | Out-Null
foreach ($dir in "data", "backups", "transcripts", "logs") {
    & icacls (Join-Path $ProjectDir $dir) /grant "${grantee}:(OI)(CI)M" /Q | Out-Null
}
# A per-user Python (under C:\Users\...) is unreadable to service accounts by default,
# and the venv's python.exe loads its DLLs and stdlib from there.
$basePrefix = (& $VenvPython -c "import sys; print(sys.base_prefix)").Trim()
if ($basePrefix -like "$env:SystemDrive\Users\*") {
    Write-Host "granting $Account read access to the per-user Python at $basePrefix"
    & icacls $basePrefix /grant "${grantee}:(OI)(CI)RX" /Q | Out-Null
}
Write-Host "$Account can read the project and write only to data, backups, transcripts and logs"

if ($Start) {
    Step "Starting $ServiceName (connects to Discord)"
    Invoke-Nssm start $ServiceName
    $healthy = $false
    foreach ($i in 1..18) {
        Start-Sleep -Seconds 5
        try {
            $r = Invoke-WebRequest -UseBasicParsing -TimeoutSec 4 "http://127.0.0.1:8081/health"
            if ($r.StatusCode -eq 200) { $healthy = $true; break }
        } catch { }
    }
    if ($healthy) { Write-Host "healthy: http://127.0.0.1:8081/health" -ForegroundColor Green }
    else { Write-Warning "not healthy after 90s; check $LogFile" }
} else {
    Write-Host "`nInstalled. Nothing has connected to Discord yet. Start it with:"
    Write-Host "    nssm start $ServiceName        (or: Start-Service $ServiceName)"
    Write-Host "Logs: $LogFile   Health: http://127.0.0.1:8081/health"
}
