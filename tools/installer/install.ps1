# ReverseHire offline installer.
#
# Run it through install.bat in the root of the installation media. It needs no
# internet connection and no administrator rights:
#   1. installs Python 3.14 for the current user if it is missing;
#   2. creates a private Python environment in the install folder;
#   3. installs ReverseHire and its dependencies from the bundled wheels;
#   4. copies the embedding model to <InstallDir>\models (read-only flags cleared),
#      where the app finds it (model search folders);
#   5. creates the Desktop and Start menu shortcuts;
#   6. installs the Microsoft Visual C++ runtime from redist\vc_redist.x64.exe
#      when it is missing and the file is on the media;
#   7. self-test with the installed interpreter: the model is loaded from the
#      copied folder with no network and computes one embedding, and the OCR
#      engine is imported (python -m reversehire.diagnostics --selftest).
#      If only the model fails the installation still succeeds (the app then
#      works with reduced accuracy) and the cause is printed.
#
# Data: the app keeps settings, saved searches and log in <InstallDir>\userdata
# (portable mode: the program folder is per-user and writable, and unlike
# %APPDATA% it is not redirected for programs started from packaged apps). A
# reinstall replaces only the venv: userdata and models are kept. Data of older
# versions in %APPDATA%\ReverseHire is copied there by the app on its first start.
#
# Everything shown is also written to <InstallDir>\install.log.
# (Native programs are piped to Out-Host, so that the transcript records their output.)
#
# Parameters (all optional, mainly for testing):
#   -InstallDir   where the program goes (default %LOCALAPPDATA%\Programs\ReverseHire)
#   -NoShortcuts  do not create shortcuts
#   -NoModel      do not copy the embedding model

param(
    [string]$InstallDir = (Join-Path $env:LOCALAPPDATA 'Programs\ReverseHire'),
    [switch]$NoShortcuts,
    [switch]$NoModel
)

$ErrorActionPreference = 'Stop'
$PythonVersion = '3.14'
$Media = Split-Path -Parent $PSScriptRoot
$Wheels = Join-Path $Media 'wheels'

function Step([string]$Text) { Write-Host ''; Write-Host "== $Text" -ForegroundColor Cyan }

function Stop-Log { try { Stop-Transcript | Out-Null } catch { } }

function Fail([string]$Text) {
    Write-Host ''
    Write-Host "ERROR: $Text" -ForegroundColor Red
    Write-Host "The full output is in $LogFile"
    Stop-Log
    exit 1
}

function Clear-ReadOnly([string]$Folder) {
    # Files copied from an ISO or a DVD keep the read-only flag: clear it on
    # every file and folder, so that later updates can replace them.
    if (-not (Test-Path $Folder)) { return }
    Get-ChildItem $Folder -Recurse -Force | ForEach-Object {
        if ($_.Attributes -band [IO.FileAttributes]::ReadOnly) {
            $_.Attributes = $_.Attributes -band (-bnot [IO.FileAttributes]::ReadOnly)
        }
    }
}

function Test-Python([string]$Exe) {
    # True if $Exe is a 64-bit Python of the required version.
    if (-not $Exe -or -not (Test-Path $Exe)) { return $false }
    try {
        $out = & $Exe -c "import sys; print('%d.%d %d' % (sys.version_info[0], sys.version_info[1], sys.maxsize > 2**32))"
        return ($LASTEXITCODE -eq 0 -and "$out".Trim() -eq "$PythonVersion 1")
    } catch { return $false }
}

function Find-Python {
    $folder = 'Python' + $PythonVersion.Replace('.', '')
    $candidates = @(
        (Join-Path $env:LOCALAPPDATA "Programs\Python\$folder\python.exe"),
        (Join-Path $env:ProgramFiles "$folder\python.exe")
    )
    $launcher = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($launcher) {
        try {
            $fromLauncher = & $launcher.Source "-$PythonVersion" -c "import sys; print(sys.executable)" 2>$null
            if ($LASTEXITCODE -eq 0 -and $fromLauncher) { $candidates += "$fromLauncher".Trim() }
        } catch { }
    }
    foreach ($exe in $candidates) { if (Test-Python $exe) { return $exe } }
    return $null
}

New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
$LogFile = Join-Path $InstallDir 'install.log'
try { Start-Transcript -Path $LogFile -Force | Out-Null } catch { Write-Host "The log $LogFile cannot be written." }

Write-Host 'ReverseHire - offline installation' -ForegroundColor Green
Write-Host "Started: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss'); Windows $([Environment]::OSVersion.Version); media: $Media"
Write-Host "Program folder: $InstallDir"
$DataDir = Join-Path $InstallDir 'userdata'
Write-Host "Data folder:    $DataDir (portable)"

if (-not [Environment]::Is64BitOperatingSystem) { Fail 'A 64-bit version of Windows is required.' }
if (-not (Test-Path (Join-Path $Wheels 'reversehire-*.whl'))) {
    Fail "The 'wheels' folder is missing or incomplete on the installation media ($Wheels)."
}

# --- 1. Python ---------------------------------------------------------------------
Step "Python $PythonVersion"
$python = Find-Python
if ($python) {
    Write-Host "Found: $python"
} else {
    $setup = Get-ChildItem (Join-Path $Media 'python') -Filter "python-$PythonVersion.*-amd64.exe" -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if (-not $setup) { Fail "Python $PythonVersion is not installed and its installer is not on the media." }
    Write-Host "Installing $($setup.Name) for the current user (this takes a minute or two)..."
    $arguments = '/passive InstallAllUsers=0 PrependPath=0 Include_launcher=1 Include_test=0 Shortcuts=0'
    $process = Start-Process -FilePath $setup.FullName -ArgumentList $arguments -Wait -PassThru
    if ($process.ExitCode -ne 0) { Fail "The Python installer ended with code $($process.ExitCode)." }
    $python = Find-Python
    if (-not $python) { Fail "Python $PythonVersion was installed but cannot be found." }
    Write-Host "Installed: $python"
}

# --- 2. Private environment --------------------------------------------------------
Step 'Program folder and Python environment'
New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
$venv = Join-Path $InstallDir 'venv'
if (Test-Path $venv) {
    Write-Host 'Removing the environment of a previous installation...'
    Remove-Item -Recurse -Force $venv
}
& $python -m venv $venv | Out-Host
if ($LASTEXITCODE -ne 0) { Fail 'The Python environment could not be created.' }
$venvPython = Join-Path $venv 'Scripts\python.exe'
$venvPythonW = Join-Path $venv 'Scripts\pythonw.exe'

# --- 3. ReverseHire and dependencies (offline) -------------------------------------
Step 'Installing ReverseHire and its dependencies (a few minutes, no internet needed)'
& $venvPython -m pip install --no-index --find-links $Wheels --disable-pip-version-check --no-warn-script-location 'reversehire[mcp,jobboards]' | Out-Host
if ($LASTEXITCODE -ne 0) { Fail 'The installation of the packages failed (see the messages above).' }
& $venvPython -c "import reversehire, PySide6.QtWidgets, fastembed, onnxruntime, rapidocr; print('ReverseHire', reversehire.__version__, 'installed')" | Out-Host
if ($LASTEXITCODE -ne 0) { Fail 'The installed program does not start (see the messages above).' }

# --- 4. Embedding model ------------------------------------------------------------
$modelSource = Join-Path $Media 'models'
if ($NoModel) {
    Write-Host 'Embedding model: skipped on request.'
} elseif (Test-Path $modelSource) {
    Step 'Copying the embedding model (about 470 MB)'
    $modelTarget = Join-Path $InstallDir 'models'
    New-Item -ItemType Directory -Force -Path $modelTarget | Out-Null
    robocopy $modelSource $modelTarget /E /NFL /NDL /NJH /NJS /NP | Out-Null
    if ($LASTEXITCODE -ge 8) { Fail "The embedding model could not be copied to $modelTarget." }
    Clear-ReadOnly $modelTarget
    Write-Host "Model copied to $modelTarget"
} else {
    Write-Host 'Embedding model not on the media: it will be downloaded at the first search.' -ForegroundColor Yellow
}

# --- 5. Program files, shortcuts ---------------------------------------------------
Step 'Shortcuts and documentation'
$setupFolder = Join-Path $Media 'setup'
Copy-Item (Join-Path $setupFolder 'icon.ico') $InstallDir -Force
Copy-Item (Join-Path $setupFolder 'uninstall.bat') $InstallDir -Force
$docs = Join-Path $Media 'docs'
if (Test-Path $docs) { Copy-Item $docs $InstallDir -Recurse -Force }
Clear-ReadOnly (Join-Path $InstallDir 'docs')
foreach ($name in 'icon.ico', 'uninstall.bat') {
    $file = Get-Item (Join-Path $InstallDir $name)
    if ($file.IsReadOnly) { $file.IsReadOnly = $false }
}

if (-not $NoShortcuts) {
    $shell = New-Object -ComObject WScript.Shell
    $places = @([Environment]::GetFolderPath('Desktop'), [Environment]::GetFolderPath('Programs'))
    foreach ($place in $places) {
        $shortcut = $shell.CreateShortcut((Join-Path $place 'ReverseHire.lnk'))
        $shortcut.TargetPath = $venvPythonW
        $shortcut.Arguments = '-m reversehire'
        $shortcut.WorkingDirectory = $InstallDir
        $shortcut.IconLocation = (Join-Path $InstallDir 'icon.ico')
        $shortcut.Description = 'ReverseHire'
        $shortcut.Save()
    }
    Write-Host 'Shortcuts created on the Desktop and in the Start menu.'
}

# --- 6. Microsoft Visual C++ runtime -----------------------------------------------
function Test-VCRuntime {
    $system32 = Join-Path $env:SystemRoot 'System32'
    foreach ($name in 'msvcp140.dll', 'vcruntime140.dll', 'vcruntime140_1.dll') {
        if (-not (Test-Path (Join-Path $system32 $name))) { return $false }
    }
    return $true
}
if (-not (Test-VCRuntime)) {
    $redist = Join-Path $Media 'redist\vc_redist.x64.exe'
    if (Test-Path $redist) {
        Step 'Microsoft Visual C++ runtime (Windows asks for permission)'
        $process = Start-Process -FilePath $redist -ArgumentList '/install /passive /norestart' -Wait -PassThru
        if ($process.ExitCode -notin 0, 1638, 3010) {
            Write-Host "The Visual C++ runtime installer ended with code $($process.ExitCode)." -ForegroundColor Yellow
        }
    } else {
        Write-Host ''
        Write-Host 'NOTE: the Microsoft Visual C++ runtime was not found on this PC.' -ForegroundColor Yellow
        Write-Host 'If a search stops with a model error, install it from' -ForegroundColor Yellow
        Write-Host '  https://aka.ms/vs/17/release/vc_redist.x64.exe' -ForegroundColor Yellow
        Write-Host 'and start ReverseHire again (see the Installation Manual, Troubleshooting).' -ForegroundColor Yellow
    }
}

# --- 7. Self-test with the installed program ---------------------------------------
Step 'Self-test (scoring model offline, OCR engine)'
# No REVERSEHIRE_HOME: the self-test resolves the data folder as the app will
# (portable <InstallDir>\userdata) and finds the model in <InstallDir>\models.
$previousHome = $env:REVERSEHIRE_HOME
Remove-Item Env:REVERSEHIRE_HOME -ErrorAction SilentlyContinue
& $venvPython -m reversehire.diagnostics --selftest | Out-Host
$selftest = $LASTEXITCODE
if ($previousHome) { $env:REVERSEHIRE_HOME = $previousHome }
$warning = $false
if ($selftest -eq 0) {
    Write-Host 'Self-test: PASS' -ForegroundColor Green
} elseif ($selftest -eq 2) {
    $warning = $true
    Write-Host ''
    Write-Host 'Self-test: WARNING - the scoring model cannot be loaded (see the cause above).' -ForegroundColor Yellow
    Write-Host 'ReverseHire works anyway, with reduced accuracy, until the problem is solved.' -ForegroundColor Yellow
    Write-Host 'In the app: menu ?, Diagnostics, for the details (Installation Manual, Troubleshooting).' -ForegroundColor Yellow
} else {
    Fail 'Self-test: FAIL - the installed program does not work (see the lines marked [FAIL] above).'
}

Write-Host ''
if ($warning) {
    Write-Host 'Installation completed with a warning. Start ReverseHire from the Desktop or the Start menu.' -ForegroundColor Yellow
} else {
    Write-Host 'Installation completed. Start ReverseHire from the Desktop or the Start menu.' -ForegroundColor Green
}
Write-Host "Installation log: $LogFile"
Stop-Log
exit 0
