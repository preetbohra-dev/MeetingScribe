<#
.SYNOPSIS
  Installs MeetingScribe for this Windows user: run .\install from the repository.
  No admin rights.

.DESCRIPTION
  1. Python 3.11-3.13 (installed for this user with winget if there is none)
  2. .venv with requirements.txt
  3. The terms of use (type "I agree")
  4. Models: Whisper large-v3-turbo, WeSpeaker, Qwen 2.5 GGUF (14B with 24 GB of RAM or
     more, else 7B). The only step that uses the network; every later run is offline.
  5. Scheduled task "MeetingScribe": starts the background recorder at logon (and
     every 5 minutes if it ever stopped), started right away.

  Safe to run again: it reuses what is already there.
#>

$ErrorActionPreference = 'Stop'
$Root = Split-Path $PSScriptRoot -Parent
$Venv = Join-Path $Root '.venv'
$Py = Join-Path $Venv 'Scripts\python.exe'
$Pyw = Join-Path $Venv 'Scripts\pythonw.exe'
$Script = Join-Path $Root 'meeting_scribe.py'
$Task = 'MeetingScribe'

function Step($text) { Write-Host "`n== $text" -ForegroundColor Cyan }

function Find-Python {
  foreach ($v in '3.13', '3.12', '3.11') {
    $p = & py "-$v" -c 'import sys; print(sys.executable)' 2>$null
    if ($LASTEXITCODE -eq 0 -and $p) { return $p.Trim() }
  }
  foreach ($dir in Get-ChildItem "$env:LOCALAPPDATA\Programs\Python\Python31[123]" -Directory -ErrorAction SilentlyContinue |
           Sort-Object Name -Descending) {
    $exe = Join-Path $dir.FullName 'python.exe'
    if (Test-Path $exe) { return $exe }
  }
  return $null
}

if ([Environment]::OSVersion.Version.Build -lt 20348) {
  Write-Warning 'Recording Teams alone needs Windows 11 (or Windows 10 build 20348+). This version records all system audio instead.'
}

Step 'Python'
if (-not (Test-Path $Py)) {
  $base = Find-Python
  if (-not $base) {
    Write-Host 'No Python 3.11-3.13 found; installing Python 3.12 for this user with winget...'
    winget install --exact --id Python.Python.3.12 --scope user --silent `
      --accept-package-agreements --accept-source-agreements | Out-Host
    $base = Find-Python
    if (-not $base) { throw 'Python could not be installed. Install Python 3.12 from python.org, then run .\install again.' }
  }
  Write-Host "Using $base"
  & $base -m venv $Venv
  if ($LASTEXITCODE) { throw 'Could not create the virtual environment.' }
} else {
  Write-Host "Reusing $Venv"
}

Step 'Packages'
& $Py -m pip install --quiet --upgrade pip
& $Py -m pip install --quiet --requirement (Join-Path $Root 'requirements.txt')
if ($LASTEXITCODE) { throw 'Package install failed; see the messages above.' }
if (Get-Command nvidia-smi -ErrorAction SilentlyContinue) {
  Write-Host 'NVIDIA GPU found: adding the CUDA libraries so Whisper runs on the GPU.'
  & $Py -m pip install --quiet nvidia-cublas-cu12 'nvidia-cudnn-cu12==9.*'
}

Step 'Terms of use'
& $Py $Script terms
if ($LASTEXITCODE) { throw 'Not installed: the terms of use were not accepted.' }

Step 'Models (several GB, once)'
& $Py $Script download-models
if ($LASTEXITCODE) { throw 'Model download failed; run .\install again to resume.' }

Step 'Background task'
& (Join-Path $PSScriptRoot 'ctl.ps1') stop | Out-Null
$user = "$env:USERDOMAIN\$env:USERNAME"
$action = New-ScheduledTaskAction -Execute $Pyw -Argument "`"$Script`" run" -WorkingDirectory $Root
$logon = New-ScheduledTaskTrigger -AtLogOn -User $user
# A second trigger every 5 minutes brings it back if it ever exits; a running copy holds a
# lock, so extra starts end at once.
$every = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 5)
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
  -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew -StartWhenAvailable
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName $Task -Action $action -Trigger $logon, $every -Settings $settings `
  -Principal $principal -Description 'MeetingScribe: records and transcribes Teams meetings on this PC.' -Force | Out-Null
Start-ScheduledTask -TaskName $Task

$logs = Join-Path $env:LOCALAPPDATA 'MeetingScribe\Logs'
Write-Host "`nInstalled and started." -ForegroundColor Green
Write-Host "  Task:  $Task (Task Scheduler, runs at logon)"
Write-Host "  Notes: $env:USERPROFILE\Documents\Meeting Notes (output_dir in config.toml)"
Write-Host "  Logs:  $logs\daemon.log"
Write-Host "`nCheck it: .\ctl status   Remove it: .\uninstall"
