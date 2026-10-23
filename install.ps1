$ErrorActionPreference = 'Stop'
$windowsProject = $PSScriptRoot.Replace('\','/')
$converted = & wsl.exe -d Ubuntu -- wslpath -a $windowsProject
if ($LASTEXITCODE -ne 0) { throw 'Ubuntu WSL 不可用' }
$linuxProject = $converted.Trim()
& wsl.exe -d Ubuntu --cd $linuxProject -- bash ./scripts/install.sh
exit $LASTEXITCODE
