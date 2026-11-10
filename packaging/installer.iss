; Inno Setup script. Built by .github/workflows/build.yml:
;   iscc /DAppVersion=0.1.0 /DSourceDir=<pyinstaller dist\Photo2PDF> packaging\installer.iss
;
; Per-user install: no admin prompt, lands in %LOCALAPPDATA%\Programs.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef SourceDir
  #define SourceDir "..\dist\Photo2PDF"
#endif

[Setup]
AppId={{6E0B3F4C-2B7A-4C59-9D7E-0B5C2B1F7A11}
AppName=Photos to PDF
AppVersion={#AppVersion}
AppPublisher=Photos to PDF
DefaultDirName={localappdata}\Programs\Photos to PDF
DefaultGroupName=Photos to PDF
DisableProgramGroupPage=yes
DisableDirPage=yes
PrivilegesRequired=lowest
OutputBaseFilename=PhotosToPDF-Setup-{#AppVersion}
OutputDir=..\dist
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\Photo2PDF.exe
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

[Tasks]
Name: "desktopicon"; Description: "Put a shortcut on the desktop (you can drag photos onto it)"; Flags: checkedonce

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion
Source: "..\HOW_TO_USE.txt"; DestDir: "{app}"; Flags: isreadme

[Icons]
Name: "{userprograms}\Photos to PDF"; Filename: "{app}\Photo2PDF.exe"
Name: "{userdesktop}\Photos to PDF"; Filename: "{app}\Photo2PDF.exe"; Tasks: desktopicon
; Right-click photos -> Send to -> Photos to PDF
Name: "{usersendto}\Photos to PDF"; Filename: "{app}\Photo2PDF.exe"

[Run]
Filename: "{app}\Photo2PDF.exe"; Description: "Open Photos to PDF now"; Flags: nowait postinstall skipifsilent
