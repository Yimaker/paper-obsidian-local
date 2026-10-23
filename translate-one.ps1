param([switch]$Preflight)
$ErrorActionPreference = 'Stop'
$py = Join-Path $PSScriptRoot '.venv-translation-win/Scripts/python.exe'
if (-not (Test-Path -LiteralPath $py)) { throw 'Missing isolated translation environment' }
$taskArgs = @('-X', 'utf8', (Join-Path $PSScriptRoot 'tools/translate_one.py'))
if ($Preflight) { $taskArgs += '--preflight' }
& $py @taskArgs
exit $LASTEXITCODE
