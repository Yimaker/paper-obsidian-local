param([Parameter(ValueFromRemainingArguments=$true)][string[]]$PaperArgs)
$ErrorActionPreference = 'Stop'
[Console]::InputEncoding = New-Object System.Text.UTF8Encoding($false)
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$OutputEncoding = [Console]::OutputEncoding
if (-not $PaperArgs -or $PaperArgs.Count -eq 0) { $PaperArgs = @('status') }
$windowsProject = $PSScriptRoot.Replace('\','/')
$linuxProject = '/mnt/' + $windowsProject.Substring(0,1).ToLowerInvariant() + $windowsProject.Substring(2)
# Avoid decoding a Unicode path through PowerShell 5.1's scheduler OEM codepage.
# The native command line transports the existing .NET Unicode path directly.
$ErrorActionPreference = 'Continue'
& wsl.exe -d Ubuntu --cd $linuxProject -- bash ./scripts/run.sh @PaperArgs
exit $LASTEXITCODE
