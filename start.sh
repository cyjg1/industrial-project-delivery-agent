#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

if command -v python3 >/dev/null 2>&1; then
  PYTHON=python3
elif command -v python >/dev/null 2>&1; then
  PYTHON=python
else
  echo "[start] 失败：未找到 Python。请先安装 Python 3。" >&2
  exit 1
fi

exec "$PYTHON" "$ROOT/scripts/start_dev.py" "$@"
