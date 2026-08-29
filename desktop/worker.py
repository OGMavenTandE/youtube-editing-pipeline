"""Watch Drive inbox, run the existing pipeline, upload the Studio folder."""

from __future__ import annotations

import contextlib
import json
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable

import ffmpeg

from desktop.config_store import AppConfig, load_config, save_config
from desktop.logutil import LogWriter, sanitize_log_line
from desktop.oauth import build_drive_service, load_saved_credentials
from desktop.paths import env_path, install_root, processed_ids_path
from pipeline.config import Settings, load_settings, require_ffprobe
from pipeline.drive_io import (
    DriveClient,
    ProcessedIdStore,
    drive_folder_url,
    file_stem,
    is_landscape_dimensions,
)
from pipeline.studio import parse_titles_file

LogFn = Callable[[str], None]
StatusFn = Callable[["JobStatus", str], None]
DoneFn = Callable[["JobResult"], None]


class JobStatus(str, Enum):
    IDLE = "Idle"
    WATCHING = "Watching"
    DOWNLOADING = "Downloading"
    PROCESSING = "Processing"
    UPLOADING = "Uploading"
    ERROR = "Error"


@dataclass
class JobResult:
    file_id: str
    name: str
    stem: str
    finished_at: str
    outbox_url: str
    titles: list[str] = field(default_factory=list)
    studio_dir: str = ""
    skipped: bool = False
    error: str = ""


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def pipeline_settings() -> Settings:
    return load_settings(env_path())


def titles_for_stem(stem: str, settings: Settings) -> list[str]:
    meta_path = settings.output_dir / f"{stem}_youtube_metadata.json"
    if meta_path.is_file():
        payload = json.loads(meta_path.read_text(encoding="utf-8"))
        titles = [str(item).strip() for item in payload.get("titles", []) if str(item).strip()]
        if titles:
            return titles[:5]
    studio = settings.output_dir / f"{stem}_studio" / "titles.txt"
    if studio.is_file():
        titles, _selected = parse_titles_file(studio.read_text(encoding="utf-8"))
        return titles[:5]
    return []


def invoke_run_py(argv: list[str], log: LogFn) -> int:
    """Call run.main from this thread. UI must not sit on this call."""
    from run import main

    writer = LogWriter(log)
    with contextlib.redirect_stdout(writer), contextlib.redirect_stderr(writer):
        code = main(argv)
    writer.flush()
    return int(code)


def probe_landscape(path: Path, settings: Settings) -> bool:
    ffprobe = require_ffprobe(settings)
    info = ffmpeg.probe(str(path), cmd=ffprobe)
    for stream in info.get("streams", []):
        if stream.get("codec_type") != "video":
            continue
        width = int(stream.get("width") or 0)
        height = int(stream.get("height") or 0)
        return is_landscape_dimensions(width, height)
    return False


class PipelineWorker:
    def __init__(
        self,
        *,
        log: LogFn,
        status: StatusFn,
        on_job_done: DoneFn | None = None,
    ) -> None:
        self._log = log
        self._status = status
        self._on_job_done = on_job_done
        self._stop = threading.Event()
        self._pause = threading.Event()
        self._run_now = threading.Event()
        self._thread: threading.Thread | None = None
        self._busy = threading.Lock()
        self.status = JobStatus.IDLE
        self.last_detail = ""

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="pipeline-worker", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._run_now.set()

    def set_paused(self, paused: bool) -> None:
        if paused:
            self._pause.set()
            self._set_status(JobStatus.IDLE, "Watching paused")
        else:
            self._pause.clear()
            self._set_status(JobStatus.WATCHING, "Watching inbox")
            self._run_now.set()

    def is_paused(self) -> bool:
        return self._pause.is_set()

    def run_once(self) -> None:
        self._run_now.set()

    def log(self, line: str) -> None:
        cleaned = sanitize_log_line(line)
        if cleaned:
            self._log(cleaned)

    def _set_status(self, status: JobStatus, detail: str = "") -> None:
        self.status = status
        self.last_detail = detail
        self._status(status, detail)

    def _loop(self) -> None:
        self._set_status(JobStatus.WATCHING, "Watching inbox")
        while not self._stop.is_set():
            if self._pause.is_set() and not self._run_now.is_set():
                self._stop.wait(0.4)
                continue
            forced = self._run_now.is_set()
            self._run_now.clear()
            if not self._pause.is_set() or forced:
                try:
                    self.poll_once(log_empty=forced)
                    self._set_status(JobStatus.ERROR, "Watch failed")
                    self.log(f"Watch error: {exc}")
            if self._stop.is_set():
                break
            if self._pause.is_set():
                self._set_status(JobStatus.IDLE, "Watching paused")
            else:
                self._set_status(JobStatus.WATCHING, "Watching inbox")
            config = load_config()
            self._run_now.wait(max(15, int(config.poll_seconds)))
            self._run_now.clear()

    def poll_once(self, *, log_empty: bool = False, rerun: bool = False) -> None:
        if not self._busy.acquire(blocking=False):
            self.log("A job is already running.")
            return
        try:
            self._poll_once(log_empty=log_empty, rerun=rerun)
        finally:
            self._busy.release()

    def _drive_client(self) -> DriveClient:
        creds = load_saved_credentials()
        if creds is None:
            raise RuntimeError("Google Drive is not signed in.")
        return DriveClient(build_drive_service(creds))

    def _poll_once(self, *, log_empty: bool = False, rerun: bool = False) -> None:
        config = load_config()
        if not config.folders_ready():
            self.log("Drive folders are not set. Open Settings.")
            return
        client = self._drive_client()
        store = ProcessedIdStore(processed_ids_path())
        videos = client.list_inbox_videos(config.inbox_folder_id)
        if not videos:
            if log_empty:
                self.log("Inbox is empty.")
            return
        for item in videos:
            if self._stop.is_set():
                return
            self._handle_inbox_item(client, store, config, item, rerun=rerun)

    def _handle_inbox_item(
        self,
        client: DriveClient,
        store: ProcessedIdStore,
        config: AppConfig,
        item: Any,
        *,
        rerun: bool,
    ) -> None:
        stem = file_stem(item.name)
        if store.is_done(item.id) and not rerun:
            self.log(f"Already processed {item.name}.")
            return
        if client.outbox_has_final_mp4(config.outbox_folder_id, stem, item.id) and not rerun:
            self.log(f"Outbox already has a finished MP4 for {item.name}. Skipping.")
            store.mark_done(
                item.id,
                name=item.name,
                stem=stem,
            )
            try:
                client.move_file(item.id, config.done_folder_id, config.inbox_folder_id)
            except Exception as exc:
                self.log(f"Could not move {item.name} to done: {exc}")
            return
        self._process(client, store, config, item, stem)

    def _process(
        self,
        client: DriveClient,
        store: ProcessedIdStore,
        config: AppConfig,
        item: Any,
        stem: str,
    ) -> None:
        if not store.claim(item.id, name=item.name, stem=stem):
            self.log(f"{item.name} is already claimed.")
            return
        try:
            if not client.claim_file(item.id):
                self.log(f"{item.name} is claimed on Drive. Skipping.")
                return
        except Exception as exc:
            self.log(f"Drive claim failed for {item.name}: {exc}")

        settings = pipeline_settings()
        settings.ensure_dirs()
        dest = settings.input_dir / item.name
        try:
            self._set_status(JobStatus.DOWNLOADING, item.name)
            self.log(f"Downloading {item.name}…")
            last_bucket = {"n": -1}

            def _progress(pct: float) -> None:
                bucket = int(pct * 4)
                if bucket != last_bucket["n"] and bucket > 0:
                    last_bucket["n"] = bucket
                    self.log(f"Download {min(100, bucket * 25)}%")

            client.download_resumable(item.id, dest, progress=_progress)
            if not probe_landscape(dest, settings):
                self.log(f"{item.name} is not landscape. Skipping.")
                store.mark_skipped(item.id, name=item.name, stem=stem)
                return

            self._set_status(JobStatus.PROCESSING, item.name)
            self.log(f"Running pipeline on {item.name}…")
            argv = ["--input", str(dest)]
            if config.broll_dir:
                argv.extend(["--broll-dir", config.broll_dir])
            code = invoke_run_py(argv, self.log)
            if code != 0:
                raise RuntimeError(f"Pipeline exited with status {code}.")

            studio_dir = settings.output_dir / f"{stem}_studio"
            if not studio_dir.is_dir():
                raise FileNotFoundError(f"Studio folder was not written: {studio_dir}")

            self._set_status(JobStatus.UPLOADING, item.name)
            self.log(f"Uploading Studio package to outbox/{stem}/…")
            folder = client.upload_studio_package(
                studio_dir,
                config.outbox_folder_id,
                stem,
                source_file_id=item.id,
                source_name=item.name,
            )
            client.move_file(item.id, config.done_folder_id, config.inbox_folder_id)
            outbox_url = drive_folder_url(folder.id)
            titles = titles_for_stem(stem, settings)
            finished = _now_iso()
            store.mark_done(
                item.id,
                name=item.name,
                stem=stem,
                outbox_folder_id=folder.id,
            )
            config.last_job_name = item.name
            config.last_job_finished_at = finished
            config.last_job_outbox_url = outbox_url
            config.last_job_stem = stem
            config.last_job_file_id = item.id
            config.last_job_titles = titles
            save_config(config)
            self.log(f"Done. Outbox: {outbox_url}")
            result = JobResult(
                file_id=item.id,
                name=item.name,
                stem=stem,
                finished_at=finished,
                outbox_url=outbox_url,
                titles=titles,
                studio_dir=str(studio_dir),
            )
            if self._on_job_done:
                self._on_job_done(result)
        except Exception as exc:
            store.mark_error(item.id, str(exc))
            self._set_status(JobStatus.ERROR, item.name)
            self.log(f"Job failed: {exc}")
            if self._on_job_done:
                self._on_job_done(
                    JobResult(
                        file_id=item.id,
                        name=item.name,
                        stem=stem,
                        finished_at=_now_iso(),
                        outbox_url="",
                        error=str(exc),
                    )
                )

    def repack_and_reupload(self, stem: str, title_index: int) -> str:
        if not self._busy.acquire(blocking=False):
            raise RuntimeError("A job is already running.")
        try:
            return self._repack_and_reupload(stem, title_index)
        finally:
            self._busy.release()

    def _repack_and_reupload(self, stem: str, title_index: int) -> str:
        config = load_config()
        settings = pipeline_settings()
        studio_dir = settings.output_dir / f"{stem}_studio"
        self._set_status(JobStatus.PROCESSING, f"Repack {stem}")
        self.log(f"Repacking studio with title {title_index + 1}…")
        code = invoke_run_py(
            ["--repack-studio", str(studio_dir), "--title-index", str(title_index)],
            self.log,
        )
        if code != 0:
            raise RuntimeError(f"Repack exited with status {code}.")
        client = self._drive_client()
        self._set_status(JobStatus.UPLOADING, stem)
        folder = client.upload_studio_package(
            studio_dir,
            config.outbox_folder_id,
            stem,
            source_file_id=config.last_job_file_id,
            source_name=config.last_job_name,
        )
        url = drive_folder_url(folder.id)
        config.last_job_outbox_url = url
        save_config(config)
        self._set_status(JobStatus.WATCHING, "Watching inbox")
        self.log(f"Re-uploaded Studio package. {url}")
        return url


def prepare_runtime_env() -> None:
    """Point the CLI settings loader at the app .env and writable media dirs."""
    import os

    root = install_root()
    os.environ.setdefault("YOUTUBE_PIPELINE_ENV", str(env_path()))
    os.environ.setdefault("INPUT_DIR", str(root / "input"))
    os.environ.setdefault("OUTPUT_DIR", str(root / "output"))
    os.environ.setdefault("WORK_DIR", str(root / "work"))
    os.environ.setdefault("SLIDES_DIR", str(root / "work" / "slides"))
    os.environ.setdefault("SCENES_DIR", str(root / "work" / "scenes"))
