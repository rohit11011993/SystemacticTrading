; Inno Setup script (PRD s.13). Installs the one-folder build and creates the user data folder
; layout outside the install folder (FR-13.4). Sign the resulting installer (FR-13.3).
#define AppVersion "0.1.0"

[Setup]
AppName=AlgoTrader
AppVersion={#AppVersion}
DefaultDirName={autopf}\AlgoTrader
DefaultGroupName=AlgoTrader
OutputBaseFilename=AlgoTrader-Setup-{#AppVersion}
ArchitecturesInstallIn64BitMode=x64
PrivilegesRequired=admin

[Files]
Source: "..\dist\AlgoTrader\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion
; Default configuration and plugins are copied once; later upgrades never overwrite them.
Source: "..\config\*"; DestDir: "{userdocs}\AlgoTrader\config"; Flags: onlyifdoesntexist recursesubdirs
Source: "..\plugins\*.py"; DestDir: "{userdocs}\AlgoTrader\plugins"; Flags: onlyifdoesntexist

[Dirs]
Name: "{userdocs}\AlgoTrader\data"
Name: "{userdocs}\AlgoTrader\state"
Name: "{userdocs}\AlgoTrader\reports"

[Icons]
Name: "{group}\AlgoTrader"; Filename: "{app}\AlgoTraderUI.exe"; Parameters: """{userdocs}\AlgoTrader\config"""
Name: "{group}\AlgoTrader status"; Filename: "{app}\AlgoTrader.exe"; Parameters: "status --config ""{userdocs}\AlgoTrader\config"""
