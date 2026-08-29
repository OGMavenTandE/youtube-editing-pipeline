"""Start with Windows via a Startup-folder launcher. No-op on other platforms."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from desktop.paths import launch_command

STARTUP_NAME = "YouTube Pipeline.bat"


def startup_dir() -> Path | None:
    if sys.platform != "win32":
        return None
    appdata = os.getenv("APPDATA")
    if not appdata:
        return None
    return Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


def startup_launcher_path() -> Path | None:
    folder = startup_dir()
    if folder is None:
        return None
    return folder / STARTUP_NAME


def startup_enabled() -> bool:
    path = startup_launcher_path()
    return bool(path and path.is_file())


def set_startup(enabled: bool, command: list[str] | None = None) -> bool:
    """Create or remove the Startup-folder launcher. Returns whether it is enabled."""
    path = startup_launcher_path()
    if path is None:
        return False
    if not enabled:
        if path.is_file():
            path.unlink()
        return False
    argv = command or launch_command()
    quoted = " ".join(f'"{part}"' if " " in part else part for part in argv)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'@echo off\nstart "" {quoted}\n', encoding="utf-8")
    return True
