# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for AlgoTrader.exe (PRD s.13, FR-13.1 / FR-13.2).
#
# Build on 64-bit Windows 10/11 from a clean virtual environment with hash-pinned requirements:
#     pip install --require-hashes -r requirements.lock
#     pyinstaller packaging\AlgoTrader.spec --clean --noconfirm
#
# One-FOLDER mode is used on purpose: one-file builds unpack to a temp folder on every start,
# are slower and are flagged more often by antivirus software (FR-13.2).
#
# Strategy plugins, configuration, data, state and logs are NOT bundled: they live in the user
# data folder (FR-13.4, FR-5.5) so they can change without a rebuild. Plugins may only import
# libraries that ship in this bundle (FR-13.5) - numpy, pandas, pydantic and the stdlib.
from PyInstaller.utils.hooks import collect_submodules

hidden = collect_submodules("algotrader") + ["yaml", "pydantic", "numpy", "pandas"]

a = Analysis(
    ["launcher.py"],
    pathex=[".."],
    hiddenimports=hidden,
    excludes=["tkinter", "matplotlib", "IPython", "pytest"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,       # one-folder build
    name="AlgoTrader",
    console=True,                # headless engine / CLI; the PySide6 UI is a separate target
    disable_windowed_traceback=False,
    codesign_identity=None,      # sign with signtool after the build (FR-13.3)
)

# Desktop UI (PRD s.12): a separate windowed executable in the same folder. It attaches to the
# engine through the state database, so closing it never stops the engine.
ui = Analysis(["launcher_ui.py"], pathex=[".."], hiddenimports=hidden + ["PySide6.QtWidgets", "PySide6.QtGui"],
              excludes=["tkinter", "matplotlib", "IPython", "pytest"])
ui_pyz = PYZ(ui.pure)
ui_exe = EXE(ui_pyz, ui.scripts, [], exclude_binaries=True, name="AlgoTraderUI", console=False,
             codesign_identity=None)

coll = COLLECT(exe, a.binaries, a.datas, ui_exe, ui.binaries, ui.datas, strip=False, upx=False,
               name="AlgoTrader")
