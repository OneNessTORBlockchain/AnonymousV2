@echo off
REM Anonymous V2 - double-click installer. Delegates to install.ps1, which
REM detects Python, installs the backend packages and reports on Docker.
setlocal
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" %*
if errorlevel 1 pause
endlocal
