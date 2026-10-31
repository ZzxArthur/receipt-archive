#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
if [[ ! -x .venv/bin/python ]]; then
  if command -v uv >/dev/null 2>&1; then
    uv venv --python python3.12 .venv
    uv pip install --python .venv/bin/python --no-deps -r requirements-linux.lock
  else
    python3 -m venv .venv
    .venv/bin/python -m pip install --no-deps -r requirements-linux.lock
  fi
elif ! .venv/bin/python -c 'import PIL, numpy, rapidocr, yaml, requests, tqdm' >/dev/null 2>&1; then
  if command -v uv >/dev/null 2>&1; then
    uv pip install --python .venv/bin/python --no-deps -r requirements-linux.lock
  else
    .venv/bin/python -m pip install --no-deps -r requirements-linux.lock
  fi
fi
.venv/bin/python scripts/prepare_ocr_models.py
export OMP_NUM_THREADS="${OMP_NUM_THREADS:-2}"
export OPENBLAS_NUM_THREADS="${OPENBLAS_NUM_THREADS:-2}"
printf '凭证归档已启动，请在浏览器打开 http://127.0.0.1:%s\n' "${RECEIPT_PORT:-8765}"
exec .venv/bin/python -m receipt_tool.app --host 127.0.0.1 --port "${RECEIPT_PORT:-8765}"
