#Requires -RunAsAdministrator
<#
.SYNOPSIS
    Stop (gracefully) and remove the caudal-bot Windows service. Data, backups,
    transcripts and logs are left in place.
#>
[CmdletBinding()]
param(
    [string]$ServiceName = "CaudalBot",
    [string]$Nssm = "nssm.exe"
)
$ErrorActionPreference = "Stop"

$nssmCmd = Get-Command $Nssm -ErrorAction SilentlyContinue
if (-not $nssmCmd) { throw "NSSM not found; pass -Nssm C:\path\to\nssm.exe" }
if (-not (Get-Service -Name $ServiceName -ErrorAction SilentlyContinue)) {
    Write-Host "Service $ServiceName is not installed."
    return
}
& $nssmCmd.Source stop $ServiceName          # Ctrl+C -> graceful shutdown (up to 30s)
& $nssmCmd.Source remove $ServiceName confirm
if ($LASTEXITCODE -ne 0) { throw "nssm remove failed (exit $LASTEXITCODE)" }
Write-Host "Removed $ServiceName. Data folders were not touched."
