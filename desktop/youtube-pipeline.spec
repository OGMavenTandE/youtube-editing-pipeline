# -*- mode: python ; coding: utf-8 -*-
"""One-folder Windows build. Chromium is bundled; FFmpeg stays on the PC."""

import sys
from pathlib import Path

from PyInstaller.building.api import COLLECT, EXE, PYZ
from PyInstaller.building.build_main import Analysis
from PyInstaller.utils.hooks import collect_all, collect_data_files, collect_submodules

SPEC_DIR = Path(SPECPATH).resolve()
ROOT = SPEC_DIR.parent if SPEC_DIR.name == "desktop" else SPEC_DIR

# Spec lives in desktop/; collect_submodules needs the repo root on sys.path.
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

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

# Every first-party module. Do not hand-list a subset.
hiddenimports += collect_submodules("desktop")
hiddenimports += collect_submodules("pipeline")

# Collect the desktop package itself (package data + modules).
desk_datas, desk_binaries, desk_hidden = collect_all("desktop")
datas += desk_datas
binaries += desk_binaries
hiddenimports += desk_hidden
datas += collect_data_files("desktop", include_py_files=True)

datas += collect_data_files("pipeline")
datas += [
    (str(ROOT / "pipeline" / "broll" / "templates"), "pipeline/broll/templates"),
    (str(ROOT / "pipeline" / "fonts"), "pipeline/fonts"),
    (str(ROOT / "run.py"), "."),
    (str(ROOT / ".env.example"), "."),
]


def _playwright_local_browsers():
    try:
        import playwright
    except Exception:
        return None
    path = Path(playwright.__file__).resolve().parent / "driver" / "package" / ".local-browsers"
    if path.is_dir() and any(path.iterdir()):
        return path
    return None


_pw_browsers = _playwright_local_browsers()
if _pw_browsers is not None:
    datas += [(str(_pw_browsers), "playwright/driver/package/.local-browsers")]

hiddenimports += [
    "run",
]

# Import desktop as a package. Do not freeze desktop/app.py as a bare script
# named "app" — that makes `import desktop.worker` fail at runtime.
a = Analysis(
    [str(ROOT / "desktop" / "__main__.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[str(SPEC_DIR / "rthooks" / "pyi_rth_syspath.py")],
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
