from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from PIL import Image

from desktop.talk_sheet_form import (
    apply_browsed_still,
    ask_open_still,
    commit_form_still,
    image_filetypes,
    normalize_dialog_path,
)
from pipeline.models import TalkSheet
from pipeline.talk_sheet import point_still_filename


def _tiny_jpg(path: Path, color: tuple[int, int, int] = (20, 40, 60)) -> Path:
    image = Image.new("RGB", (48, 32), color)
    image.save(path, "JPEG")
    return path


def _form_stub():
    from desktop.talk_sheet_form import TalkSheetForm

    form = TalkSheetForm.__new__(TalkSheetForm)
    form._on_change = None
    form._sheet = TalkSheet()
    form._still_paths = ["", "", ""]
    form._stills_dir = None
    form._preview_images = [None, None, None]
    form._preview_text = ["No still", "No still", "No still"]
    form._point_vars = [
        {"platform": SimpleNamespace(get=lambda: "MQ-9 Reaper")},
        {"platform": SimpleNamespace(get=lambda: "")},
        {"platform": SimpleNamespace(get=lambda: "")},
    ]

    class _Label:
        def __init__(self, owner: object, index: int) -> None:
            self.owner = owner
            self.index = index

        def configure(self, **kwargs: object) -> None:
            if "text" in kwargs:
                self.owner._preview_text[self.index] = str(kwargs["text"])
            if "image" in kwargs:
                self.owner._preview_images[self.index] = kwargs["image"]

    form._previews = [_Label(form, index) for index in range(3)]
    return form


def test_normalize_dialog_path_strips_windows_braces_and_quotes() -> None:
    spaced = "/tmp/My Photos/reaper.jpg"
    assert normalize_dialog_path("{" + spaced + "}") == str(Path(spaced))
    assert normalize_dialog_path(f'"{spaced}"') == str(Path(spaced))
    assert normalize_dialog_path("") == ""
    assert normalize_dialog_path(None) == ""


def test_windows_image_filetypes_are_single_wildcard() -> None:
    types = image_filetypes(platform="win32")
    patterns = [pattern for _label, pattern in types if pattern != "*.*"]
    assert patterns
    for pattern in patterns:
        assert " " not in pattern
        assert pattern.startswith("*.")
    assert {pattern.removeprefix("*.") for pattern in patterns} == {"jpg", "jpeg", "png", "webp"}


def test_ask_open_still_passes_parent_and_normalizes() -> None:
    seen: dict[str, object] = {}
    parent = SimpleNamespace(
        update_idletasks=lambda: None,
        attributes=lambda *_args, **_kwargs: 0,
    )

    def fake_ask(**kwargs: object) -> str:
        seen.update(kwargs)
        return "{/tmp/photo with space.webp}"

    path = ask_open_still(parent=parent, title="Point 1 still", filedialog_fn=fake_ask)
    assert seen["parent"] is parent
    assert seen["title"] == "Point 1 still"
    assert seen["filetypes"] == image_filetypes()
    assert path == str(Path("/tmp/photo with space.webp"))


def test_browse_still_copies_and_user_locks(tmp_path: Path) -> None:
    src = _tiny_jpg(tmp_path / "picked still.jpg")
    dest = tmp_path / "stills"
    form = _form_stub()
    form._browse_still(0, chosen=str(src), dest_dir=dest)

    stored = form._still_paths[0]
    copied = dest / point_still_filename(1, "MQ-9 Reaper", ".jpg")
    assert Path(stored) == copied
    assert copied.is_file()
    assert copied.read_bytes() == src.read_bytes()
    assert form._preview_text[0] in {"", copied.name}
    assert form._sheet.points[0].still_path == str(copied)
    assert form._sheet.points[0].still_source == "user"
    assert form._sheet.points[0].still_locked()

    sheet = TalkSheet()
    commit_form_still(sheet.points[0], stored)
    assert sheet.points[0].still_locked()


def test_browse_cancel_does_not_clear_existing_still(tmp_path: Path, monkeypatch) -> None:
    first = _tiny_jpg(tmp_path / "first.jpg", (10, 10, 10))
    dest = tmp_path / "stills"
    form = _form_stub()
    form._browse_still(0, chosen=str(first), dest_dir=dest)
    existing = form._still_paths[0]
    assert existing
    assert form._sheet.points[0].still_locked()

    form._browse_still(0, chosen="", dest_dir=dest)
    assert form._still_paths[0] == existing
    assert Path(existing).is_file()
    assert form._sheet.points[0].still_locked()

    monkeypatch.setattr("desktop.talk_sheet_form.ask_open_still", lambda **_kwargs: "")
    form._browse_still(0, dest_dir=dest)
    assert form._still_paths[0] == existing
    assert form._sheet.points[0].still_source == "user"


def test_repeat_browse_replaces_still(tmp_path: Path) -> None:
    dest = tmp_path / "stills"
    form = _form_stub()
    form._browse_still(0, chosen=str(_tiny_jpg(tmp_path / "one.jpg", (1, 2, 3))), dest_dir=dest)
    first = Path(form._still_paths[0])
    replacement = _tiny_jpg(tmp_path / "two.jpg", (200, 10, 10))
    form._browse_still(0, chosen=str(replacement), dest_dir=dest)
    stored = Path(form._still_paths[0])
    assert stored == first
    assert stored.read_bytes() == replacement.read_bytes()
    assert form._sheet.points[0].still_locked()


def test_apply_browsed_still_empty_leaves_list() -> None:
    paths = ["/kept/user.jpg", "", ""]
    assert apply_browsed_still(paths, 0, "", dest_dir=Path("/tmp"), platform="X") is None
    assert paths == ["/kept/user.jpg", "", ""]


def test_clear_still_unlocks(tmp_path: Path) -> None:
    form = _form_stub()
    form._browse_still(0, chosen=str(_tiny_jpg(tmp_path / "keep.jpg")), dest_dir=tmp_path / "stills")
    form._clear_still(0)
    assert form._still_paths[0] == ""
    assert form._preview_text[0] == "No still"
    assert not form._sheet.points[0].still_locked()
