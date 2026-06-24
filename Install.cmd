@echo off
REM GboardIME installer — double-click to run.
REM Passes any extra args through (e.g. Install.cmd -SkipDebloat).
echo Starting GboardIME installer...
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" %*
echo.
echo Installer finished. Press any key to close.
pause >nul
