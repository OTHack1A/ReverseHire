@echo off
rem ReverseHire offline installer: double-click this file.
rem It needs no internet connection and no administrator rights.
setlocal
title ReverseHire - installation
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup\install.ps1" %*
if errorlevel 1 (
    echo.
    echo The installation did NOT complete. Read the messages above;
    echo the Installation Manual in the docs folder has a troubleshooting section.
    echo.
    pause
    exit /b 1
)
echo.
pause
endlocal
