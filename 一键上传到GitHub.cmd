@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

rem 双击即把当前项目提交并推送到 GitHub（验收通过后再用）。
rem 只做检查与预览：  一键上传到GitHub.cmd -DryRun
rem 指定公开仓库：    一键上传到GitHub.cmd -Visibility Public -Yes

set "PS1=%USERPROFILE%\.codex\skills\github-publish-oneclick\scripts\publish-to-github.ps1"
if not exist "%PS1%" (
  echo 没有找到一键上传脚本：
  echo   %PS1%
  pause
  exit /b 1
)

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%PS1%" -ProjectPath "%~dp0." %*
echo.
pause
