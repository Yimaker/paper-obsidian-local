param(
    [ValidateSet('Install','Uninstall','Status','Stop','Pause','Resume','Run')][string]$Action = 'Status',
    [string]$Time = '12:01',
    [switch]$AllowPaidCalls
)
$ErrorActionPreference = 'Stop'
$taskName = 'PaperObsidianLocal-AI-ML'
$root = Split-Path $PSScriptRoot -Parent
$task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if ($Action -eq 'Uninstall') {
    if ($task) { Unregister-ScheduledTask -TaskName $taskName -Confirm:$false }
    Write-Output '任务已卸载，全部论文、缓存和配置保留。'; exit
}
if ($Action -in @('Pause','Stop')) {
    if ($task) { Disable-ScheduledTask -TaskName $taskName | Out-Null }
    & (Join-Path $root 'run.ps1') stop
    Write-Output '已暂停每日任务；当前运行会保存已完成步骤，在安全边界停止。'; exit
}
if ($Action -eq 'Resume') {
    Remove-Item -LiteralPath (Join-Path $root 'data/production/STOP') -ErrorAction SilentlyContinue
    Enable-ScheduledTask -TaskName $taskName | Out-Null
    $zone = [TimeZoneInfo]::FindSystemTimeZoneById('China Standard Time')
    $boundary = [DateTimeOffset]::Parse($task.Triggers[0].StartBoundary)
    $scheduledTime = [TimeZoneInfo]::ConvertTime($boundary,$zone).ToString('HH:mm')
    Write-Output ('已恢复每日北京时间'+$scheduledTime+'运行。'); exit
}
if ($Action -eq 'Run') {
    if (-not $task) { throw '每日任务尚未安装' }
    if ($task.State -eq 'Disabled') { throw '任务已暂停，请先恢复，再立即运行' }
    Start-ScheduledTask -TaskName $taskName
    Write-Output '已请求立即运行；任务和项目锁会阻止并发。可用“查看状态与错误”查看结果。'; exit
}
if ($Action -eq 'Install') {
    if ($Time -notmatch '^([01]\d|2[0-3]):[0-5]\d$') { throw '时间须为HH:mm' }
    $shanghai = [TimeZoneInfo]::FindSystemTimeZoneById('China Standard Time')
    $beijingNow = [TimeZoneInfo]::ConvertTime([DateTimeOffset]::UtcNow,$shanghai)
    $first = [DateTimeOffset]::ParseExact(($beijingNow.ToString('yyyy-MM-dd')+' '+$Time+' +08:00'),'yyyy-MM-dd HH:mm zzz',[Globalization.CultureInfo]::InvariantCulture)
    if ($first -le $beijingNow) { $first = $first.AddDays(1) }
    $trigger = New-ScheduledTaskTrigger -Daily -At $first.LocalDateTime
    $trigger.StartBoundary = $first.ToString("yyyy-MM-dd'T'HH:mm:sszzz")
    $entry = Join-Path $PSScriptRoot 'daily-run.ps1'
    $actionObj = New-ScheduledTaskAction -Execute "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe" -Argument ('-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File "'+$entry+'"') -WorkingDirectory $root
    $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
    $principal = New-ScheduledTaskPrincipal -UserId ([Security.Principal.WindowsIdentity]::GetCurrent().Name) -LogonType Interactive -RunLevel Limited
    Register-ScheduledTask -TaskName $taskName -Action $actionObj -Trigger $trigger -Settings $settings -Principal $principal -Description ('本机论文库；北京时间'+$Time+'；WSL Ubuntu；检索、AI原文审校、双语PDF；恢复后补跑一次；授权模型调用') -Force | Out-Null
    Enable-ScheduledTask -TaskName $taskName | Out-Null
}
$task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if (-not $task) { Write-Output '每日任务尚未安装。'; exit 2 }
$info = Get-ScheduledTaskInfo -TaskName $taskName
$zone = [TimeZoneInfo]::FindSystemTimeZoneById('China Standard Time')
$last = if ($info.LastRunTime.Year -gt 2000) { $info.LastRunTime.ToString('yyyy-MM-dd HH:mm:ss') } else { '尚未运行' }
$status = [ordered]@{
    TaskName=$task.TaskName; State=[string]$task.State; Enabled=$task.Settings.Enabled
    WindowsTimeZone=(Get-TimeZone).Id
    NextRunBeijing=[TimeZoneInfo]::ConvertTime($info.NextRunTime,$zone).ToString('yyyy-MM-dd HH:mm:ss')
    RunAccount=$task.Principal.UserId; LogonType=[string]$task.Principal.LogonType
    StartBoundary=$task.Triggers[0].StartBoundary; StartWhenAvailable=$task.Settings.StartWhenAvailable
    MultipleInstances=[string]$task.Settings.MultipleInstances; LastRun=$last
    LastTaskResult=$info.LastTaskResult; MissedRuns=$info.NumberOfMissedRuns
    WSLDistribution='Ubuntu'; Entry='scripts/daily-run.ps1 -> WSL scripts/run.sh daily'
}
$status | ConvertTo-Json -Depth 4
$status | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath (Join-Path $root 'data/production/task-status.json') -Encoding UTF8
if (Test-Path -LiteralPath (Join-Path $root 'data/production/last-run.json')) {
    Write-Output ('详细结果：'+(Join-Path $root 'data/production/last-run.json'))
}
