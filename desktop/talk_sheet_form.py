"""Fillable talk-sheet form: open overview, three points, locked close."""

from __future__ import annotations

from pathlib import Path
from typing import Callable

import customtkinter as ctk

from pipeline.config import identity_from_settings, load_settings
from pipeline.models import TALK_POINT_COUNT, TalkPoint, TalkSheet
from pipeline.talk_sheet import (
    collect_form_text,
    default_talk_sheet_md_path,
    parse_talk_sheet_markdown,
)

IMAGE_TYPES = [("Images", "*.jpg *.jpeg *.png *.webp"), ("All files", "*.*")]
MARKDOWN_TYPES = [("Markdown", "*.md *.markdown *.txt"), ("All files", "*.*")]

OnChange = Callable[[], None]


class TalkSheetForm(ctk.CTkScrollableFrame):
    def __init__(self, master: ctk.CTk | ctk.CTkFrame, *, on_change: OnChange | None = None) -> None:
        super().__init__(master, fg_color="transparent")
        self._on_change = on_change
        self._sheet = TalkSheet()
        self._previews: list[ctk.CTkLabel] = []
        self._preview_images: list[object] = [None, None, None]
        self._still_paths: list[str] = ["", "", ""]
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
            side="left"
        )
        self.paste_box = ctk.CTkTextbox(self, height=72, wrap="word")
        self.paste_box.grid(row=1, column=0, sticky="ew", pady=(0, 12))
        self.paste_box.insert("1.0", "Paste a talk_sheet.md here, then Apply paste.")

        open_box = ctk.CTkFrame(self)
        open_box.grid(row=2, column=0, sticky="ew", pady=(0, 12))
        open_box.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(open_box, text="Open overview", font=ctk.CTkFont(size=16, weight="bold")).grid(
            row=0, column=0, columnspan=2, sticky="w", padx=12, pady=(10, 6)
        )
        ctk.CTkLabel(open_box, text="Title (kicker)").grid(row=1, column=0, sticky="w", padx=12, pady=4)
        self.title_var = ctk.StringVar()
        ctk.CTkEntry(open_box, textvariable=self.title_var).grid(
            row=1, column=1, sticky="ew", padx=12, pady=4
        )
        ctk.CTkLabel(open_box, text="Overview line 1").grid(row=2, column=0, sticky="w", padx=12, pady=4)
        self.line1_var = ctk.StringVar()
        ctk.CTkEntry(open_box, textvariable=self.line1_var).grid(
            row=2, column=1, sticky="ew", padx=12, pady=4
        )
        ctk.CTkLabel(open_box, text="Overview line 2").grid(row=3, column=0, sticky="w", padx=12, pady=4)
        self.line2_var = ctk.StringVar()
        ctk.CTkEntry(open_box, textvariable=self.line2_var).grid(
            row=3, column=1, sticky="ew", padx=12, pady=4
        )
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
        )
        self.close_preview.pack(anchor="w", padx=12, pady=(0, 12))

    def _build_point(self, index: int) -> None:
        box = ctk.CTkFrame(self)
        box.grid(row=3 + index, column=0, sticky="ew", pady=(0, 12))
        box.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(box, text=f"Point {index + 1}", font=ctk.CTkFont(size=16, weight="bold")).grid(
            row=0, column=0, columnspan=3, sticky="w", padx=12, pady=(10, 6)
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

        cards: list[ctk.StringVar] = []
        labels = ("Card 1", "Card 2", "Card 3 (optional)")
        for card_i, label in enumerate(labels):
            var = ctk.StringVar()
            ctk.CTkLabel(box, text=label).grid(row=4 + card_i, column=0, sticky="w", padx=12, pady=4)
            ctk.CTkEntry(box, textvariable=var).grid(
                row=4 + card_i, column=1, columnspan=2, sticky="ew", padx=12, pady=4
            )
            cards.append(var)
        self._point_vars.append({"platform": platform, "c1": cards[0], "c2": cards[1], "c3": cards[2]})

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
            vars_["c1"].set(point.cards[0])
            vars_["c2"].set(point.cards[1])
            vars_["c3"].set(point.cards[2])
            self._still_paths[index] = point.still_path
            self._set_preview(index, point.still_path)

    def collect_sheet(self) -> TalkSheet:
        sheet = self._sheet.model_copy(deep=True)
        title, title_source = collect_form_text(self.title_var.get(), sheet.title, sheet.title_source)
        sheet.title = title
        sheet.title_source = title_source
        if title:
            sheet.open_card.kicker = title
        line1 = self.line1_var.get().strip()
        line2 = self.line2_var.get().strip()
        combined = "\n".join(part for part in (line1, line2) if part)
        headline, headline_source = collect_form_text(
            combined, sheet.exec_headline, sheet.exec_headline_source
        )
        sheet.exec_headline = headline
        sheet.exec_headline_source = headline_source
        if headline:
            sheet.open_card.headline = headline
        sheet.exec_notes = self.notes_box.get("1.0", "end").strip()
        for index, vars_ in enumerate(self._point_vars):
            point = sheet.points[index]
            platform, platform_source = collect_form_text(
                vars_["platform"].get(), point.platform, point.platform_source
            )
            point.platform = platform
            point.platform_source = platform_source
            cards = [vars_["c1"].get(), vars_["c2"].get(), vars_["c3"].get()]
            for card_i, typed in enumerate(cards):
                text, source = collect_form_text(typed, point.cards[card_i], point.card_sources[card_i])
                point.cards[card_i] = text
                point.card_sources[card_i] = source
            still = self._still_paths[index].strip()
            if not still:
                point.still_path = ""
                point.still_source = "empty"
            elif still != point.still_path.strip():
                point.still_path = still
                point.still_source = "user"
            else:
                point.still_path = still
                if point.still_source == "empty":
                    point.still_source = "user"
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

    def _import_markdown(self, text: str) -> None:
        current = self.collect_sheet()
        imported = parse_talk_sheet_markdown(text, base=current)
        self.load_sheet(imported)
        if self._on_change:
            self._on_change()

    def _browse_still(self, index: int) -> None:
        from tkinter import filedialog

        path = filedialog.askopenfilename(title=f"Point {index + 1} still", filetypes=IMAGE_TYPES)
        if not path:
            return
        self._still_paths[index] = path
        self._set_preview(index, path)
        if self._on_change:
            self._on_change()

    def _clear_still(self, index: int) -> None:
        self._still_paths[index] = ""
        self._set_preview(index, "")
        if self._on_change:
            self._on_change()

    def _set_preview(self, index: int, path: str) -> None:
        label = self._previews[index]
        if not path or not Path(path).is_file():
            self._preview_images[index] = None
            label.configure(image=None, text="No still")
            return
        try:
            from PIL import Image

            image = Image.open(path)
            image.thumbnail((160, 90))
            photo = ctk.CTkImage(light_image=image, dark_image=image, size=image.size)
            self._preview_images[index] = photo
            label.configure(image=photo, text="")
        except Exception:
            self._preview_images[index] = None
            label.configure(image=None, text=Path(path).name)


def empty_point() -> TalkPoint:
    return TalkPoint()
