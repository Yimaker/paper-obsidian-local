#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
python3 -m venv .venv
.venv/bin/python -m pip install -e .
if [[ ! -e .env ]]; then cp .env.example .env; chmod 600 .env; fi
printf '%s\n' '安装完成。诊断：.venv/bin/python -m paper_obsidian doctor'
