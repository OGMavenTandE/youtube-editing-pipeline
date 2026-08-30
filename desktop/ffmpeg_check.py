"""Friendly FFmpeg / Playwright checks. No raw shell dumps in the UI."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Callable, Iterable, MutableMapping
from dataclasses import dataclass
from pathlib import Path

from pipeline.config import load_settings, which_or_path

from .playwright_runtime import (
    SETTINGS_CHROMIUM_HINT,
    chromium_user_hint,
    playwright_install_argv,
    playwright_install_environ,
)

HKLM_ENV_KEY = r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"
HKCU_ENV_KEY = "Environment"
# winreg.REG_EXPAND_SZ. Kept numeric so tests do not import winreg.
REG_EXPAND_SZ = 2

WINGET_FFMPEG = "winget install Gyan.FFmpeg"
CHOCO_FFMPEG = "choco install ffmpeg"


@dataclass(frozen=True)
class ToolCheck:
    name: str
    found: bool
    path: str | None
    hint: str


def merge_windows_path(machine_path: str, user_path: str, *, pathsep: str = ";") -> str:
    """Join HKLM Path then HKCU Path, skipping empties and duplicates."""
    parts: list[str] = []
    seen: set[str] = set()
    for chunk in (machine_path, user_path):
        for raw in chunk.split(pathsep):
            item = raw.strip().strip('"')
            if not item:
                continue
            key = item.casefold()
            if key in seen:
                continue
            seen.add(key)
            parts.append(item)
    return pathsep.join(parts)


def expand_registry_path(
    value: str,
    value_type: int,
    expander: Callable[[str], str],
) -> str:
    """Expand REG_EXPAND_SZ Path values. REG_SZ is returned as stored."""
    if value_type == REG_EXPAND_SZ:
        return expander(value)
    return value


def refresh_os_path_from_registry(
    machine_path: str,
    user_path: str,
    environ: MutableMapping[str, str] | None = None,
    *,
    pathsep: str = ";",
) -> str:
    """Rebuild environ['PATH'] from machine + user registry Path strings."""
    merged = merge_windows_path(machine_path, user_path, pathsep=pathsep)
    target = os.environ if environ is None else environ
    target["PATH"] = merged
    return merged


def prepend_dir_to_path(
    directory: str | Path,
    environ: MutableMapping[str, str] | None = None,
    *,
    pathsep: str | None = None,
) -> str:
    """Put a bin directory first on PATH for this process."""
    target = os.environ if environ is None else environ
    sep = os.pathsep if pathsep is None else pathsep
    bin_dir = str(directory)
    current = target.get("PATH", "")
    parts = [part for part in current.split(sep) if part and part.casefold() != bin_dir.casefold()]
    rebuilt = sep.join([bin_dir, *parts]) if parts else bin_dir
    target["PATH"] = rebuilt
    return rebuilt


def winget_gyan_ffmpeg_exes(packages_root: Path) -> list[Path]:
    """Gyan.FFmpeg WinGet package layout: ``*/**/bin/ffmpeg.exe``."""
    if not packages_root.is_dir():
        return []
    found: list[Path] = []
    for package_dir in sorted(packages_root.glob("Gyan.FFmpeg*")):
        found.extend(sorted(package_dir.glob("**/bin/ffmpeg.exe")))
    return found


def common_ffmpeg_probe_paths(
    *,
    local_app_data: str | Path | None = None,
    program_files: str | Path | None = None,
    program_files_x86: str | Path | None = None,
    extra: Iterable[Path] = (),
) -> list[Path]:
    """Candidate ffmpeg.exe locations when process PATH is still stale."""
    candidates: list[Path] = []
    if local_app_data:
        winget = Path(local_app_data) / "Microsoft" / "WinGet" / "Packages"
        candidates.extend(winget_gyan_ffmpeg_exes(winget))
    for root in (program_files, program_files_x86):
        if not root:
            continue
        root_path = Path(root)
        candidates.extend(winget_gyan_ffmpeg_exes(root_path / "WinGet" / "Packages"))
        candidates.append(root_path / "ffmpeg" / "bin" / "ffmpeg.exe")
        candidates.append(root_path / "FFmpeg" / "bin" / "ffmpeg.exe")
    candidates.append(Path(r"C:\ffmpeg\bin\ffmpeg.exe"))
    candidates.extend(extra)
    return candidates


def first_existing_file(paths: Iterable[Path]) -> Path | None:
    for path in paths:
        if path.is_file():
            return path
    return None


def sibling_ffprobe(ffmpeg_path: str | Path) -> Path | None:
    """ffprobe.exe / ffprobe next to a found ffmpeg binary."""
    parent = Path(ffmpeg_path).parent
    for name in ("ffprobe.exe", "ffprobe"):
        candidate = parent / name
        if candidate.is_file():
            return candidate
    return None


def _winreg_read_path(hive: int, subkey: str) -> str:
    """Read Path from a Windows environment registry key."""
    try:
        import winreg
    except ImportError:
        return ""
    try:
        with winreg.OpenKey(hive, subkey) as key:
            raw, value_type = winreg.QueryValueEx(key, "Path")
    except OSError:
        return ""
    if not isinstance(raw, str):
        return ""
    return expand_registry_path(raw, value_type, winreg.ExpandEnvironmentStrings)


def read_windows_registry_path() -> str:
    """HKLM Session Manager Environment Path + HKCU Environment Path."""
    try:
        import winreg
    except ImportError:
        return ""
    machine = _winreg_read_path(winreg.HKEY_LOCAL_MACHINE, HKLM_ENV_KEY)
    user = _winreg_read_path(winreg.HKEY_CURRENT_USER, HKCU_ENV_KEY)
    return merge_windows_path(machine, user)


def refresh_windows_path(environ: MutableMapping[str, str] | None = None) -> str:
    """Replace process PATH with the live registry PATH (Windows only)."""
    target = os.environ if environ is None else environ
    if sys.platform != "win32":
        return target.get("PATH", "")
    merged = read_windows_registry_path()
    target["PATH"] = merged
    return merged


def probe_windows_ffmpeg(
    *,
    local_app_data: str | Path | None = None,
    program_files: str | Path | None = None,
    program_files_x86: str | Path | None = None,
    extra: Iterable[Path] = (),
) -> Path | None:
    return first_existing_file(
        common_ffmpeg_probe_paths(
            local_app_data=local_app_data,
            program_files=program_files,
            program_files_x86=program_files_x86,
            extra=extra,
        )
    )


def _default_probe_ffmpeg() -> Path | None:
    return probe_windows_ffmpeg(
        local_app_data=os.environ.get("LOCALAPPDATA"),
        program_files=os.environ.get("ProgramFiles") or os.environ.get("PROGRAMFILES"),
        program_files_x86=os.environ.get("ProgramFiles(x86)") or os.environ.get("PROGRAMFILES(X86)"),
    )


def locate_ffmpeg(preferred: str = "ffmpeg") -> str | None:
    """Find ffmpeg, refreshing Windows PATH from the registry on Recheck."""
    if sys.platform == "win32":
        refresh_windows_path()
    found = which_or_path(preferred)
    if found:
        _adopt_ffmpeg_bin(found)
        return found
    if sys.platform == "win32":
        for name in ("ffmpeg", "ffmpeg.exe"):
            found = shutil.which(name)
            if found:
                _adopt_ffmpeg_bin(found)
                return found
        probed = _default_probe_ffmpeg()
        if probed:
            _adopt_ffmpeg_bin(str(probed))
            return str(probed)
    return None


def _adopt_ffmpeg_bin(ffmpeg_path: str) -> None:
    """Prepend ffmpeg's directory so this process can also see ffprobe."""
    bin_dir = Path(ffmpeg_path).parent
    prepend_dir_to_path(bin_dir)
    sibling_ffprobe(ffmpeg_path)


def check_ffmpeg() -> ToolCheck:
    settings = load_settings()
    path = locate_ffmpeg(settings.ffmpeg_bin)
    return ToolCheck(
        name="FFmpeg",
        found=path is not None,
        path=path,
        hint=f"{WINGET_FFMPEG}\n{CHOCO_FFMPEG}",
    )


def playwright_chromium_ok() -> bool:
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        return False
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            browser.close()
        return True
    except Exception:
        return False


def check_playwright() -> ToolCheck:
    found = playwright_chromium_ok()
    return ToolCheck(
        name="Playwright Chromium",
        found=found,
        path=None,
        hint=chromium_user_hint(),
    )


def install_playwright_chromium() -> tuple[bool, str]:
    """Install Chromium once. Frozen EXE uses bundled node+cli, not system Python."""
    command = _playwright_install_argv()
    if command is None:
        return False, (
            "Could not find the bundled Playwright driver. "
            f"{SETTINGS_CHROMIUM_HINT}"
        )
    try:
        from pipeline.hidden_process import run_hidden

        result = run_hidden(
            command,
            capture_output=True,
            text=True,
            timeout=600,
            check=False,
            env=playwright_install_environ(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return False, (
            "Chromium could not be installed from the app. "
            f"{chromium_user_hint()}"
        )
    if result.returncode != 0:
        return False, (
            "Chromium could not be installed from the app. "
            f"{chromium_user_hint()}"
        )
    if playwright_chromium_ok():
        return True, "Chromium is installed."
    return False, (
        "The installer finished but Chromium is still missing. "
        f"{chromium_user_hint()}"
    )


def _playwright_install_argv() -> list[str] | None:
    return playwright_install_argv()


def copyable_ffmpeg_help() -> str:
    return f"{WINGET_FFMPEG}\n{CHOCO_FFMPEG}\n\nOr from Chocolatey / Gyan builds, then click Recheck."


def ffmpeg_bin_on_path() -> Path | None:
    check = check_ffmpeg()
    return Path(check.path) if check.path else None
