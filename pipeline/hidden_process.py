"""Hide child-process consoles on Windows.

ffmpeg, ffprobe, auto-editor, MoviePy, and pydub all spawn tools via
subprocess. On Windows, that flashes a console even with capture_output=True
unless CREATE_NO_WINDOW is set. The CustomTkinter tray/wizard is this
process; do not hide it.

subprocess.Popen must stay a class. CPython asyncio/windows_utils.py does
`class Popen(subprocess.Popen)` on Windows. Assigning a function there
raises TypeError: function() argument 'code' must be code, not str.
"""

from __future__ import annotations

import subprocess
import sys
from typing import Any

# Windows CREATE_NO_WINDOW. Defined here so tests can assert the value on Linux.
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)

_original_popen: type[subprocess.Popen[Any]] | None = None


def hidden_popen_kwargs() -> dict[str, Any]:
    """creationflags on win32. Empty dict on other platforms."""
    if sys.platform != "win32":
        return {}
    kwargs: dict[str, Any] = {"creationflags": CREATE_NO_WINDOW}
    startupinfo_cls = getattr(subprocess, "STARTUPINFO", None)
    if startupinfo_cls is not None:
        info = startupinfo_cls()
        info.dwFlags |= getattr(subprocess, "STARTF_USESHOWWINDOW", 1)
        info.wShowWindow = getattr(subprocess, "SW_HIDE", 0)
        kwargs["startupinfo"] = info
    return kwargs


def _merge_hidden_kwargs(kwargs: dict[str, Any]) -> dict[str, Any]:
    extra = hidden_popen_kwargs()
    if not extra:
        return kwargs
    merged = dict(kwargs)
    flags = int(merged.get("creationflags", 0) or 0) | int(extra.get("creationflags", 0) or 0)
    merged["creationflags"] = flags
    if extra.get("startupinfo") is not None and merged.get("startupinfo") is None:
        merged["startupinfo"] = extra["startupinfo"]
    return merged


class HiddenPopen(subprocess.Popen):
    """subprocess.Popen that ORs in CREATE_NO_WINDOW / STARTUPINFO on Windows."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **_merge_hidden_kwargs(kwargs))


def run_hidden(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[Any]:
    """subprocess.run with Windows console-hide flags merged in."""
    return subprocess.run(args, **_merge_hidden_kwargs(kwargs))


def install_hidden_subprocess() -> None:
    """Patch subprocess.Popen so ffmpeg-python, MoviePy, and pydub stay windowless.

    Assigns HiddenPopen (a class), never a function. Safe to call more than once.
    No-op on non-Windows.
    """
    global _original_popen
    if sys.platform != "win32":
        return
    if _original_popen is not None or subprocess.Popen is HiddenPopen:
        return
    _original_popen = subprocess.Popen
    subprocess.Popen = HiddenPopen
