; Inno Setup script of the Windows installer (built by .github/workflows/release.yml).
;   iscc /DAppVersion=<version> /DSourceDir=<folder with ReverseHire.exe> /DOutputDir=<out> reversehire.iss
; Per-user installation (no administrator rights) into
; %LOCALAPPDATA%\Programs\ReverseHire Standalone (not ...\ReverseHire, the folder of the
; offline-disc installation, which holds a Python environment). The data goes to the "userdata"
; folder inside it (the program folder is writable); uninstalling removes the program
; files and leaves "userdata" in place.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef SourceDir
  #define SourceDir "..\..\..\dist\ReverseHire"
#endif
#ifndef OutputDir
  #define OutputDir "..\..\..\dist"
#endif

[Setup]
AppId={{6C0F3E1B-3B5D-4E0E-9C51-0A7E5F2B9D41}
AppName=ReverseHire
AppVersion={#AppVersion}
AppPublisher=0THack1A
AppPublisherURL=https://github.com/OTHack1A/ReverseHire
AppSupportURL=https://github.com/OTHack1A/ReverseHire/issues
DefaultDirName={localappdata}\Programs\ReverseHire Standalone
DisableDirPage=auto
DefaultGroupName=ReverseHire
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir={#OutputDir}
OutputBaseFilename=ReverseHire-{#AppVersion}-setup
SetupIconFile=..\..\..\src\reversehire\resources\icon.ico
UninstallDisplayIcon={app}\ReverseHire.exe
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
LicenseFile=..\..\..\LICENSE

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"
Name: "italian"; MessagesFile: "compiler:Languages\Italian.isl"
Name: "ukrainian"; MessagesFile: "compiler:Languages\Ukrainian.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[InstallDelete]
; An update replaces the bundled libraries completely (never the "userdata" folder).
Type: filesandordirs; Name: "{app}\_internal"

[Icons]
Name: "{autoprograms}\ReverseHire"; Filename: "{app}\ReverseHire.exe"
Name: "{autodesktop}\ReverseHire"; Filename: "{app}\ReverseHire.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\ReverseHire.exe"; Description: "{cm:LaunchProgram,ReverseHire}"; Flags: nowait postinstall skipifsilent
