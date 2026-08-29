"""Non-secret desktop settings (folder ids, last job, wizard flag)."""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, Field

from desktop.paths import config_path, user_data_dir


class AppConfig(BaseModel):
    wizard_complete: bool = False
    inbox_folder_id: str = ""
    outbox_folder_id: str = ""
    done_folder_id: str = ""
    start_with_windows: bool = False
    poll_seconds: int = Field(default=45, ge=15, le=600)
    broll_dir: str = ""
    client_secret_path: str = ""
    last_job_name: str = ""
    last_job_finished_at: str = ""
    last_job_outbox_url: str = ""
    last_job_stem: str = ""
    last_job_file_id: str = ""
    last_job_titles: list[str] = Field(default_factory=list)

    def folders_ready(self) -> bool:
        return bool(self.inbox_folder_id and self.outbox_folder_id and self.done_folder_id)


def load_config(path: Path | None = None) -> AppConfig:
    target = Path(path) if path is not None else config_path()
    if not target.is_file():
        return AppConfig()
    payload = json.loads(target.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        return AppConfig()
    return AppConfig.model_validate(payload)


def save_config(config: AppConfig, path: Path | None = None) -> Path:
    target = Path(path) if path is not None else config_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(config.model_dump_json(indent=2) + "\n", encoding="utf-8")
    tmp.replace(target)
    return target


def ensure_user_data() -> Path:
    path = user_data_dir()
    path.mkdir(parents=True, exist_ok=True)
    return path
