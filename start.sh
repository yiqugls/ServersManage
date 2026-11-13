#!/usr/bin/env bash
# 哨兵 / Sentinel Ops 启动脚本（Linux / macOS）
set -e
cd "$(dirname "$0")"

PY=${PYTHON:-python3}
command -v "$PY" >/dev/null 2>&1 || { echo "没有找到 $PY，请先安装 Python 3.9+"; exit 1; }

if ! "$PY" -c "import paramiko" >/dev/null 2>&1; then
  echo "缺少 paramiko，正在安装…"
  "$PY" -m pip install "paramiko>=2.9" || {
    echo "安装失败，请手动执行：$PY -m pip install paramiko"
    exit 1
  }
fi

exec "$PY" run.py "$@"
