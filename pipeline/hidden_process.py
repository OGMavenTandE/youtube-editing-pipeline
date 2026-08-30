"""Hide child-process consoles on Windows.

ffmpeg, ffprobe, auto-editor, MoviePy, and pydub all spawn tools via
subprocess. On Windows, that flashes a console even with capture_output=True
unless CREATE_NO_WINDOW is set. The CustomTkinter tray/wizard is this
process; do not hide it.
"""

from __future__ import annotations

import subprocess
import sys
from typing import Any

# Windows CREATE_NO_WINDOW. Defined here so tests can assert the value on Linux.
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)

_original_popen: Any | None = None


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


def run_hidden(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[Any]:
    """subprocess.run with Windows console-hide flags merged in."""
    extra = hidden_popen_kwargs()
    if extra:
        flags = int(kwargs.get("creationflags", 0) or 0) | int(extra.get("creationflags", 0) or 0)
        kwargs["creationflags"] = flags
        if extra.get("startupinfo") is not None and kwargs.get("startupinfo") is None:
            kwargs["startupinfo"] = extra["startupinfo"]
    return subprocess.run(args, **kwargs)


def install_hidden_subprocess() -> None:
    """Patch subprocess.Popen so ffmpeg-python, MoviePy, and pydub stay windowless.

    No-op on non-Windows. Safe to call more than once.
    """
    global _original_popen
    if sys.platform != "win32" or _original_popen is not None:
        return
    extra = hidden_popen_kwargs()
    if not extra:
        return
    _original_popen = subprocess.Popen

    def hidden_popen(*args: Any, **kwargs: Any) -> Any:
        flags = int(kwargs.get("creationflags", 0) or 0) | int(extra.get("creationflags", 0) or 0)
        kwargs["creationflags"] = flags
        if extra.get("startupinfo") is not None and kwargs.get("startupinfo") is None:
            kwargs["startupinfo"] = extra["startupinfo"]
        return _original_popen(*args, **kwargs)

    subprocess.Popen = hidden_popen  # type: ignore[misc, assignment]
