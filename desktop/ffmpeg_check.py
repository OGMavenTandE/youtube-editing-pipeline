"""Friendly FFmpeg / Playwright checks. No raw shell dumps in the UI."""

from __future__ import annotations

import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from pipeline.config import load_settings, which_or_path

WINGET_FFMPEG = "winget install Gyan.FFmpeg"
CHOCO_FFMPEG = "choco install ffmpeg"
PLAYWRIGHT_INSTALL = "playwright install chromium"


@dataclass(frozen=True)
class ToolCheck:
    name: str
    found: bool
    path: str | None
    hint: str


def check_ffmpeg() -> ToolCheck:
    settings = load_settings()
    path = which_or_path(settings.ffmpeg_bin)
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
        hint=PLAYWRIGHT_INSTALL,
    )


def install_playwright_chromium() -> tuple[bool, str]:
    """Run the official install once. Returns (ok, friendly message)."""
    command = _playwright_install_argv()
    if command is None:
        return False, (
            "Could not find a Python that can run Playwright. "
            f"In a terminal, run: {PLAYWRIGHT_INSTALL}"
        )
    try:
        from pipeline.hidden_process import run_hidden

        result = run_hidden(
            command,
            capture_output=True,
            text=True,
            timeout=600,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False, (
            "Chromium could not be installed from the app. "
            f"In a terminal, run: {PLAYWRIGHT_INSTALL}"
        )
    if result.returncode != 0:
        return False, (
            "Chromium could not be installed from the app. "
            f"In a terminal, run: {PLAYWRIGHT_INSTALL}"
        )
    if playwright_chromium_ok():
        return True, "Chromium is installed."
    return False, (
        "The installer finished but Chromium is still missing. "
        f"In a terminal, run: {PLAYWRIGHT_INSTALL}"
    )


def _playwright_install_argv() -> list[str] | None:
    if not getattr(sys, "frozen", False):
        return [sys.executable, "-m", "playwright", "install", "chromium"]
    python = shutil.which("python") or shutil.which("python3") or shutil.which("py")
    if python:
        return [python, "-m", "playwright", "install", "chromium"]
    return None


def copyable_ffmpeg_help() -> str:
    return f"{WINGET_FFMPEG}\n{CHOCO_FFMPEG}\n\nOr from Chocolatey / Gyan builds, then click Recheck."


def ffmpeg_bin_on_path() -> Path | None:
    check = check_ffmpeg()
    return Path(check.path) if check.path else None
