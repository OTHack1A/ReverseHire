@echo off
rem Removes ReverseHire (program folder and shortcuts). Python itself is left
rem installed: remove it from Windows Settings > Apps if you no longer need it.
rem Your data (settings, saved searches, log) is in the "userdata" folder inside
rem the program folder: you choose whether to keep it.
setlocal
title ReverseHire - removal
set "APPDIR=%~dp0"
if "%APPDIR:~-1%"=="\" set "APPDIR=%APPDIR:~0,-1%"

if not exist "%APPDIR%\venv\Scripts\python.exe" (
    echo This file must stay in the ReverseHire program folder.
    pause
    exit /b 1
)

echo This removes ReverseHire from:
echo   %APPDIR%
echo Close ReverseHire first if it is running.
echo.
choice /C YN /M "Remove ReverseHire"
if errorlevel 2 exit /b 0

for /f "usebackq delims=" %%D in (`powershell -NoProfile -Command "[Environment]::GetFolderPath('Desktop')"`) do del /q "%%D\ReverseHire.lnk" 2>nul
for /f "usebackq delims=" %%D in (`powershell -NoProfile -Command "[Environment]::GetFolderPath('Programs')"`) do del /q "%%D\ReverseHire.lnk" 2>nul

set "KEEPDATA=0"
if exist "%APPDIR%\userdata" (
    echo.
    echo Your data ^(settings, saved searches and log^) is in
    echo   %APPDIR%\userdata
    choice /C YN /M "Delete your data too"
    if errorlevel 2 set "KEEPDATA=1"
)

if exist "%APPDATA%\ReverseHire" (
    echo.
    echo An older version kept data and the embedding model ^(about 470 MB^) in
    echo   %APPDATA%\ReverseHire
    choice /C YN /M "Delete that folder too"
    if not errorlevel 2 rmdir /s /q "%APPDATA%\ReverseHire"
)

echo.
echo Removing the program files...
cd /d "%TEMP%"
if "%KEEPDATA%"=="1" goto keepdata
start "" /min cmd /c "timeout /t 2 /nobreak >nul & rmdir /s /q "%APPDIR%""
endlocal
exit /b 0

:keepdata
rem Everything except userdata (the embedding model in "models" is removed: a new
rem installation copies it again).
for /d %%F in ("%APPDIR%\*") do if /i not "%%~nxF"=="userdata" rmdir /s /q "%%F"
for %%F in ("%APPDIR%\*") do if /i not "%%~nxF"=="uninstall.bat" del /q /f "%%F" 2>nul
echo Your data was kept in "%APPDIR%\userdata". Installing ReverseHire again in the
echo same folder uses it again.
start "" /min cmd /c "timeout /t 2 /nobreak >nul & del /q /f "%APPDIR%\uninstall.bat""
endlocal
exit /b 0
