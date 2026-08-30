"""Installed-app Google Drive OAuth. Desktop client only. No YouTube scopes."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .envfile import read_env_value
from .paths import env_path, first_existing_client_secret, token_path
from .secrets import read_secret_file, write_secret_file
from pipeline.drive_io import DRIVE_SCOPE

DRIVE_SCOPES = [DRIVE_SCOPE]


class OAuthConfigError(RuntimeError):
    """Missing or invalid desktop OAuth client."""


def parse_client_secret_json(path: Path) -> tuple[str, str]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise OAuthConfigError("The client secret file is not a JSON object.")
    if "web" in payload and "installed" not in payload:
        raise OAuthConfigError(
            "That file is a web OAuth client. Create a Desktop app client instead."
        )
    block = payload.get("installed") or payload
    if not isinstance(block, dict):
        raise OAuthConfigError("The client secret file is missing the installed client.")
    client_id = str(block.get("client_id") or "").strip()
    client_secret = str(block.get("client_secret") or "").strip()
    if not client_id or not client_secret:
        raise OAuthConfigError("The client secret file is missing client_id or client_secret.")
    return client_id, client_secret


def load_client_id_secret(
    *,
    env_file: Path | None = None,
    json_path: str | Path | None = None,
) -> tuple[str, str]:
    env_file = Path(env_file) if env_file is not None else env_path()
    client_id = os.getenv("GOOGLE_OAUTH_CLIENT_ID", "").strip() or read_env_value(
        env_file, "GOOGLE_OAUTH_CLIENT_ID"
    )
    client_secret = os.getenv("GOOGLE_OAUTH_CLIENT_SECRET", "").strip() or read_env_value(
        env_file, "GOOGLE_OAUTH_CLIENT_SECRET"
    )
    if client_id and client_secret:
        return client_id, client_secret
    secret_file = first_existing_client_secret(json_path)
    if secret_file is None:
        raise OAuthConfigError(
            "No desktop OAuth client found. Paste the client id and secret, "
            "or choose a client_secret.json from Google Cloud."
        )
    return parse_client_secret_json(secret_file)


def client_config(client_id: str, client_secret: str) -> dict[str, Any]:
    return {
        "installed": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["http://localhost"],
        }
    }


def save_credentials_json(payload: str, path: Path | None = None) -> Path:
    target = Path(path) if path is not None else token_path()
    write_secret_file(target, payload)
    return target


def load_saved_credentials(path: Path | None = None) -> Any | None:
    target = Path(path) if path is not None else token_path()
    raw = read_secret_file(target)
    if not raw:
        return None
    from google.oauth2.credentials import Credentials

    info = json.loads(raw)
    creds = Credentials.from_authorized_user_info(info, scopes=DRIVE_SCOPES)
    return _refresh_if_needed(creds, target)


def _refresh_if_needed(creds: Any, path: Path) -> Any:
    if creds.valid:
        return creds
    if creds.expired and creds.refresh_token:
        from google.auth.transport.requests import Request

        creds.refresh(Request())
        save_credentials_json(creds.to_json(), path)
        return creds
    return None


def run_installed_app_flow(
    client_id: str,
    client_secret: str,
    path: Path | None = None,
) -> Any:
    from google_auth_oauthlib.flow import InstalledAppFlow

    flow = InstalledAppFlow.from_client_config(
        client_config(client_id, client_secret),
        scopes=DRIVE_SCOPES,
    )
    creds = flow.run_local_server(port=0, prompt="consent", open_browser=True)
    save_credentials_json(creds.to_json(), path)
    return creds


def build_drive_service(creds: Any) -> Any:
    from googleapiclient.discovery import build

    return build("drive", "v3", credentials=creds, cache_discovery=False)


def signed_in(path: Path | None = None) -> bool:
    try:
        return load_saved_credentials(path) is not None
    except Exception:
        return False
