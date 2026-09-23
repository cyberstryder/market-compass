@echo off
setlocal
cd /d "%~dp0"
py -3.12 -m venv .venv
if errorlevel 1 (
 echo Install Python 3.12 from python.org with the Python launcher, then run setup.cmd again.
 pause
 exit /b 1
)
.venv\Scripts\python.exe -m pip install -r requirements.txt
if errorlevel 1 exit /b 1
.venv\Scripts\python.exe -m unittest discover -s tests -v
if errorlevel 1 exit /b 1
echo Setup complete. Read README.md, then run run.cmd.
pause
