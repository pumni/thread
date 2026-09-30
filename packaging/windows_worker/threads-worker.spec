# -*- mode: python ; coding: utf-8 -*-

import platform
import sys
from pathlib import Path

from PyInstaller.utils.hooks import copy_metadata


ROOT = Path.cwd().resolve()
if sys.platform != "win32" or platform.machine().casefold() not in {"amd64", "x86_64"}:
    raise RuntimeError("Windows Worker packaging must run on Windows x64")

entry_point = ROOT / "src" / "threads_platform" / "workers" / "__main__.py"
if not entry_point.is_file():
    raise RuntimeError("Worker package entry point is missing")

analysis = Analysis(
    [str(entry_point)],
    pathex=[str(ROOT / "src")],
    binaries=[],
    datas=copy_metadata("threads-platform"),
    hiddenimports=[],
    hookspath=[],
    runtime_hooks=[],
    excludes=["pytest", "_pytest", "tests"],
    noarchive=False,
)
python_archive = PYZ(analysis.pure)
worker_executable = EXE(
    python_archive,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name="threads-worker",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    uac_admin=False,
    uac_uiaccess=False,
    contents_directory="_internal",
)
COLLECT(
    worker_executable,
    analysis.binaries,
    analysis.datas,
    strip=False,
    upx=False,
    name="threads-worker",
)
