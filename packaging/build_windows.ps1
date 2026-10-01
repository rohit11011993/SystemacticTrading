# Scripted Windows build (FR-13.1). Run from the repository root in PowerShell.
$ErrorActionPreference = "Stop"
python -m venv .build-venv
.\.build-venv\Scripts\pip install --upgrade pip
.\.build-venv\Scripts\pip install -r requirements.txt PySide6 pyinstaller
.\.build-venv\Scripts\python -m pytest -q                       # release gate: tests must pass
.\.build-venv\Scripts\pyinstaller packaging\AlgoTrader.spec --clean --noconfirm --distpath dist --workpath build
# Record release hashes (FR-13.8)
Get-FileHash dist\AlgoTrader\AlgoTrader.exe -Algorithm SHA256 | Format-List | Out-File dist\RELEASE_HASHES.txt
git rev-parse HEAD | Out-File -Append dist\RELEASE_HASHES.txt
Write-Host "Now sign dist\AlgoTrader\AlgoTrader.exe and build the installer with Inno Setup (packaging\installer.iss)."
