<#
.SYNOPSIS
  Makes sure the MeetingScribe homepage (server.mjs, localhost:3070) is running,
  starting it only if nothing is listening on the port.

.DESCRIPTION
  Run by the "MeetingScribe Site" scheduled task at logon and every 5 minutes
  (register it with .\register-task.ps1), so the page comes back after a
  restart or a crash on its own. Safe to run by hand.

    Live copy       %USERPROFILE%\.sites\meetingscribe\site, written by deploy.ps1
    Public address  https://meetingscribe.preetbohra.com, through the shared
                    Cloudflare tunnel "applyai" (~/.cloudflared/config.yml).
#>

$ErrorActionPreference = 'Stop'
$port = 3070

# deploy.ps1 holds this marker while it swaps files.
if (Test-Path (Join-Path $PSScriptRoot '..\deploying')) { exit 0 }
if (Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue) { exit 0 }

$logs = Join-Path $PSScriptRoot '..\logs'
New-Item -ItemType Directory -Force $logs | Out-Null
Start-Process -FilePath 'C:\Program Files\nodejs\node.exe' -ArgumentList 'server.mjs' `
  -WorkingDirectory $PSScriptRoot -WindowStyle Hidden `
  -RedirectStandardOutput (Join-Path $logs 'site.log') -RedirectStandardError (Join-Path $logs 'site.err.log')
