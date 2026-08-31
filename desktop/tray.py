"""System tray icon. Close-to-tray lives in the main window; quit is here."""

from __future__ import annotations

import threading
from typing import Callable

from .worker import JobStatus

ShowFn = Callable[[], None]
QuitFn = Callable[[], None]
PauseFn = Callable[[], None]
RunFn = Callable[[], None]


def _icon_image(status: JobStatus):
    from PIL import Image, ImageDraw

    color = (46, 160, 67) if status != JobStatus.PROCESSING else (218, 165, 32)
    if status == JobStatus.TALK_SHEET:
        color = (224, 180, 74)
    if status == JobStatus.ERROR:
        color = (192, 57, 43)
    image = Image.new("RGBA", (64, 64), (20, 24, 28, 255))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((6, 6, 58, 58), radius=14, fill=color)
    draw.polygon([(24, 18), (24, 46), (48, 32)], fill=(20, 24, 28))
    return image


class TrayController:
    def __init__(
        self,
        *,
        on_show: ShowFn,
        on_quit: QuitFn,
        on_pause: PauseFn,
        on_run_once: RunFn,
        paused: bool = False,
    ) -> None:
        self._on_show = on_show
        self._on_quit = on_quit
        self._on_pause = on_pause
        self._on_run_once = on_run_once
        self._paused = paused
        self._status = JobStatus.WATCHING
        self._icon = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        try:
            import pystray
            from pystray import Menu, MenuItem
        except Exception:
            return

        self._icon = pystray.Icon(
            "youtube-pipeline",
            _icon_image(self._status),
            "YouTube Pipeline — Watching",
            Menu(
                MenuItem("Show", lambda *_: self._on_show(), default=True),
                MenuItem(
                    lambda text: "Resume watching" if self._paused else "Pause watching",
                    lambda *_: self._on_pause(),
                ),
                MenuItem("Run once now", lambda *_: self._on_run_once()),
                MenuItem("Quit", lambda *_: self._on_quit()),
            ),
        )
        self._thread = threading.Thread(target=self._icon.run, name="pipeline-tray", daemon=True)
        self._thread.start()

    def set_status(self, status: JobStatus, paused: bool = False) -> None:
        self._status = status
        self._paused = paused
        if self._icon is None:
            return
        label = "Paused" if paused and status == JobStatus.IDLE else status.value
        try:
            self._icon.icon = _icon_image(status)
            self._icon.title = f"YouTube Pipeline — {label}"
        except Exception:
            pass

    def stop(self) -> None:
        if self._icon is None:
            return
        try:
            self._icon.stop()
        except Exception:
            pass
        self._icon = None
