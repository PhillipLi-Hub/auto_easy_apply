param(
    [ValidatePattern('^(?:[01][0-9]|2[0-3]):[0-5][0-9]$')]
    [string]$Time = '09:00',
    [switch]$Disable
)
$ErrorActionPreference = 'Stop'
$taskName = 'LinkedIn Easy Apply Daily'
$configPath = Join-Path $PSScriptRoot 'data\schedule.json'
if ($Disable) {
    Disable-ScheduledTask -TaskName $taskName | Out-Null
    @{enabled=$false; time=$Time; task_name=$taskName} | ConvertTo-Json | Set-Content -LiteralPath $configPath -Encoding UTF8
    Write-Output 'Daily auto apply disabled. An already-running application run can be stopped with Stop in the app.'
    return
}
$appPython = Join-Path $PSScriptRoot '.venv\Scripts\pythonw.exe'
if (-not (Test-Path -LiteralPath $appPython)) { throw 'Run Setup.ps1 first.' }
$nextRun = [DateTime]::Today.Add([TimeSpan]::Parse($Time))
if ($nextRun -le (Get-Date)) { $nextRun = $nextRun.AddDays(1) }
$userName = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$action = New-ScheduledTaskAction -Execute $appPython -Argument '-m easy_apply.scheduled --if-due' -WorkingDirectory $PSScriptRoot
$trigger = New-ScheduledTaskTrigger -Daily -At $nextRun
# A boundary without a UTC offset means 09:00 local wall time, including DST.
$trigger.StartBoundary = $nextRun.ToString("yyyy-MM-dd'T'HH:mm:ss")
# Retry a missed launch after wake, plus at sign-in. The Python entry point
# checks the configured time and the once-per-day database claim on every call.
$repeating = New-ScheduledTaskTrigger -Once -At $nextRun -RepetitionInterval ([TimeSpan]::FromMinutes(15)) -RepetitionDuration ([TimeSpan]::FromDays(1))
$trigger.Repetition = $repeating.Repetition
$logonTrigger = New-ScheduledTaskTrigger -AtLogOn -User $userName
$principal = New-ScheduledTaskPrincipal -UserId $userName -LogonType Interactive -RunLevel Limited
# The desktop may stay open overnight. Allow tomorrow's short request process
# even while that task is running; the app lock and daily request claim prevent
# overlapping browsers and application runs. Never forcibly kill a submission.
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -WakeToRun -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances Parallel -ExecutionTimeLimit ([TimeSpan]::Zero)
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger @($trigger, $logonTrigger) -Principal $principal -Settings $settings -Description 'Daily Easy Apply at the saved time, with 15-minute catch-up checks and sign-in checks. One scheduled run per local day. Requires a signed-in Windows session.' -Force | Out-Null
New-Item -ItemType Directory -Path (Join-Path $PSScriptRoot 'data') -Force | Out-Null
@{enabled=$true; time=$Time; task_name=$taskName; timezone=(Get-TimeZone).Id} | ConvertTo-Json | Set-Content -LiteralPath $configPath -Encoding UTF8
Get-ScheduledTaskInfo -TaskName $taskName | Select-Object TaskName,NextRunTime,LastTaskResult
