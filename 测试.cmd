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
  echo 没有找到可用的 Python。
  pause
  exit /b 1
)

set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "PYTHONDONTWRITEBYTECODE=1"
%PYEXE% -m unittest discover -s "源码\tests" -p "test_*.py" -v
pause
