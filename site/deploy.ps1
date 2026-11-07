<#
.SYNOPSIS
  Publishes this checkout's site/ folder to the live copy and restarts the server.

.DESCRIPTION
    powershell -ExecutionPolicy Bypass -File .\site\deploy.ps1

  Mirrors site\ into %USERPROFILE%\.sites\meetingscribe\site, stops only the
  server on port 3070, then runs supervise.ps1 from the live copy, which starts
  it again. The scheduled task points at the live copy, so switching branches
  in this checkout never changes what is served.
#>
[CmdletBinding()]
param([int]$Port = 3070)

$ErrorActionPreference = 'Stop'
$root = Join-Path $env:USERPROFILE '.sites\meetingscribe'
$live = Join-Path $root 'site'
$marker = Join-Path $root 'deploying'
New-Item -ItemType Directory -Force $live | Out-Null

Set-Content $marker (Get-Date -Format 'u')
try {
  $servers = Get-CimInstance Win32_Process -Filter "Name='node.exe'" |
    Where-Object { $_.CommandLine -match 'server\.mjs' -and (Get-NetTCPConnection -OwningProcess $_.ProcessId -LocalPort $Port -State Listen -ErrorAction SilentlyContinue) }
  foreach ($s in $servers) {
    Write-Output "Stopping server on :$Port (pid $($s.ProcessId))"
    Stop-Process -Id $s.ProcessId -Force -ErrorAction SilentlyContinue
  }
  if ($servers) { Start-Sleep -Seconds 1 }

  # robocopy exit codes below 8 mean success
  robocopy $PSScriptRoot $live /MIR /NFL /NDL /NJH /NJS | Out-Null
  if ($LASTEXITCODE -ge 8) { throw 'copy failed' }
} finally {
  Remove-Item $marker -ErrorAction SilentlyContinue
}

& powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $live 'supervise.ps1')
Start-Sleep -Seconds 1
$code = (Invoke-WebRequest "http://127.0.0.1:$Port/" -UseBasicParsing -TimeoutSec 10).StatusCode
Write-Output "Live on :$Port (HTTP $code)"
