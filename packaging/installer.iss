; Inno Setup script (PRD s.13). Installs the one-folder build and creates the user data folder
; layout outside the install folder (FR-13.4): Documents\AlgoTrader\{config,plugins,data,state,reports}.
; Built automatically by .github/workflows/windows-installer.yml. Sign the installer for release (FR-13.3).
#ifndef AppVersion
  #define AppVersion "0.1.0"
#endif
#define DataDir "{userdocs}\AlgoTrader"

[Setup]
AppId={{6F1C2A4E-3B7D-4C1A-9E52-7A0B9D3C2F11}
AppName=AlgoTrader
AppVersion={#AppVersion}
AppPublisher=AlgoTrader (personal use)
DefaultDirName={autopf}\AlgoTrader
DefaultGroupName=AlgoTrader
OutputDir=..\dist\installer
OutputBaseFilename=AlgoTrader-Setup-{#AppVersion}
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
; Per-user install: no administrator prompt; {autopf} maps to the user's Programs folder.
PrivilegesRequired=lowest
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\AlgoTraderUI.exe

[Files]
Source: "..\dist\AlgoTrader\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion createallsubdirs
; Configuration and plugins are copied once; upgrades never overwrite the operator's edits.
Source: "..\config\*"; DestDir: "{#DataDir}\config"; Flags: onlyifdoesntexist recursesubdirs
Source: "..\plugins\*.py"; DestDir: "{#DataDir}\plugins"; Flags: onlyifdoesntexist
Source: "..\README.md"; DestDir: "{app}"; Flags: ignoreversion

[Dirs]
Name: "{#DataDir}\data"
Name: "{#DataDir}\state"
Name: "{#DataDir}\reports"

[Icons]
Name: "{group}\AlgoTrader (desktop UI)"; Filename: "{app}\AlgoTraderUI.exe"; Parameters: """{#DataDir}\config"""; WorkingDir: "{#DataDir}"
Name: "{group}\AlgoTrader engine (paper)"; Filename: "{app}\AlgoTrader.exe"; Parameters: "engine --config ""{#DataDir}\config"""; WorkingDir: "{#DataDir}"
Name: "{group}\AlgoTrader paper replay (demo)"; Filename: "{app}\AlgoTrader.exe"; Parameters: "replay --config ""{#DataDir}\config"" --start 2026-01-01 --wait"; WorkingDir: "{#DataDir}"
Name: "{group}\AlgoTrader command prompt"; Filename: "{cmd}"; Parameters: "/k set ""PATH={app};%PATH%"""; WorkingDir: "{#DataDir}"
Name: "{group}\AlgoTrader data folder"; Filename: "{#DataDir}"
Name: "{group}\Uninstall AlgoTrader"; Filename: "{uninstallexe}"
Name: "{userdesktop}\AlgoTrader"; Filename: "{app}\AlgoTraderUI.exe"; Parameters: """{#DataDir}\config"""; WorkingDir: "{#DataDir}"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"
Name: "demodata"; Description: "Generate synthetic demo data (needed to try the system without a data feed)"; GroupDescription: "Data:"

[Run]
Filename: "{app}\AlgoTrader.exe"; Parameters: "make-synthetic --out ""{#DataDir}\data"""; StatusMsg: "Generating demo data..."; Flags: runhidden; Tasks: demodata
Filename: "{app}\AlgoTrader.exe"; Parameters: "validate-config --config ""{#DataDir}\config"""; StatusMsg: "Validating configuration..."; Flags: runhidden
Filename: "{app}\AlgoTraderUI.exe"; Parameters: """{#DataDir}\config"""; WorkingDir: "{#DataDir}"; Description: "Open AlgoTrader"; Flags: postinstall nowait skipifsilent
