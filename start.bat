@echo off
rem Starts ReverseHire from the source folder (Windows).
rem On the first run it creates the virtual environment .venv and installs the
rem dependencies (internet needed, a few minutes, no administrator rights).
rem The data (settings, saved searches, log) goes to the "userdata" folder next
rem to this file when the folder is writable (see docs\INSTALLATION.md).
setlocal
cd /d "%~dp0"

if exist ".venv\Scripts\pythonw.exe" goto run

echo ReverseHire - first start: creating the Python environment in .venv
echo.
set "PY="
for %%V in (3.14 3.13 3.12) do (
    if not defined PY (
        py -%%V -c "import sys" >nul 2>&1 && set "PY=py -%%V"
    )
)
if not defined PY (
    python -c "import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)" >nul 2>&1 && set "PY=python"
)
if not defined PY (
    echo Python 3.12 or later was not found.
    echo Install it from https://www.python.org/downloads/windows/ ^(tick "Add python.exe to PATH"^)
    echo and run start.bat again.
    pause
    exit /b 1
)
echo Using: %PY%
%PY% -m venv .venv
if errorlevel 1 goto failed
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -r requirements.txt
if errorlevel 1 goto failed
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -e .
if errorlevel 1 goto failed
echo.
echo Environment ready.

:run
start "" ".venv\Scripts\pythonw.exe" -m reversehire
endlocal
exit /b 0

:failed
echo.
echo The setup failed (see the messages above). Check the internet connection, delete
echo the .venv folder and run start.bat again.
pause
exit /b 1
