"""Fillable talk-sheet form: open overview, three points, locked close."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Callable

import customtkinter as ctk

from pipeline.config import identity_from_settings, load_settings
from pipeline.models import TALK_POINT_COUNT, TalkPoint, TalkSheet
from pipeline.picture_kit import HEADLINE_CHAR_LIMIT, KICKER_CHAR_LIMIT
from pipeline.talk_sheet import (
    apply_open_form_values,
    apply_point_form_values,
    copy_point_still,
    default_stills_dir,
    default_talk_sheet_md_path,
    parse_talk_sheet_markdown,
    talk_sheet_to_markdown,
)

OVERVIEW_LINE_LIMIT = HEADLINE_CHAR_LIMIT // 2

MARKDOWN_TYPES = [("Markdown", "*.md *.markdown *.txt"), ("All files", "*.*")]
OnChange = Callable[[], None]


def _bind_char_limit(var: ctk.StringVar, limit: int, counter: ctk.CTkLabel | None = None) -> None:
    """Hard max. Trim pasted overflow. Optional live counter."""

    def _trim(*_args: object) -> None:
        text = var.get()
        if len(text) > limit:
            var.set(text[:limit])
            text = var.get()
        if counter is not None:
            counter.configure(text=f"{len(text)}/{limit}")

    var.trace_add("write", _trim)
    _trim()


def _limited_entry(
    parent: ctk.CTkFrame,
    row: int,
    label: str,
    var: ctk.StringVar,
    limit: int,
    *,
    columnspan: int = 2,
) -> None:
    ctk.CTkLabel(parent, text=label).grid(row=row, column=0, sticky="w", padx=12, pady=4)
    ctk.CTkEntry(parent, textvariable=var).grid(
        row=row, column=1, columnspan=columnspan, sticky="ew", padx=12, pady=4
    )
    counter = ctk.CTkLabel(parent, text=f"0/{limit}", text_color="#888888")
    counter.grid(row=row, column=1 + columnspan, sticky="e", padx=(0, 12), pady=4)
    _bind_char_limit(var, limit, counter)


def image_filetypes(*, platform: str | None = None) -> list[tuple[str, str]]:
    """Windows GetOpenFileName rejects space-separated multi-wildcards.

    One pattern per type so Open returns the path instead of ''.
    """
    if (platform or sys.platform) == "win32":
        return [
            ("JPEG", "*.jpg"),
            ("JPEG", "*.jpeg"),
            ("PNG", "*.png"),
            ("WebP", "*.webp"),
            ("All files", "*.*"),
        ]
    return [("Images", "*.jpg *.jpeg *.png *.webp"), ("All files", "*.*")]


IMAGE_TYPES = image_filetypes()


def normalize_dialog_path(raw: object) -> str:
    """Turn a filedialog result into a usable path. Empty is cancel."""
    text = str(raw or "").strip()
    if len(text) >= 2 and text[0] == "{" and text[-1] == "}" and text.count("{") == 1:
        text = text[1:-1].strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {'"', "'"}:
        text = text[1:-1].strip()
    if not text:
        return ""
    return str(Path(text))


def commit_form_still(point: TalkPoint, still: str) -> None:
    still = (still or "").strip()
    if not still:
        point.still_path = ""
        point.still_source = "empty"
        return
    if still != point.still_path.strip():
        point.still_path = still
        point.still_source = "user"
        return
    point.still_path = still
    if point.still_source == "empty":
        point.still_source = "user"


def apply_browsed_still(
    still_paths: list[str],
    index: int,
    chosen: str,
    *,
    dest_dir: Path,
    platform: str = "",
) -> str | None:
    """Copy a dialog result into dest_dir as pointN_*.ext.

    Empty chosen is cancel and leaves still_paths unchanged.
    """
    path = normalize_dialog_path(chosen)
    if not path:
        return None
    copied = copy_point_still(Path(path), dest_dir, index + 1, platform)
    still_paths[index] = str(copied)
    return still_paths[index]


def ask_open_still(
    *,
    parent: Any | None = None,
    title: str = "Point still",
    filedialog_fn: Callable[..., Any] | None = None,
) -> str:
    """Open the native image picker owned by the CTk/Tk toplevel.

    No parent is the usual Windows CTk miss: Open dismisses and returns ''.
    """
    from tkinter import filedialog

    ask = filedialog_fn or filedialog.askopenfilename
    kwargs: dict[str, Any] = {"title": title, "filetypes": image_filetypes()}
    topmost = False
    if parent is not None:
        kwargs["parent"] = parent
        try:
            parent.update_idletasks()
        except Exception:
            pass
        try:
            topmost = bool(int(parent.attributes("-topmost")))
        except Exception:
            topmost = False
        if topmost:
            try:
                parent.attributes("-topmost", False)
                parent.update_idletasks()
            except Exception:
                topmost = False
    try:
        raw = ask(**kwargs)
    finally:
        if topmost and parent is not None:
            try:
                parent.attributes("-topmost", True)
            except Exception:
                pass
    return normalize_dialog_path(raw)


class TalkSheetForm(ctk.CTkScrollableFrame):
    def __init__(self, master: ctk.CTk | ctk.CTkFrame, *, on_change: OnChange | None = None) -> None:
        super().__init__(master, fg_color="transparent")
        self._on_change = on_change
        self._sheet = TalkSheet()
        self._previews: list[ctk.CTkLabel] = []
        self._preview_images: list[object] = [None, None, None]
        self._still_paths: list[str] = ["", "", ""]
        self._stills_dir: Path | None = None
        self._build()
        self.load_sheet(TalkSheet())

    def _build(self) -> None:
        self.grid_columnconfigure(0, weight=1)

        import_row = ctk.CTkFrame(self, fg_color="transparent")
        import_row.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        ctk.CTkButton(import_row, text="Load markdown", width=140, command=self._load_markdown).pack(
            side="left", padx=(0, 8)
        )
        ctk.CTkButton(import_row, text="Apply paste", width=120, command=self._apply_paste).pack(
            side="left", padx=(0, 8)
        )
        ctk.CTkButton(import_row, text="Copy markdown", width=140, command=self._copy_markdown).pack(
            side="left"
        )
        self.paste_box = ctk.CTkTextbox(self, height=72, wrap="word")
        self.paste_box.grid(row=1, column=0, sticky="ew", pady=(0, 12))
        self.paste_box.insert("1.0", "Paste a talk_sheet.md here, then Apply paste.")

        open_box = ctk.CTkFrame(self)
        open_box.grid(row=2, column=0, sticky="ew", pady=(0, 12))
        open_box.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(open_box, text="Open overview", font=ctk.CTkFont(size=16, weight="bold")).grid(
            row=0, column=0, columnspan=3, sticky="w", padx=12, pady=(10, 6)
        )
        self.title_var = ctk.StringVar()
        _limited_entry(open_box, 1, "Title (kicker)", self.title_var, KICKER_CHAR_LIMIT, columnspan=1)
        self.line1_var = ctk.StringVar()
        _limited_entry(open_box, 2, "Overview line 1", self.line1_var, OVERVIEW_LINE_LIMIT, columnspan=1)
        self.line2_var = ctk.StringVar()
        _limited_entry(open_box, 3, "Overview line 2", self.line2_var, OVERVIEW_LINE_LIMIT, columnspan=1)
        ctk.CTkLabel(open_box, text="Spoken notes (not painted)").grid(
            row=4, column=0, sticky="nw", padx=12, pady=4
        )
        self.notes_box = ctk.CTkTextbox(open_box, height=56, wrap="word")
        self.notes_box.grid(row=4, column=1, sticky="ew", padx=12, pady=(4, 10))

        self._point_vars: list[dict[str, ctk.StringVar]] = []
        for index in range(TALK_POINT_COUNT):
            self._build_point(index)

        close_box = ctk.CTkFrame(self)
        close_box.grid(row=6, column=0, sticky="ew", pady=(0, 8))
        ctk.CTkLabel(close_box, text="Close + identity (locked)", font=ctk.CTkFont(size=16, weight="bold")).pack(
            anchor="w", padx=12, pady=(10, 4)
        )
        identity = identity_from_settings(load_settings())
        close_lines = [
            "WORK WITH ME",
            "Independent AI T&E.  Vendor-agnostic.",
            identity.name,
            identity.title_line,
            identity.affiliations,
            identity.mission,
            "FIND ME  " + "  ·  ".join(identity.find_me),
        ]
        self.close_preview = ctk.CTkLabel(
            close_box,
            text="\n".join(line for line in close_lines if line),
            justify="left",
            anchor="w",
            wraplength=720,
        )
        self.close_preview.pack(anchor="w", padx=12, pady=(0, 12))

    def _build_point(self, index: int) -> None:
        box = ctk.CTkFrame(self)
        box.grid(row=3 + index, column=0, sticky="ew", pady=(0, 12))
        box.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(box, text=f"Point {index + 1}", font=ctk.CTkFont(size=16, weight="bold")).grid(
            row=0, column=0, columnspan=4, sticky="w", padx=12, pady=(10, 6)
        )
        platform = ctk.StringVar()
        ctk.CTkLabel(box, text="Platform / still query").grid(row=1, column=0, sticky="w", padx=12, pady=4)
        ctk.CTkEntry(box, textvariable=platform).grid(row=1, column=1, columnspan=2, sticky="ew", padx=12, pady=4)

        btn_row = ctk.CTkFrame(box, fg_color="transparent")
        btn_row.grid(row=2, column=0, columnspan=3, sticky="ew", padx=12, pady=4)
        ctk.CTkButton(
            btn_row,
            text="Browse image",
            width=130,
            command=lambda i=index: self._browse_still(i),
        ).pack(side="left", padx=(0, 8))
        ctk.CTkButton(
            btn_row,
            text="Clear",
            width=80,
            fg_color="#4a4a4a",
            command=lambda i=index: self._clear_still(i),
        ).pack(side="left")

        preview = ctk.CTkLabel(box, text="No still", width=160, height=90, anchor="center")
        preview.grid(row=3, column=0, columnspan=3, sticky="w", padx=12, pady=4)
        self._previews.append(preview)

        image_title = ctk.StringVar()
        _limited_entry(box, 4, "Image title (optional)", image_title, KICKER_CHAR_LIMIT)
        image_text = ctk.StringVar()
        _limited_entry(box, 5, "Image text", image_text, HEADLINE_CHAR_LIMIT)

        cards: list[ctk.StringVar] = []
        titles: list[ctk.StringVar] = []
        labels = ("Card 1", "Card 2", "Card 3 (optional)")
        for card_i, label in enumerate(labels):
            title_var = ctk.StringVar()
            headline_var = ctk.StringVar()
            row = 6 + card_i * 2
            _limited_entry(box, row, f"{label} title (gold on card)", title_var, KICKER_CHAR_LIMIT)
            _limited_entry(box, row + 1, label, headline_var, HEADLINE_CHAR_LIMIT)
            titles.append(title_var)
            cards.append(headline_var)
        self._point_vars.append(
            {
                "platform": platform,
                "image_title": image_title,
                "image_text": image_text,
                "t1": titles[0],
                "t2": titles[1],
                "t3": titles[2],
                "c1": cards[0],
                "c2": cards[1],
                "c3": cards[2],
            }
        )

    def load_sheet(self, sheet: TalkSheet) -> None:
        self._sheet = sheet.model_copy(deep=True)
        self.title_var.set(self._sheet.title)
        line1, line2 = self._sheet.headline_lines()
        self.line1_var.set(line1)
        self.line2_var.set(line2)
        self.notes_box.delete("1.0", "end")
        if self._sheet.exec_notes:
            self.notes_box.insert("1.0", self._sheet.exec_notes)
        for index, point in enumerate(self._sheet.points):
            vars_ = self._point_vars[index]
            vars_["platform"].set(point.platform)
            vars_["image_title"].set(point.image_title)
            vars_["image_text"].set(point.image_text)
            vars_["t1"].set(point.titles[0])
            vars_["t2"].set(point.titles[1])
            vars_["t3"].set(point.titles[2])
            vars_["c1"].set(point.cards[0])
            vars_["c2"].set(point.cards[1])
            vars_["c3"].set(point.cards[2])
            self._still_paths[index] = point.still_path
            self._set_preview(index, point.still_path)

    def collect_sheet(self) -> TalkSheet:
        sheet = self._sheet.model_copy(deep=True)
        apply_open_form_values(
            sheet,
            self.title_var.get(),
            self.line1_var.get(),
            self.line2_var.get(),
            self.notes_box.get("1.0", "end"),
        )
        for index, vars_ in enumerate(self._point_vars):
            point = sheet.points[index]
            apply_point_form_values(
                point,
                platform=vars_["platform"].get(),
                image_title=vars_["image_title"].get(),
                image_text=vars_["image_text"].get(),
                titles=[vars_["t1"].get(), vars_["t2"].get(), vars_["t3"].get()],
                cards=[vars_["c1"].get(), vars_["c2"].get(), vars_["c3"].get()],
            )
            commit_form_still(point, self._still_paths[index])
        sheet.close_card.kicker = "WORK WITH ME"
        sheet.close_card.headline = "Independent AI T&E.\nVendor-agnostic."
        sheet.close_card.icon = "share"
        return sheet

    def _load_markdown(self) -> None:
        from tkinter import filedialog

        initial = default_talk_sheet_md_path()
        initialdir = str(initial.parent) if initial.parent.is_dir() else None
        path = filedialog.askopenfilename(
            title="Load talk sheet markdown",
            initialdir=initialdir,
            initialfile=initial.name if initial.is_file() else "",
            filetypes=MARKDOWN_TYPES,
        )
        if not path:
            return
        text = Path(path).read_text(encoding="utf-8")
        self.paste_box.delete("1.0", "end")
        self.paste_box.insert("1.0", text)
        self._import_markdown(text)

    def _apply_paste(self) -> None:
        text = self.paste_box.get("1.0", "end")
        self._import_markdown(text)

    def _copy_markdown(self) -> None:
        text = talk_sheet_to_markdown(self.collect_sheet())
        self.paste_box.delete("1.0", "end")
        self.paste_box.insert("1.0", text)
        try:
            top = self.winfo_toplevel()
            top.clipboard_clear()
            top.clipboard_append(text)
        except Exception:
            pass

    def _import_markdown(self, text: str) -> None:
        current = self.collect_sheet()
        imported = parse_talk_sheet_markdown(text, base=current)
        self.load_sheet(imported)
        if self._on_change:
            self._on_change()

    def _dialog_parent(self) -> Any | None:
        try:
            return self.winfo_toplevel()
        except Exception:
            return None

    def _resolve_stills_dir(self) -> Path:
        if self._stills_dir is not None:
            return Path(self._stills_dir)
        return default_stills_dir()

    def _point_platform(self, index: int) -> str:
        if index < 0 or index >= len(self._point_vars):
            return ""
        return str(self._point_vars[index]["platform"].get() or "")

    def _browse_still(
        self,
        index: int,
        chosen: str | None = None,
        dest_dir: Path | None = None,
    ) -> None:
        if chosen is None:
            chosen = ask_open_still(
                parent=self._dialog_parent(),
                title=f"Point {index + 1} still",
            )
        attached = apply_browsed_still(
            self._still_paths,
            index,
            chosen,
            dest_dir=Path(dest_dir) if dest_dir is not None else self._resolve_stills_dir(),
            platform=self._point_platform(index),
        )
        if attached is None:
            return
        commit_form_still(self._sheet.points[index], attached)
        self._set_preview(index, attached)
        if self._on_change:
            self._on_change()

    def _clear_still(self, index: int) -> None:
        self._still_paths[index] = ""
        commit_form_still(self._sheet.points[index], "")
        self._set_preview(index, "")
        if self._on_change:
            self._on_change()

    def _set_preview(self, index: int, path: str) -> None:
        label = self._previews[index]
        resolved = normalize_dialog_path(path)
        if not resolved:
            self._preview_images[index] = None
            label.configure(image=None, text="No still")
            return
        file_path = Path(resolved)
        if not file_path.is_file():
            self._preview_images[index] = None
            label.configure(image=None, text=file_path.name)
            return
        try:
            from PIL import Image

            image = Image.open(file_path)
            image.thumbnail((160, 90))
            photo = ctk.CTkImage(light_image=image, dark_image=image, size=image.size)
            self._preview_images[index] = photo
            label.configure(image=photo, text="")
        except Exception:
            self._preview_images[index] = None
            label.configure(image=None, text=file_path.name)
