@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

set "PYEXE="
where py >nul 2>nul && set "PYEXE=py -3"
if not defined PYEXE (
  where python >nul 2>nul && set "PYEXE=python"
)
if not defined PYEXE (
  if exist "C:\Program Files\Lenovo\ModelMgr\Plugins\Image\python.exe" (
    set "PYEXE=C:\Program Files\Lenovo\ModelMgr\Plugins\Image\python.exe"
  )
)
if not defined PYEXE (
  echo 没有找到可用的 Python，请先安装 Python 或用命令行指定解释器。
  pause
  exit /b 1
)

set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
%PYEXE% "安装到Anki.py" %*
echo.
echo 安装结束。请重启 Anki（插件只在启动时加载）。
pause
