# -*- mode: python ; coding: utf-8 -*-
"""One-folder Windows build. Playwright Chromium and FFmpeg stay on the PC."""

from pathlib import Path

from PyInstaller.building.api import COLLECT, EXE, PYZ
from PyInstaller.building.build_main import Analysis
from PyInstaller.utils.hooks import collect_all, collect_data_files

SPEC_DIR = Path(SPECPATH).resolve()
ROOT = SPEC_DIR.parent if SPEC_DIR.name == "desktop" else SPEC_DIR

datas: list = []
binaries: list = []
hiddenimports: list = []

for package in (
    "customtkinter",
    "moviepy",
    "imageio",
    "imageio_ffmpeg",
    "googleapiclient",
    "google.auth",
    "google_auth_oauthlib",
    "playwright",
    "pydub",
    "PIL",
    "pystray",
):
    try:
        extra_datas, extra_binaries, extra_hidden = collect_all(package)
    except Exception:
        continue
    datas += extra_datas
    binaries += extra_binaries
    hiddenimports += extra_hidden

datas += collect_data_files("pipeline")
datas += [
    (str(ROOT / "pipeline" / "broll" / "templates"), "pipeline/broll/templates"),
    (str(ROOT / "run.py"), "."),
    (str(ROOT / ".env.example"), "."),
]

hiddenimports += [
    "run",
    "pipeline",
    "pipeline.broll.slides",
    "pipeline.broll.local",
    "pipeline.compositor",
    "pipeline.drive_io",
    "pipeline.repack",
    "pipeline.studio",
    "pipeline.gemini_director",
    "desktop.app",
    "desktop.main_window",
    "desktop.wizard",
    "desktop.worker",
    "desktop.tray",
]

a = Analysis(
    [str(ROOT / "desktop" / "app.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["streamlit"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="youtube-pipeline",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    name="youtube-pipeline",
)
