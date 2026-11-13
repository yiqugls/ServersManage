@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo ============================================
echo   哨兵 / Sentinel Ops
echo ============================================

where python >nul 2>nul
if errorlevel 1 (
  echo [错误] 没有找到 python，请先安装 Python 3.9+ 并勾选 "Add to PATH"
  pause
  exit /b 1
)

python -c "import paramiko" >nul 2>nul
if errorlevel 1 (
  echo [提示] 缺少 paramiko，正在安装...
  python -m pip install "paramiko>=2.9"
  if errorlevel 1 (
    echo [错误] 安装失败，请手动执行: python -m pip install paramiko
    pause
    exit /b 1
  )
)

python run.py --open %*
if errorlevel 1 pause
