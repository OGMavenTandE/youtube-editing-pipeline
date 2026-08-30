"""First-run wizard. One question per screen, Next/Back."""

from __future__ import annotations

from typing import Callable

import customtkinter as ctk

from .config_store import AppConfig, load_config, save_config
from .envfile import read_env_value, upsert_env_value
from .ffmpeg_check import (
    check_ffmpeg,
    check_playwright,
    copyable_ffmpeg_help,
    install_playwright_chromium,
)
from .oauth import (
    OAuthConfigError,
    load_client_id_secret,
    load_saved_credentials,
    run_installed_app_flow,
    signed_in,
)
from .paths import env_path, first_existing_client_secret
from .startup import set_startup, startup_enabled
from pipeline.drive_io import (
    DriveClient,
    PIPELINE_ROOT_NAME,
    drive_folder_url,
    pipeline_folder_relpath,
)

FinishFn = Callable[[AppConfig], None]

WELCOME_BODY = (
    "This app watches a Google Drive inbox for landscape talking-head MP4s, "
    "runs the local editing pipeline on this PC, and puts a YouTube Studio "
    "package in your Drive outbox.\n\n"
    "You record on your phone, drop the file in the inbox, and later grab "
    "the finished folder from outbox. Forge chat is not the transfer path."
)

OAUTH_HELP = (
    "Create a Google Cloud desktop OAuth client with the Drive scope "
    "(https://www.googleapis.com/auth/drive). Do not create a YouTube client.\n\n"
    "Google Cloud console → APIs & Services → Credentials → Create credentials "
    "→ OAuth client ID → Desktop app. Enable the Google Drive API. Then paste "
    "the client id and secret here, or choose the downloaded client_secret.json."
)


class WizardView(ctk.CTkFrame):
    STEPS = (
        "Welcome",
        "FFmpeg",
        "Gemini key",
        "Google Drive",
        "Folders",
        "Startup",
    )

    def __init__(
        self,
        master: ctk.CTk | ctk.CTkToplevel,
        *,
        on_finish: FinishFn,
        start_step: int = 0,
        settings_mode: bool = False,
    ) -> None:
        super().__init__(master, fg_color="transparent")
        self._on_finish = on_finish
        self._settings_mode = settings_mode
        self.step = max(0, min(start_step, len(self.STEPS) - 1))
        self.config_data = load_config()
        self._folder_choices: list[tuple[str, str]] = []
        self._build()
        self._show_step()

    def _build(self) -> None:
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)

        self.header = ctk.CTkLabel(self, text="", font=ctk.CTkFont(size=22, weight="bold"))
        self.header.grid(row=0, column=0, sticky="w", padx=28, pady=(24, 8))

        self.body = ctk.CTkFrame(self, fg_color="transparent")
        self.body.grid(row=1, column=0, sticky="nsew", padx=28, pady=8)
        self.body.grid_columnconfigure(0, weight=1)

        nav = ctk.CTkFrame(self, fg_color="transparent")
        nav.grid(row=2, column=0, sticky="ew", padx=28, pady=(8, 24))
        nav.grid_columnconfigure(1, weight=1)
        self.back_btn = ctk.CTkButton(nav, text="Back", width=100, command=self._back)
        self.back_btn.grid(row=0, column=0, sticky="w")
        self.next_btn = ctk.CTkButton(nav, text="Next", width=120, command=self._next)
        self.next_btn.grid(row=0, column=2, sticky="e")

        self._gemini = ctk.StringVar(value=read_env_value(env_path(), "GEMINI_API_KEY"))
        self._client_id = ctk.StringVar(value=read_env_value(env_path(), "GOOGLE_OAUTH_CLIENT_ID"))
        self._client_secret = ctk.StringVar(
            value=read_env_value(env_path(), "GOOGLE_OAUTH_CLIENT_SECRET")
        )
        self._secret_path = ctk.StringVar(value=self.config_data.client_secret_path)
        self._inbox = ctk.StringVar(value=self.config_data.inbox_folder_id)
        self._outbox = ctk.StringVar(value=self.config_data.outbox_folder_id)
        self._done = ctk.StringVar(value=self.config_data.done_folder_id)
        self._startup = ctk.BooleanVar(value=self.config_data.start_with_windows or startup_enabled())
        self._status = ctk.StringVar(value="")

    def _clear_body(self) -> None:
        for child in self.body.winfo_children():
            child.destroy()

    def _show_step(self) -> None:
        self._clear_body()
        self.header.configure(text=self.STEPS[self.step])
        self.back_btn.configure(state="normal" if self.step > 0 and not self._settings_mode else "disabled")
        last = self.step == len(self.STEPS) - 1
        self.next_btn.configure(text="Finish" if last or self._settings_mode else "Next")
        self._status.set("")
        {
            0: self._screen_welcome,
            1: self._screen_ffmpeg,
            2: self._screen_gemini,
            3: self._screen_drive,
            4: self._screen_folders,
            5: self._screen_startup,
        }[self.step]()

    def _label(self, text: str, **kwargs) -> ctk.CTkLabel:
        widget = ctk.CTkLabel(self.body, text=text, justify="left", wraplength=620, **kwargs)
        widget.grid(sticky="w", pady=(0, 10))
        return widget

    def _status_label(self) -> None:
        ctk.CTkLabel(self.body, textvariable=self._status, wraplength=620, justify="left").grid(
            sticky="w", pady=(12, 0)
        )

    def _screen_welcome(self) -> None:
        self._label(WELCOME_BODY, font=ctk.CTkFont(size=15))

    def _screen_ffmpeg(self) -> None:
        self._label("FFmpeg must be on PATH. Playwright Chromium is used for slides and thumbnails.")
        self._ffmpeg_state = ctk.CTkLabel(self.body, text="", justify="left")
        self._ffmpeg_state.grid(sticky="w", pady=(0, 8))
        help_box = ctk.CTkTextbox(self.body, height=90, wrap="word")
        help_box.grid(sticky="ew", pady=(0, 8))
        help_box.insert("1.0", copyable_ffmpeg_help())
        help_box.configure(state="disabled")

        def copy_help() -> None:
            self.clipboard_clear()
            self.clipboard_append(copyable_ffmpeg_help())
            self._status.set("Install commands copied.")

        row = ctk.CTkFrame(self.body, fg_color="transparent")
        row.grid(sticky="w", pady=8)
        ctk.CTkButton(row, text="Recheck", width=110, command=self._recheck_tools).pack(side="left", padx=(0, 8))
        ctk.CTkButton(row, text="Copy install commands", width=180, command=copy_help).pack(side="left", padx=(0, 8))
        ctk.CTkButton(row, text="Install Chromium", width=150, command=self._install_chromium).pack(side="left")
        self._status_label()
        self._recheck_tools()

    def _recheck_tools(self) -> None:
        ffmpeg = check_ffmpeg()
        play = check_playwright()
        ffmpeg_line = f"FFmpeg: found at {ffmpeg.path}" if ffmpeg.found else "FFmpeg: not found"
        play_line = "Playwright Chromium: ready" if play.found else "Playwright Chromium: not installed"
        if hasattr(self, "_ffmpeg_state"):
            self._ffmpeg_state.configure(text=f"{ffmpeg_line}\n{play_line}")
        self._status.set("Checked." if ffmpeg.found else "Install FFmpeg, then click Recheck.")

    def _install_chromium(self) -> None:
        self._status.set("Installing Chromium…")
        self.update_idletasks()
        ok, message = install_playwright_chromium()
        self._status.set(message)
        self._recheck_tools()
        if ok:
            self._status.set(message)

    def _screen_gemini(self) -> None:
        self._label(
            "Paste your Google AI Studio key. It is saved in the user data "
            "folder (not next to the EXE), so unzipping a new build keeps it. "
            "It is never logged."
        )
        entry = ctk.CTkEntry(self.body, textvariable=self._gemini, show="*", width=420)
        entry.grid(sticky="w", pady=8)
        self._status_label()

    def _screen_drive(self) -> None:
        self._label(OAUTH_HELP)
        ctk.CTkLabel(self.body, text="Client ID").grid(sticky="w")
        ctk.CTkEntry(self.body, textvariable=self._client_id, width=480).grid(sticky="w", pady=(0, 8))
        ctk.CTkLabel(self.body, text="Client secret").grid(sticky="w")
        ctk.CTkEntry(self.body, textvariable=self._client_secret, show="*", width=480).grid(
            sticky="w", pady=(0, 8)
        )
        ctk.CTkLabel(self.body, text="Or client_secret.json").grid(sticky="w")
        path_row = ctk.CTkFrame(self.body, fg_color="transparent")
        path_row.grid(sticky="ew", pady=(0, 8))
        ctk.CTkEntry(path_row, textvariable=self._secret_path, width=360).pack(side="left", padx=(0, 8))
        ctk.CTkButton(path_row, text="Browse", width=90, command=self._browse_secret).pack(side="left")
        sign_row = ctk.CTkFrame(self.body, fg_color="transparent")
        sign_row.grid(sticky="w", pady=8)
        ctk.CTkButton(sign_row, text="Sign in with Google", command=self._sign_in).pack(side="left")
        self._status_label()
        self._status.set("Signed in." if signed_in() else "Not signed in yet.")

    def _browse_secret(self) -> None:
        from tkinter import filedialog

        chosen = filedialog.askopenfilename(
            title="Choose client_secret.json",
            filetypes=[("JSON", "*.json"), ("All files", "*.*")],
        )
        if chosen:
            self._secret_path.set(chosen)

    def _sign_in(self) -> None:
        try:
            self._persist_oauth_client()
            client_id, client_secret = load_client_id_secret(json_path=self._secret_path.get() or None)
            run_installed_app_flow(client_id, client_secret)
        except OAuthConfigError as exc:
            self._status.set(str(exc))
            return
        except Exception:
            self._status.set("Sign-in did not finish. Check the desktop OAuth client and try again.")
            return
        self._status.set("Signed in. Drive token saved for this Windows user.")

    def _screen_folders(self) -> None:
        self._label(
            f"Pick or create {pipeline_folder_relpath('inbox')}, "
            f"{pipeline_folder_relpath('outbox')}, and {pipeline_folder_relpath('done')}."
        )
        ctk.CTkButton(self.body, text=f"Create {PIPELINE_ROOT_NAME} folders", command=self._create_defaults).grid(
            sticky="w", pady=(0, 10)
        )
        self._folder_menu_row("Inbox folder ID", self._inbox)
        self._folder_menu_row("Outbox folder ID", self._outbox)
        self._folder_menu_row("Done folder ID", self._done)
        ctk.CTkButton(self.body, text="Refresh Drive folders", command=self._refresh_folders).grid(
            sticky="w", pady=8
        )
        self._folder_box = ctk.CTkTextbox(self.body, height=140, wrap="word")
        self._folder_box.grid(sticky="ew")
        self._status_label()
        self._refresh_folders()

    def _folder_menu_row(self, label: str, variable: ctk.StringVar) -> None:
        ctk.CTkLabel(self.body, text=label).grid(sticky="w")
        ctk.CTkEntry(self.body, textvariable=variable, width=420).grid(sticky="w", pady=(0, 8))

    def _drive_client_or_status(self) -> DriveClient | None:
        creds = load_saved_credentials()
        if creds is None:
            self._status.set("Sign in on the previous screen first.")
            return None
        from .oauth import build_drive_service

        return DriveClient(build_drive_service(creds))

    def _create_defaults(self) -> None:
        client = self._drive_client_or_status()
        if client is None:
            return
        try:
            folders = client.ensure_pipeline_folders()
        except Exception:
            self._status.set("Could not create the Drive folders. Check sign-in and try again.")
            return
        self._inbox.set(folders.inbox.id)
        self._outbox.set(folders.outbox.id)
        self._done.set(folders.done.id)
        self._status.set(
            f"Created {PIPELINE_ROOT_NAME}. "
            f"Inbox {drive_folder_url(folders.inbox.id)}"
        )
        self._refresh_folders()

    def _refresh_folders(self) -> None:
        client = self._drive_client_or_status()
        if client is None:
            return
        try:
            folders = client.list_folders("root")
        except Exception:
            self._status.set("Could not list Drive folders.")
            return
        lines = [f"{item.name}  ({item.id})" for item in folders]
        if hasattr(self, "_folder_box"):
            self._folder_box.delete("1.0", "end")
            self._folder_box.insert("1.0", "\n".join(lines) or "(no folders in My Drive)")

    def _screen_startup(self) -> None:
        self._label("Start with Windows so the inbox is watched when this PC boots.")
        ctk.CTkCheckBox(
            self.body,
            text="Start YouTube Pipeline when Windows starts",
            variable=self._startup,
        ).grid(sticky="w", pady=12)
        self._status_label()

    def _persist_oauth_client(self) -> None:
        env = env_path()
        if self._client_id.get().strip():
            upsert_env_value(env, "GOOGLE_OAUTH_CLIENT_ID", self._client_id.get().strip())
        if self._client_secret.get().strip():
            upsert_env_value(env, "GOOGLE_OAUTH_CLIENT_SECRET", self._client_secret.get().strip())
        if self._secret_path.get().strip():
            self.config_data.client_secret_path = self._secret_path.get().strip()

    def _validate(self) -> bool:
        if self.step == 1:
            if not check_ffmpeg().found:
                self._status.set("FFmpeg is still missing. Install it, then Recheck.")
                return False
            return True
        if self.step == 2:
            key = self._gemini.get().strip()
            if not key:
                self._status.set("Paste a Gemini API key to continue.")
                return False
            upsert_env_value(env_path(), "GEMINI_API_KEY", key)
            return True
        if self.step == 3:
            self._persist_oauth_client()
            if not signed_in():
                self._status.set("Sign in with Google before continuing.")
                return False
            return True
        if self.step == 4:
            if not (self._inbox.get().strip() and self._outbox.get().strip() and self._done.get().strip()):
                self._status.set("Inbox, outbox, and done folder IDs are required.")
                return False
            self.config_data.inbox_folder_id = self._inbox.get().strip()
            self.config_data.outbox_folder_id = self._outbox.get().strip()
            self.config_data.done_folder_id = self._done.get().strip()
            return True
        if self.step == 5:
            enabled = bool(self._startup.get())
            set_startup(enabled)
            self.config_data.start_with_windows = enabled
            return True
        return True

    def _back(self) -> None:
        if self.step > 0:
            self.step -= 1
            self._show_step()

    def _next(self) -> None:
        if not self._validate():
            return
        if self._settings_mode or self.step == len(self.STEPS) - 1:
            self._finish()
            return
        self.step += 1
        self._show_step()

    def _finish(self) -> None:
        if self._gemini.get().strip():
            upsert_env_value(env_path(), "GEMINI_API_KEY", self._gemini.get().strip())
        self._persist_oauth_client()
        self.config_data.inbox_folder_id = self._inbox.get().strip() or self.config_data.inbox_folder_id
        self.config_data.outbox_folder_id = self._outbox.get().strip() or self.config_data.outbox_folder_id
        self.config_data.done_folder_id = self._done.get().strip() or self.config_data.done_folder_id
        self.config_data.start_with_windows = bool(self._startup.get())
        if self.config_data.folders_ready() and signed_in():
            self.config_data.wizard_complete = True
        save_config(self.config_data)
        self._on_finish(self.config_data)


def open_wizard_window(
    master: ctk.CTk,
    *,
    start_step: int,
    on_finish: FinishFn,
    settings_mode: bool = True,
) -> ctk.CTkToplevel:
    window = ctk.CTkToplevel(master)
    window.title("Settings" if settings_mode else "Setup")
    window.geometry("720x560")

    def _done(config: AppConfig) -> None:
        window.destroy()
        on_finish(config)

    view = WizardView(
        window,
        on_finish=_done,
        start_step=start_step,
        settings_mode=settings_mode,
    )
    view.pack(fill="both", expand=True)
    window.transient(master)
    window.grab_set()
    return window
