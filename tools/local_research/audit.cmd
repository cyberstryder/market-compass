@echo off
setlocal
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
 echo Run setup.cmd first.
 pause
 exit /b 1
)
.venv\Scripts\python.exe gap_audit.py %*
set RESULT=%ERRORLEVEL%
pause
exit /b %RESULT%
