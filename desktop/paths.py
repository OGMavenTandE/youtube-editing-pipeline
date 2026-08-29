"""Install, .env, and per-user data paths for the desktop app."""

from __future__ import annotations

import os
import sys
from pathlib import Path

APP_DIR_NAME = "YouTubePipeline"
CLIENT_SECRET_NAME = "client_secret.json"


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def install_root() -> Path:
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return repo_root()


def env_path() -> Path:
    override = os.getenv("YOUTUBE_PIPELINE_ENV", "").strip()
    if override:
        return Path(override)
    return install_root() / ".env"


def client_secret_candidates(extra: str | Path | None = None) -> list[Path]:
    found: list[Path] = []
    if extra:
        found.append(Path(extra).expanduser())
    env_file = os.getenv("GOOGLE_OAUTH_CLIENT_SECRET_FILE", "").strip()
    if env_file:
        found.append(Path(env_file).expanduser())
    found.append(install_root() / "desktop" / CLIENT_SECRET_NAME)
    found.append(install_root() / CLIENT_SECRET_NAME)
    found.append(repo_root() / "desktop" / CLIENT_SECRET_NAME)
    unique: list[Path] = []
    seen: set[Path] = set()
    for path in found:
        try:
            resolved = path.resolve()
        except OSError:
            resolved = path
        if resolved in seen:
            continue
        seen.add(resolved)
        unique.append(path)
    return unique


def first_existing_client_secret(extra: str | Path | None = None) -> Path | None:
    for path in client_secret_candidates(extra):
        if path.is_file():
            return path
    return None


def user_data_dir() -> Path:
    if sys.platform == "win32":
        base = os.getenv("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / APP_DIR_NAME
    xdg = os.getenv("XDG_CONFIG_HOME")
    if xdg:
        return Path(xdg) / APP_DIR_NAME
    return Path.home() / ".config" / APP_DIR_NAME


def config_path() -> Path:
    return user_data_dir() / "app_config.json"


def processed_ids_path() -> Path:
    return user_data_dir() / "processed_ids.json"


def token_path() -> Path:
    suffix = ".dpapi" if sys.platform == "win32" else ".json"
    return user_data_dir() / f"drive_token{suffix}"


def launch_command() -> list[str]:
    if is_frozen():
        return [str(Path(sys.executable).resolve()), "--tray"]
    return [sys.executable, str(repo_root() / "desktop" / "app.py"), "--tray"]
