from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

from pipeline.drive_io import (
    DRIVE_FOLDER_MIME,
    SOURCE_SIDECAR_NAME,
    DriveClient,
    ProcessedIdStore,
    download_range_headers,
    drive_folder_url,
    file_stem,
    is_landscape_dimensions,
    is_mp4_name,
    outbox_child_name,
    pipeline_folder_relpath,
    sanitize_folder_name,
    should_skip_download,
    sidecar_matches_source,
    source_sidecar_payload,
)


class FakeFiles:
    def __init__(self, store: dict[str, dict[str, Any]]) -> None:
        self.store = store

    def list(self, **kwargs: Any) -> MagicMock:
        query = kwargs.get("q", "")
        matches = [item for item in self.store.values() if _query_match(item, query)]
        request = MagicMock()
        request.execute.return_value = {"files": matches}
        return request

    def get(self, **kwargs: Any) -> MagicMock:
        file_id = kwargs["fileId"]
        request = MagicMock()
        request.execute.return_value = dict(self.store[file_id])
        return request

    def create(self, **kwargs: Any) -> MagicMock:
        body = dict(kwargs.get("body") or {})
        new_id = f"id-{len(self.store) + 1}"
        item = {
            "id": new_id,
            "name": body.get("name"),
            "mimeType": body.get("mimeType", "application/octet-stream"),
            "parents": list(body.get("parents") or ["root"]),
            "properties": dict(body.get("properties") or {}),
        }
        self.store[new_id] = item
        request = MagicMock()
        request.execute.return_value = item
        return request

    def update(self, **kwargs: Any) -> MagicMock:
        file_id = kwargs["fileId"]
        item = self.store[file_id]
        body = kwargs.get("body") or {}
        if "properties" in body:
            item.setdefault("properties", {}).update(body["properties"])
        add_parents = kwargs.get("addParents")
        remove_parents = kwargs.get("removeParents")
        parents = list(item.get("parents") or [])
        if add_parents and add_parents not in parents:
            parents.append(add_parents)
        if remove_parents and remove_parents in parents:
            parents.remove(remove_parents)
        item["parents"] = parents
        request = MagicMock()
        request.execute.return_value = item
        return request


class FakeService:
    def __init__(self, store: dict[str, dict[str, Any]] | None = None) -> None:
        self.store = store if store is not None else {}
        self._files = FakeFiles(self.store)

    def files(self) -> FakeFiles:
        return self._files


def _query_match(item: dict[str, Any], query: str) -> bool:
    if "trashed=false" in query and item.get("trashed"):
        return False
    for chunk in query.split(" and "):
        chunk = chunk.strip()
        if " in parents" in chunk:
            parent = chunk.split(" in parents")[0].strip().strip("'")
            if parent not in item.get("parents", []):
                return False
        if chunk.startswith("mimeType="):
            mime = chunk.split("=", 1)[1].strip().strip("'")
            if item.get("mimeType") != mime:
                return False
        if chunk.startswith("name="):
            name = chunk.split("=", 1)[1].strip().strip("'")
            if item.get("name") != name:
                return False
    return True


def test_folder_relpaths() -> None:
    assert pipeline_folder_relpath("inbox") == "YouTube Pipeline/inbox"
    assert pipeline_folder_relpath("outbox") == "YouTube Pipeline/outbox"
    assert pipeline_folder_relpath("done") == "YouTube Pipeline/done"


def test_file_stem_and_sanitize() -> None:
    assert file_stem("Talk 01.mp4") == "Talk 01"
    assert sanitize_folder_name("foo/bar\\baz") == "foo-bar-baz"
    assert sanitize_folder_name("   ") == "untitled"
    assert outbox_child_name("Talk 01") == "Talk 01"


def test_drive_folder_url() -> None:
    assert drive_folder_url("abc123") == "https://drive.google.com/drive/folders/abc123"


def test_is_mp4_name() -> None:
    assert is_mp4_name("clip.MP4")
    assert is_mp4_name("clip", "video/mp4")
    assert not is_mp4_name("notes.txt", "text/plain")


def test_landscape_dimensions() -> None:
    assert is_landscape_dimensions(1920, 1080)
    assert is_landscape_dimensions(1080, 1080)
    assert not is_landscape_dimensions(1080, 1920)
    assert not is_landscape_dimensions(0, 1080)


def test_download_resume_helpers() -> None:
    assert download_range_headers(0, 100) == {}
    assert download_range_headers(40, 100) == {"Range": "bytes=40-"}
    assert should_skip_download(100, 100)
    assert not should_skip_download(40, 100)
    assert not should_skip_download(0, None)


def test_source_sidecar_roundtrip() -> None:
    payload = source_sidecar_payload("file-1", "Talk.mp4", "Talk")
    assert sidecar_matches_source(payload, "file-1")
    assert not sidecar_matches_source(payload, "other")


def test_processed_store_claim_and_done(tmp_path: Path) -> None:
    store = ProcessedIdStore(tmp_path / "processed.json")
    assert store.claim("a", name="a.mp4", stem="a")
    assert not store.claim("a")
    assert store.is_claimed("a")
    store.mark_done("a", name="a.mp4", stem="a", outbox_folder_id="out-1")
    again = ProcessedIdStore(tmp_path / "processed.json")
    assert again.is_done("a")
    assert again.get("a") is not None
    assert again.get("a").outbox_folder_id == "out-1"


def test_processed_store_releases_stale_claims(tmp_path: Path) -> None:
    store = ProcessedIdStore(tmp_path / "processed.json")
    store.claim("stale", name="stale.mp4")
    assert store.release_in_progress() == 1
    assert store.claim("stale", name="stale.mp4")


def test_processed_store_error_allows_retry(tmp_path: Path) -> None:
    store = ProcessedIdStore(tmp_path / "processed.json")
    store.claim("b", name="b.mp4")
    store.mark_error("b", "boom")
    assert not store.is_done("b")
    assert store.claim("b", name="b.mp4")


def test_ensure_pipeline_folders() -> None:
    service = FakeService()
    client = DriveClient(service)
    folders = client.ensure_pipeline_folders()
    assert folders.root.name == "YouTube Pipeline"
    assert folders.inbox.name == "inbox"
    assert folders.outbox.name == "outbox"
    assert folders.done.name == "done"
    again = client.ensure_pipeline_folders()
    assert again.inbox.id == folders.inbox.id


def test_list_inbox_videos_filters_mp4() -> None:
    service = FakeService(
        {
            "in": {"id": "in", "name": "inbox", "mimeType": DRIVE_FOLDER_MIME, "parents": ["root"]},
            "v1": {
                "id": "v1",
                "name": "talk.mp4",
                "mimeType": "video/mp4",
                "parents": ["in"],
            },
            "txt": {"id": "txt", "name": "notes.txt", "mimeType": "text/plain", "parents": ["in"]},
        }
    )
    videos = DriveClient(service).list_inbox_videos("in")
    assert [item.id for item in videos] == ["v1"]


def test_claim_by_file_id() -> None:
    service = FakeService(
        {"f1": {"id": "f1", "name": "talk.mp4", "mimeType": "video/mp4", "parents": ["in"], "properties": {}}}
    )
    client = DriveClient(service)
    assert client.claim_file("f1")
    assert client.claim_file("f1")
    assert service.store["f1"]["properties"]["pipeline_status"] == "claimed"
    service.store["f1"]["properties"]["pipeline_status"] = "done"
    assert not client.claim_file("f1")


def test_move_inbox_to_done() -> None:
    service = FakeService(
        {
            "f1": {"id": "f1", "name": "talk.mp4", "mimeType": "video/mp4", "parents": ["inbox"]},
        }
    )
    DriveClient(service).move_file("f1", "done", "inbox")
    assert service.store["f1"]["parents"] == ["done"]


def test_outbox_has_final_mp4_from_sidecar() -> None:
    service = FakeService(
        {
            "out": {"id": "out", "name": "outbox", "mimeType": DRIVE_FOLDER_MIME, "parents": ["root"]},
            "stem": {
                "id": "stem",
                "name": "talk",
                "mimeType": DRIVE_FOLDER_MIME,
                "parents": ["out"],
            },
            "mp4": {
                "id": "mp4",
                "name": "talk_final.mp4",
                "mimeType": "video/mp4",
                "parents": ["stem"],
                "properties": {"pipeline_source_id": "src-1"},
            },
            "side": {
                "id": "side",
                "name": SOURCE_SIDECAR_NAME,
                "mimeType": "application/json",
                "parents": ["stem"],
            },
        }
    )
    client = DriveClient(service)
    client._download_json = lambda _file_id: source_sidecar_payload("src-1", "talk.mp4", "talk")  # type: ignore[method-assign]
    assert client.outbox_has_final_mp4("out", "talk", "src-1")
    assert not client.outbox_has_final_mp4("out", "talk", "other")


def test_outbox_missing_stem_is_not_done() -> None:
    service = FakeService(
        {"out": {"id": "out", "name": "outbox", "mimeType": DRIVE_FOLDER_MIME, "parents": ["root"]}}
    )
    assert not DriveClient(service).outbox_has_final_mp4("out", "talk", "src-1")
