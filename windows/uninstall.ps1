<#
.SYNOPSIS
  Stops MeetingScribe and removes its scheduled task. Notes, voiceprints, models and the
  .venv are kept; delete the repository folder too if you want everything gone.
#>

& (Join-Path $PSScriptRoot 'ctl.ps1') stop | Out-Null
Unregister-ScheduledTask -TaskName 'MeetingScribe' -Confirm:$false -ErrorAction SilentlyContinue
"MeetingScribe removed. Notes in $env:USERPROFILE\Documents\Meeting Notes were left untouched."
