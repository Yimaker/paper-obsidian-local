#!/usr/bin/env bash
# Optional isolated CPU runtime. This does not install GPU drivers or alter system Python.
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p data
# Keep the existing summary venv untouched. uv must be independently available.
command -v uv >/dev/null
uv python install 3.12
uv venv --python 3.12 .venv-translation
uv pip install --python .venv-translation/bin/python 'pdf2zh-next==2.9.0' 'babeldoc==0.6.2' 'bitstring==4.4.0'
.venv-translation/bin/pdf2zh_next --help > data/translation-help.txt
printf '%s\n' '组件已安装。仍须填写独立翻译配置并确认预算，默认不启动翻译。'
