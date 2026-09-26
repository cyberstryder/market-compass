@echo off
setlocal
cd /d "%~dp0"
set "RESEARCH_PYTHON="
if exist .venv\Scripts\python.exe (
 call :try_python .venv\Scripts\python.exe
 if not errorlevel 1 goto install
 echo Existing .venv uses an unsupported Python. Rename that folder and rerun setup.cmd.
 goto failed
)
call :try_python python
if not errorlevel 1 goto create_env
call :try_python py -3.13
if not errorlevel 1 goto create_env
call :try_python py -3.12
if not errorlevel 1 goto create_env
call :try_python py -3.11
if not errorlevel 1 goto create_env
echo Install standard 64-bit Python 3.11, 3.12 or 3.13, then reopen PowerShell.
goto failed

:create_env
%RESEARCH_PYTHON% -m venv .venv
if errorlevel 1 goto failed

:install
.venv\Scripts\python.exe --version
.venv\Scripts\python.exe -m pip install -r requirements.txt
if errorlevel 1 goto failed
.venv\Scripts\python.exe -m unittest discover -s tests -v
if errorlevel 1 goto failed
echo Setup complete. Read README.md, then run run.cmd.
pause
exit /b 0

:try_python
%* -c "import sys,struct; sys.exit(0 if sys.version_info[:2] in ((3,11),(3,12),(3,13)) and struct.calcsize('P') == 8 else 1)" >nul 2>&1
if errorlevel 1 exit /b 1
set "RESEARCH_PYTHON=%*"
exit /b 0

:failed
echo Setup did not finish. Copy the error above if you need help.
pause
exit /b 1
