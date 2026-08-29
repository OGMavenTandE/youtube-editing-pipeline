"""Google Drive inbox/outbox helpers. Chunked/resumable media, not base64."""

from __future__ import annotations

import json
import mimetypes
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Literal

from pydantic import BaseModel, Field

DRIVE_SCOPE = "https://www.googleapis.com/auth/drive"
DRIVE_FOLDER_MIME = "application/vnd.google-apps.folder"
PIPELINE_ROOT_NAME = "YouTube Pipeline"
INBOX_NAME = "inbox"
OUTBOX_NAME = "outbox"
DONE_NAME = "done"
SOURCE_SIDECAR_NAME = "source.json"
CLAIM_PROPERTY = "pipeline_status"
SOURCE_PROPERTY = "pipeline_source_id"
DOWNLOAD_CHUNK = 8 * 1024 * 1024
UPLOAD_CHUNK = 8 * 1024 * 1024
MP4_SUFFIXES = (".mp4",)
MP4_MIME_PREFIXES = ("video/mp4",)

ProgressCb = Callable[[float], None]


class DriveItem(BaseModel):
    """Minimal Drive file or folder."""

    id: str
    name: str
    mime_type: str = ""
    size: int | None = None
    parents: list[str] = Field(default_factory=list)
    properties: dict[str, str] = Field(default_factory=dict)

    @property
    def is_folder(self) -> bool:
        return self.mime_type == DRIVE_FOLDER_MIME


class PipelineFolders(BaseModel):
    root: DriveItem
    inbox: DriveItem
    outbox: DriveItem
    done: DriveItem


class ProcessedRecord(BaseModel):
    file_id: str
    name: str = ""
    stem: str = ""
    status: Literal["claimed", "done", "skipped", "error"] = "claimed"
    finished_at: str | None = None
    outbox_folder_id: str | None = None
    error: str | None = None


class SourceSidecar(BaseModel):
    source_file_id: str
    source_name: str = ""
    stem: str = ""


def pipeline_folder_relpath(kind: Literal["inbox", "outbox", "done"]) -> str:
    return f"{PIPELINE_ROOT_NAME}/{kind}"


def default_folder_names() -> tuple[str, str, str, str]:
    return PIPELINE_ROOT_NAME, INBOX_NAME, OUTBOX_NAME, DONE_NAME


def drive_folder_url(folder_id: str) -> str:
    return f"https://drive.google.com/drive/folders/{folder_id}"


def file_stem(filename: str) -> str:
    return Path(filename).stem


def sanitize_folder_name(name: str) -> str:
    cleaned = str(name).replace("/", "-").replace("\\", "-").strip()
    return (cleaned or "untitled")[:200]


def outbox_child_name(stem: str) -> str:
    return sanitize_folder_name(stem)


def is_mp4_name(name: str, mime: str | None = None) -> bool:
    lower = (name or "").lower()
    if lower.endswith(MP4_SUFFIXES):
        return True
    mime_l = (mime or "").lower()
    return any(mime_l.startswith(prefix) for prefix in MP4_MIME_PREFIXES)


def is_landscape_dimensions(width: int, height: int) -> bool:
    return width > 0 and height > 0 and width >= height


def download_range_headers(existing_bytes: int, total: int | None) -> dict[str, str]:
    if existing_bytes > 0 and (total is None or existing_bytes < total):
        return {"Range": f"bytes={existing_bytes}-"}
    return {}


def should_skip_download(existing_bytes: int, total: int | None) -> bool:
    return bool(total and existing_bytes == total)


def source_sidecar_payload(file_id: str, name: str, stem: str) -> dict[str, str]:
    return SourceSidecar(
        source_file_id=file_id,
        source_name=name,
        stem=stem,
    ).model_dump()


def sidecar_matches_source(payload: dict[str, Any], file_id: str) -> bool:
    if not file_id:
        return False
    return str(payload.get("source_file_id") or "") == file_id


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class ProcessedIdStore:
    """Local record of Drive file ids that were claimed or finished."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.records: dict[str, ProcessedRecord] = {}
        self.load()

    def load(self) -> None:
        self.records = {}
        if not self.path.is_file():
            return
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        items = raw.get("records", raw) if isinstance(raw, dict) else {}
        if not isinstance(items, dict):
            return
        for key, value in items.items():
            if not isinstance(value, dict):
                continue
            record = ProcessedRecord.model_validate({"file_id": key, **value})
            self.records[record.file_id] = record

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "records": {key: rec.model_dump() for key, rec in self.records.items()},
        }
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        tmp.replace(self.path)

    def get(self, file_id: str) -> ProcessedRecord | None:
        return self.records.get(file_id)

    def is_done(self, file_id: str) -> bool:
        record = self.get(file_id)
        return record is not None and record.status == "done"

    def is_claimed(self, file_id: str) -> bool:
        record = self.get(file_id)
        return record is not None and record.status == "claimed"

    def claim(self, file_id: str, name: str = "", stem: str = "") -> bool:
        """Return False if this file id is already claimed or done."""
        existing = self.get(file_id)
        if existing is not None and existing.status in {"claimed", "done"}:
            return False
        self.records[file_id] = ProcessedRecord(
            file_id=file_id,
            name=name,
            stem=stem,
            status="claimed",
        )
        self.save()
        return True

    def mark_done(
        self,
        file_id: str,
        *,
        name: str = "",
        stem: str = "",
        outbox_folder_id: str | None = None,
    ) -> ProcessedRecord:
        record = self.get(file_id) or ProcessedRecord(file_id=file_id)
        record.name = name or record.name
        record.stem = stem or record.stem
        record.status = "done"
        record.finished_at = _now_iso()
        record.outbox_folder_id = outbox_folder_id
        record.error = None
        self.records[file_id] = record
        self.save()
        return record

    def mark_skipped(self, file_id: str, *, name: str = "", stem: str = "") -> ProcessedRecord:
        record = self.get(file_id) or ProcessedRecord(file_id=file_id)
        record.name = name or record.name
        record.stem = stem or record.stem
        record.status = "skipped"
        record.finished_at = _now_iso()
        self.records[file_id] = record
        self.save()
        return record

    def mark_error(self, file_id: str, error: str) -> ProcessedRecord:
        record = self.get(file_id) or ProcessedRecord(file_id=file_id)
        record.status = "error"
        record.error = error
        record.finished_at = _now_iso()
        self.records[file_id] = record
        self.save()
        return record

    def release_claim(self, file_id: str) -> None:
        record = self.get(file_id)
        if record is None or record.status != "claimed":
            return
        del self.records[file_id]
        self.save()

    def release_in_progress(self) -> int:
        """Drop claimed-but-unfinished ids so a restarted watcher can retry."""
        stale = [key for key, record in self.records.items() if record.status == "claimed"]
        for key in stale:
            del self.records[key]
        if stale:
            self.save()
        return len(stale)


def _item_from_api(payload: dict[str, Any]) -> DriveItem:
    size_raw = payload.get("size")
    size = int(size_raw) if size_raw not in (None, "") else None
    props = payload.get("properties") or payload.get("appProperties") or {}
    return DriveItem(
        id=str(payload.get("id") or ""),
        name=str(payload.get("name") or ""),
        mime_type=str(payload.get("mimeType") or ""),
        size=size,
        parents=list(payload.get("parents") or []),
        properties={str(k): str(v) for k, v in dict(props).items()},
    )


def iter_drive_files(service: Any, query: str, fields: str = "id,name,mimeType,size,parents,properties") -> Iterator[dict[str, Any]]:
    page_token: str | None = None
    while True:
        response = (
            service.files()
            .list(
                q=query,
                spaces="drive",
                fields=f"nextPageToken, files({fields})",
                pageToken=page_token,
                pageSize=100,
                includeItemsFromAllDrives=False,
            )
            .execute()
        )
        for item in response.get("files", []):
            yield item
        page_token = response.get("nextPageToken")
        if not page_token:
            break


class DriveClient:
    """Drive operations used by the desktop watcher. Service is injected."""

    def __init__(self, service: Any) -> None:
        self.service = service

    def get_file(self, file_id: str, fields: str = "id,name,mimeType,size,parents,properties") -> DriveItem:
        payload = self.service.files().get(fileId=file_id, fields=fields).execute()
        return _item_from_api(payload)

    def list_folders(self, parent_id: str | None = "root") -> list[DriveItem]:
        parent = parent_id or "root"
        query = (
            f"'{parent}' in parents and mimeType='{DRIVE_FOLDER_MIME}' and trashed=false"
        )
        return [_item_from_api(item) for item in iter_drive_files(self.service, query)]

    def find_child_folder(self, parent_id: str | None, name: str) -> DriveItem | None:
        parent = parent_id or "root"
        safe = name.replace("'", "\\'")
        query = (
            f"'{parent}' in parents and mimeType='{DRIVE_FOLDER_MIME}' "
            f"and name='{safe}' and trashed=false"
        )
        for item in iter_drive_files(self.service, query):
            return _item_from_api(item)
        return None

    def create_folder(self, name: str, parent_id: str | None = "root") -> DriveItem:
        body: dict[str, Any] = {
            "name": name,
            "mimeType": DRIVE_FOLDER_MIME,
        }
        if parent_id:
            body["parents"] = [parent_id]
        payload = (
            self.service.files()
            .create(body=body, fields="id,name,mimeType,parents")
            .execute()
        )
        return _item_from_api(payload)

    def get_or_create_folder(self, name: str, parent_id: str | None = "root") -> DriveItem:
        found = self.find_child_folder(parent_id, name)
        if found is not None:
            return found
        return self.create_folder(name, parent_id)

    def ensure_pipeline_folders(
        self,
        root_name: str = PIPELINE_ROOT_NAME,
        parent_id: str | None = "root",
    ) -> PipelineFolders:
        root = self.get_or_create_folder(root_name, parent_id)
        inbox = self.get_or_create_folder(INBOX_NAME, root.id)
        outbox = self.get_or_create_folder(OUTBOX_NAME, root.id)
        done = self.get_or_create_folder(DONE_NAME, root.id)
        return PipelineFolders(root=root, inbox=inbox, outbox=outbox, done=done)

    def list_inbox_videos(self, inbox_id: str) -> list[DriveItem]:
        query = f"'{inbox_id}' in parents and trashed=false"
        found: list[DriveItem] = []
        for item in iter_drive_files(self.service, query):
            drive_item = _item_from_api(item)
            if drive_item.is_folder:
                continue
            if is_mp4_name(drive_item.name, drive_item.mime_type):
                found.append(drive_item)
        return found

    def claim_file(self, file_id: str) -> bool:
        """Mark a Drive file as claimed. False only when the file is already done."""
        meta = self.get_file(file_id, fields="id,properties")
        current = (meta.properties or {}).get(CLAIM_PROPERTY, "")
        if current == "done":
            return False
        if current != "claimed":
            self.service.files().update(
                fileId=file_id,
                body={"properties": {CLAIM_PROPERTY: "claimed"}},
                fields="id,properties",
            ).execute()
        return True

    def find_outbox_stem_folder(self, outbox_id: str, stem: str) -> DriveItem | None:
        return self.find_child_folder(outbox_id, outbox_child_name(stem))

    def outbox_has_final_mp4(self, outbox_id: str, stem: str, source_file_id: str) -> bool:
        folder = self.find_outbox_stem_folder(outbox_id, stem)
        if folder is None:
            return False
        query = f"'{folder.id}' in parents and trashed=false"
        sidecar_hit = False
        has_mp4 = False
        for item in iter_drive_files(self.service, query):
            drive_item = _item_from_api(item)
            if drive_item.name == SOURCE_SIDECAR_NAME:
                try:
                    payload = self._download_json(drive_item.id)
                except Exception:
                    payload = {}
                if sidecar_matches_source(payload, source_file_id):
                    sidecar_hit = True
                elif drive_item.properties.get(SOURCE_PROPERTY) == source_file_id:
                    sidecar_hit = True
            if is_mp4_name(drive_item.name, drive_item.mime_type):
                has_mp4 = True
                if drive_item.properties.get(SOURCE_PROPERTY) == source_file_id:
                    return True
        return bool(has_mp4 and sidecar_hit)

    def move_file(self, file_id: str, dest_folder_id: str, current_parent: str | None = None) -> None:
        item = self.get_file(file_id, fields="id,parents")
        previous = current_parent or (item.parents[0] if item.parents else None)
        kwargs: dict[str, Any] = {
            "fileId": file_id,
            "addParents": dest_folder_id,
            "fields": "id,parents",
        }
        if previous and previous != dest_folder_id:
            kwargs["removeParents"] = previous
        self.service.files().update(**kwargs).execute()

    def download_resumable(
        self,
        file_id: str,
        dest: Path,
        progress: ProgressCb | None = None,
    ) -> Path:
        """Chunked download with HTTP Range resume. Never encodes the file as base64."""
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        meta = self.get_file(file_id, fields="id,name,size")
        total = meta.size
        existing = dest.stat().st_size if dest.is_file() else 0
        if should_skip_download(existing, total):
            if progress:
                progress(1.0)
            return dest

        request = self.service.files().get_media(fileId=file_id)
        uri = getattr(request, "uri", "") or (
            f"https://www.googleapis.com/drive/v3/files/{file_id}?alt=media"
        )
        headers = download_range_headers(existing, total)
        session = self._authorized_session()
        if session is not None:
            self._stream_download(session, uri, dest, existing, total, headers, progress)
            return dest

        from googleapiclient.http import MediaIoBaseDownload

        if dest.is_file():
            dest.unlink()
        with dest.open("wb") as handle:
            downloader = MediaIoBaseDownload(handle, request, chunksize=DOWNLOAD_CHUNK)
            done = False
            while not done:
                status, done = downloader.next_chunk()
                if progress and status is not None:
                    progress(float(status.progress() or 0.0))
        if progress:
            progress(1.0)
        return dest

    def upload_file_resumable(
        self,
        local_path: Path,
        parent_id: str,
        name: str | None = None,
        properties: dict[str, str] | None = None,
        existing_id: str | None = None,
        progress: ProgressCb | None = None,
    ) -> DriveItem:
        from googleapiclient.http import MediaFileUpload

        local_path = Path(local_path)
        filename = name or local_path.name
        mime, _ = mimetypes.guess_type(filename)
        media = MediaFileUpload(
            str(local_path),
            mimetype=mime or "application/octet-stream",
            resumable=True,
            chunksize=UPLOAD_CHUNK,
        )
        body: dict[str, Any] = {"name": filename}
        if properties:
            body["properties"] = properties
        if existing_id:
            request = self.service.files().update(
                fileId=existing_id,
                body=body,
                media_body=media,
                fields="id,name,mimeType,parents,properties",
            )
        else:
            body["parents"] = [parent_id]
            request = self.service.files().create(
                body=body,
                media_body=media,
                fields="id,name,mimeType,parents,properties",
            )
        response = None
        while response is None:
            status, response = request.next_chunk()
            if progress and status is not None:
                progress(float(status.progress() or 0.0))
        if progress:
            progress(1.0)
        return _item_from_api(response)

    def upload_studio_package(
        self,
        local_dir: Path,
        outbox_id: str,
        stem: str,
        source_file_id: str,
        source_name: str = "",
        progress: ProgressCb | None = None,
    ) -> DriveItem:
        local_dir = Path(local_dir)
        if not local_dir.is_dir():
            raise FileNotFoundError(f"Studio folder not found: {local_dir}")
        folder = self.find_outbox_stem_folder(outbox_id, stem)
        if folder is None:
            folder = self.create_folder(outbox_child_name(stem), outbox_id)

        sidecar_path = local_dir / SOURCE_SIDECAR_NAME
        sidecar_path.write_text(
            json.dumps(source_sidecar_payload(source_file_id, source_name, stem), indent=2)
            + "\n",
            encoding="utf-8",
        )

        existing = {
            item.name: item
            for item in (
                _item_from_api(raw)
                for raw in iter_drive_files(
                    self.service, f"'{folder.id}' in parents and trashed=false"
                )
            )
        }
        files = [path for path in sorted(local_dir.iterdir()) if path.is_file()]
        total = max(1, len(files))
        for index, path in enumerate(files):
            props = {SOURCE_PROPERTY: source_file_id} if is_mp4_name(path.name) or path.name == SOURCE_SIDECAR_NAME else None
            prior = existing.get(path.name)
            self.upload_file_resumable(
                path,
                folder.id,
                name=path.name,
                properties=props,
                existing_id=prior.id if prior else None,
            )
            if progress:
                progress((index + 1) / total)
        return folder

    def _download_json(self, file_id: str) -> dict[str, Any]:
        request = self.service.files().get_media(fileId=file_id)
        raw = request.execute()
        if isinstance(raw, bytes):
            text = raw.decode("utf-8")
        else:
            text = str(raw)
        payload = json.loads(text)
        return payload if isinstance(payload, dict) else {}

    def _authorized_session(self) -> Any | None:
        http = getattr(self.service, "_http", None)
        creds = getattr(http, "credentials", None) if http is not None else None
        if creds is None:
            return None
        from google.auth.transport.requests import AuthorizedSession

        return AuthorizedSession(creds)

    def _stream_download(
        self,
        session: Any,
        uri: str,
        dest: Path,
        existing: int,
        total: int | None,
        headers: dict[str, str],
        progress: ProgressCb | None,
    ) -> None:
        resume = bool(existing and headers)
        mode = "ab" if resume else "wb"
        if not resume and dest.exists():
            dest.unlink()
            existing = 0
        response = session.get(uri, headers=headers or None, stream=True, timeout=120)
        if response.status_code not in {200, 206}:
            response.close()
            raise RuntimeError(f"Drive download failed (HTTP {response.status_code}).")
        downloaded = existing if resume else 0
        try:
            with dest.open(mode) as handle:
                for chunk in response.iter_content(chunk_size=DOWNLOAD_CHUNK):
                    if not chunk:
                        continue
                    handle.write(chunk)
                    downloaded += len(chunk)
                    if progress and total:
                        progress(min(1.0, downloaded / total))
        finally:
            response.close()
        if progress:
            progress(1.0)
