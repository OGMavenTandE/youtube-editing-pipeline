"""Main status window: watcher controls, short log, optional title pick."""

from __future__ import annotations

import webbrowser
from collections import deque
from typing import Callable

import customtkinter as ctk

from desktop.config_store import AppConfig, load_config
from desktop.wizard import open_wizard_window
from desktop.worker import JobResult, JobStatus, PipelineWorker

MAX_LOG_LINES = 50


class MainView(ctk.CTkFrame):
    def __init__(
        self,
        master: ctk.CTk,
        *,
        worker: PipelineWorker,
        on_quit: Callable[[], None],
    ) -> None:
        super().__init__(master, fg_color="transparent")
        self.worker = worker
        self._on_quit = on_quit
        self._log_lines: deque[str] = deque(maxlen=MAX_LOG_LINES)
        self._pending_result: JobResult | None = None
        self._build()
        self.refresh_job()

    def _build(self) -> None:
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(3, weight=1)

        header = ctk.CTkLabel(self, text="YouTube Pipeline", font=ctk.CTkFont(size=22, weight="bold"))
        header.grid(row=0, column=0, sticky="w", padx=24, pady=(20, 4))

        self.status_var = ctk.StringVar(value="Idle")
        ctk.CTkLabel(self, textvariable=self.status_var, font=ctk.CTkFont(size=16)).grid(
            row=1, column=0, sticky="w", padx=24, pady=(0, 8)
        )

        self.job_var = ctk.StringVar(value="No jobs yet.")
        job_row = ctk.CTkFrame(self, fg_color="transparent")
        job_row.grid(row=2, column=0, sticky="ew", padx=24, pady=(0, 8))
        ctk.CTkLabel(job_row, textvariable=self.job_var, wraplength=480, justify="left").pack(
            side="left", fill="x", expand=True
        )
        ctk.CTkButton(job_row, text="Open outbox", width=120, command=self._open_outbox).pack(side="right")

        self.log_box = ctk.CTkTextbox(self, height=220, wrap="word")
        self.log_box.grid(row=3, column=0, sticky="nsew", padx=24, pady=8)
        self.log_box.configure(state="disabled")

        self.title_frame = ctk.CTkFrame(self)
        self.title_frame.grid(row=4, column=0, sticky="ew", padx=24, pady=8)
        self.title_frame.grid_remove()
        ctk.CTkLabel(self.title_frame, text="Pick a title (optional), then re-upload.").pack(
            anchor="w", padx=12, pady=(10, 4)
        )
        self._title_choice = ctk.IntVar(value=0)
        self._title_buttons: list[ctk.CTkRadioButton] = []
        self._title_host = ctk.CTkFrame(self.title_frame, fg_color="transparent")
        self._title_host.pack(fill="x", padx=12, pady=4)
        title_actions = ctk.CTkFrame(self.title_frame, fg_color="transparent")
        title_actions.pack(fill="x", padx=12, pady=(4, 10))
        ctk.CTkButton(title_actions, text="Apply title and re-upload", command=self._apply_title).pack(
            side="left", padx=(0, 8)
        )
        ctk.CTkButton(title_actions, text="Skip", command=self._hide_titles).pack(side="left")

        actions = ctk.CTkFrame(self, fg_color="transparent")
        actions.grid(row=5, column=0, sticky="ew", padx=24, pady=(4, 20))
        self.pause_btn = ctk.CTkButton(actions, text="Pause watching", command=self._toggle_pause)
        self.pause_btn.pack(side="left", padx=(0, 8))
        ctk.CTkButton(actions, text="Run once now", command=self._run_once).pack(side="left", padx=(0, 8))
        ctk.CTkButton(actions, text="Settings", command=self._open_settings).pack(side="left", padx=(0, 8))
        ctk.CTkButton(actions, text="Quit", fg_color="#8a2b2b", command=self._on_quit).pack(side="right")

    def set_status(self, status: JobStatus, detail: str = "") -> None:
        suffix = f" — {detail}" if detail else ""
        self.status_var.set(f"{status.value}{suffix}")
        self.pause_btn.configure(text="Resume watching" if self.worker.is_paused() else "Pause watching")

    def append_log(self, line: str) -> None:
        self._log_lines.append(line)
        self.log_box.configure(state="normal")
        self.log_box.delete("1.0", "end")
        self.log_box.insert("1.0", "\n".join(self._log_lines) + "\n")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def refresh_job(self) -> None:
        config = load_config()
        if not config.last_job_name:
            self.job_var.set("No jobs yet.")
            return
        when = config.last_job_finished_at or ""
        self.job_var.set(f"Last job: {config.last_job_name}  {when}")

    def show_title_pick(self, result: JobResult) -> None:
        if result.error or result.skipped or not result.titles:
            return
        self._pending_result = result
        for button in self._title_buttons:
            button.destroy()
        self._title_buttons = []
        self._title_choice.set(0)
        for index, title in enumerate(result.titles[:5]):
            button = ctk.CTkRadioButton(
                self._title_host,
                text=f"{index + 1}. {title}",
                variable=self._title_choice,
                value=index,
            )
            button.pack(anchor="w", pady=2)
            self._title_buttons.append(button)
        self.title_frame.grid()

    def _hide_titles(self) -> None:
        self.title_frame.grid_remove()
        self._pending_result = None

    def _apply_title(self) -> None:
        result = self._pending_result
        if result is None:
            return
        index = int(self._title_choice.get())

        def work() -> None:
            try:
                self.worker.repack_and_reupload(result.stem, index)
            except Exception as exc:
                self.after(0, lambda: self.append_log(f"Title update failed: {exc}"))
                return
            self.after(0, self._hide_titles)
            self.after(0, self.refresh_job)

        import threading

        threading.Thread(target=work, name="repack-title", daemon=True).start()
        self.append_log(f"Applying title {index + 1}…")

    def _open_outbox(self) -> None:
        config = load_config()
        if config.last_job_outbox_url:
            webbrowser.open(config.last_job_outbox_url)
        else:
            self.append_log("No outbox URL yet.")

    def _toggle_pause(self) -> None:
        self.worker.set_paused(not self.worker.is_paused())
        self.pause_btn.configure(text="Resume watching" if self.worker.is_paused() else "Pause watching")

    def _run_once(self) -> None:
        self.append_log("Run once requested.")
        self.worker.run_once()

    def _open_settings(self) -> None:
        chooser = ctk.CTkToplevel(self.winfo_toplevel())
        chooser.title("Settings")
        chooser.geometry("420x280")
        ctk.CTkLabel(chooser, text="What do you want to change?", font=ctk.CTkFont(size=16, weight="bold")).pack(
            anchor="w", padx=20, pady=(20, 12)
        )
        root = self.winfo_toplevel()

        def open_step(step: int) -> None:
            chooser.destroy()
            open_wizard_window(root, start_step=step, on_finish=self._settings_saved)

        ctk.CTkButton(chooser, text="Gemini key", command=lambda: open_step(2)).pack(fill="x", padx=20, pady=4)
        ctk.CTkButton(chooser, text="Google Drive sign-in", command=lambda: open_step(3)).pack(
            fill="x", padx=20, pady=4
        )
        ctk.CTkButton(chooser, text="Folders", command=lambda: open_step(4)).pack(fill="x", padx=20, pady=4)
        ctk.CTkButton(chooser, text="Start with Windows", command=lambda: open_step(5)).pack(
            fill="x", padx=20, pady=4
        )
        ctk.CTkButton(chooser, text="Run full setup wizard", command=lambda: open_step(0)).pack(
            fill="x", padx=20, pady=4
        )

    def _settings_saved(self, _config: AppConfig) -> None:
        self.append_log("Settings saved.")
        self.refresh_job()
