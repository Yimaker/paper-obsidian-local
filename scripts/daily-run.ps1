param([switch]$NoFetch,[switch]$Diagnostic,[ValidateSet('complete','failed','paused','abrupt')][string]$DiagnosticOutcome='complete')
$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
$logDir = Join-Path $root $(if($Diagnostic){'data/diagnostics/scheduler'}else{'data/production/scheduler'})
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$stamp = (Get-Date -Format 'yyyyMMdd-HHmmss-fff')+'-'+[guid]::NewGuid().ToString('N').Substring(0,8)
$log = Join-Path $logDir ($stamp+'.log')
$historyReceipt = Join-Path $logDir ($stamp+'.json')
$receipt = Join-Path $logDir 'latest.json'
try { $guard=[IO.File]::Open((Join-Path $logDir 'entry.lock'),'OpenOrCreate','ReadWrite','None') }
catch { [Console]::Error.WriteLine('Another scheduler entry holds the OS lock'); exit 75 }
function Save-Record {
    $json=$record | ConvertTo-Json
    foreach($target in @($historyReceipt,$receipt)) {
        $tmp=$target+'.tmp'; [IO.File]::WriteAllText($tmp,$json,(New-Object Text.UTF8Encoding($false)))
        Move-Item -LiteralPath $tmp -Destination $target -Force
    }
}
$record=[ordered]@{Started=[DateTimeOffset]::Now.ToString('o');Ended=$null;ExitCode=$null;Status='running';Log=$log;Entry=$(if($Diagnostic){'diagnostic'}else{'daily'});Pid=$PID;Account=[Security.Principal.WindowsIdentity]::GetCurrent().Name}
$code=1
try {
    Save-Record
    if (-not $Diagnostic -and (Test-Path (Join-Path $root 'data/production/STOP'))) { $code=130 }
    elseif (-not $Diagnostic -and (Test-Path (Join-Path $root 'data/production/MAINTENANCE'))) { throw 'Maintenance active' }
    else {
        $paperArgs=@('daily')
        if($Diagnostic){$paperArgs=@('diagnostic',$DiagnosticOutcome)}
        elseif($NoFetch){$paperArgs+='--no-fetch'}
        # Native child ensures run.ps1 exit cannot skip this parent's finalizer.
        & "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -NonInteractive -ExecutionPolicy Bypass -File (Join-Path $root 'run.ps1') @paperArgs *> $log
        $code=$LASTEXITCODE
    }
} catch {
    $_.Exception.Message | Add-Content -LiteralPath $log -Encoding UTF8
    $code=1
} finally {
    $record.Ended=[DateTimeOffset]::Now.ToString('o');$record.ExitCode=$code
    $record.Status=switch($code){0{'complete'} 2{'partial'} 130{'paused'} 75{'concurrent'} 137{'interrupted'} -1073741510{'interrupted'} default{'failed'}}
    Save-Record
    $guard.Dispose()
}
exit $code
