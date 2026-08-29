#!/usr/bin/env python3
"""Windows desktop entry for the YouTube talking-head pipeline."""

from __future__ import annotations

import argparse
import io
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

if sys.stdout is None:
    sys.stdout = io.StringIO()
if sys.stderr is None:
    sys.stderr = io.StringIO()

from desktop.config_store import AppConfig, ensure_user_data, load_config
from desktop.paths import install_root
from desktop.tray import TrayController
from desktop.worker import JobResult, JobStatus, PipelineWorker, prepare_runtime_env
from desktop.wizard import WizardView


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="youtube-pipeline", description="Drive inbox watcher.")
    parser.add_argument("--tray", action="store_true", help="Start hidden in the system tray.")
    parser.add_argument("--setup", action="store_true", help="Force the first-run wizard.")
    return parser


class DesktopApp:
    def __init__(self, *, start_in_tray: bool, force_setup: bool) -> None:
        import customtkinter as ctk

        prepare_runtime_env()
        ensure_user_data()
        (install_root() / "input").mkdir(parents=True, exist_ok=True)
        (install_root() / "output").mkdir(parents=True, exist_ok=True)
        (install_root() / "work").mkdir(parents=True, exist_ok=True)

        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")
        self.root = ctk.CTk()
        self.root.title("YouTube Pipeline")
        self.root.geometry("760x620")
        self.root.minsize(680, 520)
        self._main = None
        self._wizard = None
        self._quitting = False

        self.worker = PipelineWorker(
            log=self._ui_log,
            status=self._ui_status,
            on_job_done=self._ui_job_done,
        )
        self.tray = TrayController(
            on_show=self.show,
            on_quit=self.quit_app,
            on_pause=self._tray_pause,
            on_run_once=self.worker.run_once,
            paused=False,
        )

        self.root.protocol("WM_DELETE_WINDOW", self.hide_to_tray)
        config = load_config()
        if force_setup or not config.wizard_complete:
            self._show_wizard()
        else:
            self._show_main()
            self.worker.start()
            self.tray.start()
            if start_in_tray:
                self.root.after(200, self.hide_to_tray)

    def _show_wizard(self) -> None:
        self._clear_root()
        self._wizard = WizardView(self.root, on_finish=self._wizard_finished)
        self._wizard.pack(fill="both", expand=True)

    def _show_main(self) -> None:
        from desktop.main_window import MainView

        self._clear_root()
        self._main = MainView(self.root, worker=self.worker, on_quit=self.quit_app)
        self._main.pack(fill="both", expand=True)

    def _clear_root(self) -> None:
        for child in self.root.winfo_children():
            child.destroy()
        self._main = None
        self._wizard = None

    def _wizard_finished(self, _config: AppConfig) -> None:
        self._show_main()
        self.worker.start()
        if self.tray._icon is None:
            self.tray.start()

    def _ui_log(self, line: str) -> None:
        self.root.after(0, lambda: self._main.append_log(line) if self._main else None)

    def _ui_status(self, status: JobStatus, detail: str) -> None:
        def apply() -> None:
            if self._main:
                self._main.set_status(status, detail)
            self.tray.set_status(status, paused=self.worker.is_paused())

        self.root.after(0, apply)

    def _ui_job_done(self, result: JobResult) -> None:
        def apply() -> None:
            if self._main:
                self._main.refresh_job()
                self._main.show_title_pick(result)
            self.show()

        self.root.after(0, apply)

    def _tray_pause(self) -> None:
        self.worker.set_paused(not self.worker.is_paused())
        self.tray.set_status(self.worker.status, paused=self.worker.is_paused())

    def hide_to_tray(self) -> None:
        if self._quitting:
            self.root.destroy()
            return
        if self.tray._icon is None:
            self.tray.start()
        if self.tray._icon is None:
            return
        self.root.withdraw()

    def show(self) -> None:
        self.root.after(0, self._deiconify)

    def _deiconify(self) -> None:
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def quit_app(self) -> None:
        self._quitting = True
        self.worker.stop()
        self.tray.stop()
        self.root.after(0, self.root.destroy)

    def run(self) -> int:
        self.root.mainloop()
        return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    app = DesktopApp(start_in_tray=bool(args.tray), force_setup=bool(args.setup))
    return app.run()


if __name__ == "__main__":
    raise SystemExit(main())
