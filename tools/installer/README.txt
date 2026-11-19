ReverseHire - installation media
================================

ReverseHire is a desktop app for Windows that starts from one CV and finds,
within a radius around a point, the companies that fit the profile,
highlighting the ones that are hiring.

HOW TO INSTALL
  1. Double-click  install.bat
  2. Wait for "Installation completed" (a few minutes).
  3. Start ReverseHire from the Desktop or the Start menu.

No internet connection and no administrator rights are needed to install.
The app itself needs internet when you run a search.

Requirements: Windows 10 or 11, 64-bit; about 3 GB of free disk space.

WHAT IS ON THIS MEDIA
  install.bat   starts the installation
  docs\         Installation Manual and User Manual (PDF), licence, attributions
  python\       official Python 3.14 installer (used only if Python is missing)
  wheels\       ReverseHire and all its Python dependencies
  models\       embedding model used for scoring (multilingual-e5-small)
  setup\        installation script, uninstaller, icon
  redist\       (optional) Microsoft Visual C++ runtime, vc_redist.x64.exe,
                installed only on PCs where it is missing

At the end the installer runs a self-test (scoring model loaded without
internet, text recognition) and prints PASS, WARNING or FAIL. Everything it
shows is also saved in
  %LOCALAPPDATA%\Programs\ReverseHire\install.log
Your settings, saved searches and log are kept in
  %LOCALAPPDATA%\Programs\ReverseHire\userdata
and the embedding model in %LOCALAPPDATA%\Programs\ReverseHire\models.
Send install.log if something does not work. In the app, the menu "?" has
"Diagnostics..." (checks to copy and send) and "Open log folder".

Read docs\Installation_Manual.pdf for details, manual installation, removal
and troubleshooting, and docs\User_Manual.pdf to learn how to use the app.

ReverseHire is released under the MIT licence (docs\LICENSE.txt).
Third-party components keep their own licences (docs\ATTRIBUTIONS.md).
