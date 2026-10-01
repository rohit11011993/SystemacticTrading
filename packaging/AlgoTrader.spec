# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for AlgoTrader.exe and AlgoTraderUI.exe (PRD s.13, FR-13.1 / FR-13.2).
#
# Build on 64-bit Windows 10/11 (locally with packaging\build_windows.ps1, or in CI with
# .github/workflows/windows-installer.yml):
#     pyinstaller packaging\AlgoTrader.spec --clean --noconfirm --distpath dist --workpath build
#
# One-FOLDER mode is used on purpose: one-file builds unpack to a temp folder on every start,
# are slower and are flagged more often by antivirus software (FR-13.2).
#
# Strategy plugins, configuration, data, state and logs are NOT bundled: they live in the user
# data folder (FR-13.4, FR-5.5) so they can change without a rebuild. Plugins may only import
# libraries that ship in this bundle (FR-13.5) - numpy, pandas, pydantic and the stdlib.
import os
import sys

from PyInstaller.utils.hooks import collect_submodules

HERE = SPECPATH                                   # folder of this spec (set by PyInstaller)
ROOT = os.path.abspath(os.path.join(HERE, ".."))  # repository root
# collect_submodules() imports the package in THIS process, so the repository root must be on
# sys.path here - `pathex` below only applies to the later dependency analysis.
sys.path.insert(0, ROOT)

# Strategy plugins live OUTSIDE the executable and import algotrader.strategy.api at runtime.
# Nothing inside the app imports that facade, so PyInstaller cannot discover it by analysis:
# every algotrader module is bundled explicitly, and the build fails loudly if that list is empty.
app_modules = collect_submodules("algotrader")
for required in ("algotrader.strategy.api", "algotrader.indicators", "algotrader.data.provider"):
    if required not in app_modules:
        raise SystemExit(f"spec error: {required} not collected - is the repository root importable?")
hidden = app_modules + ["yaml", "pydantic", "numpy", "pandas",
                        # Kite Connect client and the OS credential store backend (FR-15.1)
                        "kiteconnect", "keyring", "keyring.backends.Windows", "win32ctypes.pywin32.win32cred"]
excludes = ["tkinter", "matplotlib", "IPython", "pytest", "hypothesis"]

# Command-line / engine executable.
a = Analysis([os.path.join(HERE, "launcher.py")], pathex=[ROOT], hiddenimports=hidden, excludes=excludes)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,       # one-folder build
    name="AlgoTrader",
    console=True,                # headless engine / CLI
    codesign_identity=None,      # sign with signtool after the build (FR-13.3)
)

# Desktop UI (PRD s.12): a separate windowed executable in the same folder. It attaches to the
# engine through the state database, so closing it never stops the engine.
ui = Analysis([os.path.join(HERE, "launcher_ui.py")], pathex=[ROOT],
              hiddenimports=hidden + ["PySide6.QtWidgets", "PySide6.QtGui", "PySide6.QtCore"],
              excludes=excludes)
ui_pyz = PYZ(ui.pure)
ui_exe = EXE(ui_pyz, ui.scripts, [], exclude_binaries=True, name="AlgoTraderUI", console=False,
             codesign_identity=None)

coll = COLLECT(exe, a.binaries, a.datas, ui_exe, ui.binaries, ui.datas, strip=False, upx=False,
               name="AlgoTrader")
