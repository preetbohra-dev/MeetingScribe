<#
.SYNOPSIS
  MeetingScribe control, the Windows twin of ctl.sh. Run it as .\ctl from the repository:
  .\ctl app | start | stop | restart | status | logs | name | reject | listen | voices | search | ask | refilter | jira | ai | terms
#>

param([Parameter(Position = 0)][string]$Command = 'status',
      [Parameter(Position = 1, ValueFromRemainingArguments = $true)][string[]]$Rest)

$Root = Split-Path $PSScriptRoot -Parent
$Py = Join-Path $Root '.venv\Scripts\python.exe'
$Script = Join-Path $Root 'meeting_scribe.py'
$Logs = Join-Path $env:LOCALAPPDATA 'MeetingScribe\Logs'
$Task = 'MeetingScribe'

function Get-Scribe($what) {
  Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" |
    Where-Object { $_.CommandLine -like "*meeting_scribe.py*`" $what*" -or $_.CommandLine -like "*meeting_scribe.py $what*" }
}

function Invoke-Scribe { & $Py $Script @args; exit $LASTEXITCODE }

switch ($Command) {
  'start' {
    Enable-ScheduledTask -TaskName $Task | Out-Null
    Start-ScheduledTask -TaskName $Task
    'started'
  }
  'stop' {
    # Disabled, so the 5-minute trigger does not bring it straight back.
    Disable-ScheduledTask -TaskName $Task -ErrorAction SilentlyContinue | Out-Null
    Stop-ScheduledTask -TaskName $Task -ErrorAction SilentlyContinue
    Get-Scribe 'run' | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    'stopped (a recording in progress is processed on next start; .\ctl start to resume)'
  }
  'restart' {
    Get-Scribe 'run' | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    Enable-ScheduledTask -TaskName $Task | Out-Null
    Start-ScheduledTask -TaskName $Task
    'restarted'
  }
  'status' {
    $d = Get-Scribe 'run'
    if ($d) { "daemon: running (pid $(@($d.ProcessId) -join ', '))" } else { 'daemon: NOT running' }
    if (Get-Scribe 'process') { 'processing: a meeting is being transcribed/summarized' }
    '--- last daemon log lines'
    Get-Content (Join-Path $Logs 'daemon.log') -Tail 8 -ErrorAction SilentlyContinue
    '--- live detection'
    Invoke-Scribe status
  }
  'app' { Start-Process (Join-Path $Root '.venv\Scripts\pythonw.exe') "`"$(Join-Path $Root 'meetingscribe_app.py')`"" }
  'reject' {  # .\ctl reject "Priya Shah" [session]: that name is wrong for that voice
    $session = if ($Rest.Count -ge 2) { $Rest[1] } else { 'latest' }
    Invoke-Scribe reject-name $Rest[0] --session $session
  }
  'logs' { Get-Content (Join-Path $Logs 'daemon.log'), (Join-Path $Logs 'process.log') -Tail 50 -Wait -ErrorAction SilentlyContinue }
  'name' {  # .\ctl name "Speaker 2" "Priya Shah" [session]
    $session = if ($Rest.Count -ge 3) { $Rest[2] } else { 'latest' }
    Invoke-Scribe name-speaker $Rest[0] $Rest[1] --session $session
  }
  'listen' {  # .\ctl listen ["Speaker 2"] [session]
    $label = @($Rest | Select-Object -First 1)
    $session = if ($Rest.Count -ge 2) { $Rest[1] } else { 'latest' }
    Invoke-Scribe listen @label --session $session
  }
  { $_ -in 'voices', 'search', 'ask', 'refilter', 'terms', 'jira', 'ai', 'process', 'download-models' } { Invoke-Scribe $Command @Rest }
  default {
    'usage: .\ctl app|start|stop|restart|status|logs|name|reject|listen|voices|search|ask|refilter|jira|ai|terms|process|download-models'
    exit 1
  }
}
