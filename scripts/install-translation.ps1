$ErrorActionPreference = 'Stop'
Push-Location (Split-Path $PSScriptRoot -Parent)
try {
    if (-not (Test-Path -LiteralPath '.venv-translation-win/Scripts/python.exe')) {
        & uv venv --python 3.12 .venv-translation-win
        if ($LASTEXITCODE -ne 0) { throw 'Could not create isolated environment' }
    }
    & uv pip install --python .venv-translation-win/Scripts/python.exe 'pdf2zh-next==2.9.0' 'babeldoc==0.6.2' 'bitstring==4.4.0' 'python-dotenv==1.2.4' 'pypdf==6.21.1'
    if ($LASTEXITCODE -ne 0) { throw 'Translation dependency installation failed' }
    & .venv-translation-win/Scripts/python.exe -X utf8 -c 'from babeldoc.assets.assets import warmup; warmup()'
    if ($LASTEXITCODE -ne 0) { throw 'Translation assets verification failed' }
} finally { Pop-Location }
