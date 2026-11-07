<#
.SYNOPSIS
  Registers the "MeetingScribe Site" scheduled task: runs the live copy's
  supervise.ps1 at logon and every 5 minutes, so the homepage recovers from a
  restart or a crash on its own. Re-running replaces the task. Run deploy.ps1
  first so the live copy exists.
#>

$ErrorActionPreference = 'Stop'
$script = Join-Path $env:USERPROFILE '.sites\meetingscribe\site\supervise.ps1'
$hidden = Join-Path $env:USERPROFILE '.scheduled-tasks\run-hidden.vbs'

$action = New-ScheduledTaskAction -Execute 'wscript.exe' `
  -Argument "`"$hidden`" `"powershell.exe`" -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$script`""
$atLogon = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$atLogon.Delay = 'PT30S'
$every5 = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 5)
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
  -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero)

Register-ScheduledTask -TaskName 'MeetingScribe Site' -Action $action -Trigger $atLogon, $every5 `
  -Settings $settings -Description 'Serves meetingscribe.preetbohra.com on localhost:3070 (behind the applyai tunnel).' -Force |
  Out-Null
Write-Output "Registered task 'MeetingScribe Site' -> $script"
