"""One-page Streamlit review for an existing YouTube Studio paste folder.

Desk review only. Does not run silence trim, Gemini, slides, or MoviePy.
Launch with: streamlit run ui.py
"""

from __future__ import annotations

from pathlib import Path

from pipeline.config import Settings, load_settings
from pipeline.models import YouTubeMetadata
from pipeline.repack import list_studio_dirs, resolve_studio_run
from pipeline.studio import (
    clip_title,
    format_chapter_block,
    parse_chapter_block,
    parse_titles_file,
    strip_chapter_tail,
    write_studio_package,
)


def _label_for(path: Path) -> str:
    return path.name


def _titles_for_run(run_metadata: YouTubeMetadata, studio_dir: Path, stem: str) -> tuple[list[str], int]:
    titles = [title.strip() for title in run_metadata.titles if title.strip()][:5]
    if not titles:
        titles = [stem.replace("_", " ") or "Untitled"]
    titles_path = studio_dir / "titles.txt"
    selected = int(run_metadata.title_index or 0)
    if titles_path.is_file():
        file_titles, file_selected = parse_titles_file(titles_path.read_text(encoding="utf-8"))
        if file_titles:
            paste = file_titles[0]
            for index, title in enumerate(titles):
                if clip_title(title) == paste:
                    selected = index
                    break
            else:
                selected = file_selected
    if selected >= len(titles):
        selected = 0
    return titles, selected


def _thumbnail_choices(studio_dir: Path) -> list[Path]:
    names = ["thumbnail.jpg", "thumbnail_01.jpg", "thumbnail_02.jpg", "thumbnail_03.jpg"]
    found: list[Path] = []
    seen: set[Path] = set()
    for name in names:
        path = studio_dir / name
        if path.is_file() and path.resolve() not in seen:
            seen.add(path.resolve())
            found.append(path)
    return found


def render_review_page(settings: Settings | None = None) -> None:
    import streamlit as st

    settings = settings or load_settings()
    st.set_page_config(page_title="Studio review", layout="wide")
    st.title("Studio review")
    st.caption(
        "Pick a finished run, title, description, and chapters. "
        "This rewrites the Studio folder only. It is not the editor."
    )

    runs = list_studio_dirs(settings)
    if not runs:
        st.info(f"No `*_studio` folders in `{settings.output_dir}`. Run the pipeline first.")
        return

    labels = {_label_for(path): path for path in runs}
    choice = st.selectbox("Studio folder", list(labels.keys()))
    studio_dir = labels[choice]

    try:
        run = resolve_studio_run(studio_dir, settings)
    except FileNotFoundError as exc:
        st.error(str(exc))
        return

    titles, current_index = _titles_for_run(run.metadata, run.studio_dir, run.stem)
    selected_label = st.radio(
        "Title",
        options=titles,
        index=current_index,
    )
    title_index = titles.index(selected_label)

    media_left, media_right = st.columns(2)
    with media_left:
        thumbs = _thumbnail_choices(run.studio_dir)
        if thumbs:
            labels_thumbs = [path.name for path in thumbs]
            picked = st.radio("Thumbnail candidate", labels_thumbs, index=0)
            shown = run.studio_dir / picked
            if shown.is_file():
                st.image(shown.read_bytes(), caption=picked)
        else:
            st.warning("No thumbnail.jpg in this folder yet.")
    with media_right:
        if run.video_path.is_file():
            st.video(str(run.video_path))
        else:
            st.warning("Packaged MP4 is missing.")

    body = strip_chapter_tail(run.metadata.description or "")
    studio_desc = run.studio_dir / "description.txt"
    if not body and studio_desc.is_file():
        body = strip_chapter_tail(studio_desc.read_text(encoding="utf-8"))
    description = st.text_area("Description", value=body, height=220)
    chapter_text = format_chapter_block(run.metadata.chapters)
    chapters_raw = st.text_area("Chapters", value=chapter_text, height=160)

    if st.button("Rewrite studio folder", type="primary"):
        edited = run.metadata.model_copy(
            update={
                "description": description,
                "chapters": parse_chapter_block(chapters_raw) or run.metadata.chapters,
                "title_index": title_index,
            }
        )
        transcript_path = settings.output_dir / f"{run.stem}_transcript.json"
        try:
            package = write_studio_package(
                video_path=run.video_path,
                webcam_path=run.webcam_path,
                metadata=edited,
                dest_dir=run.studio_dir,
                settings=settings,
                fallback_title=run.stem.replace("_", " ").replace("-", " ").strip(),
                title_index=title_index,
                transcript_path=transcript_path if transcript_path.is_file() else None,
                metadata_path=run.metadata_path,
            )
        except (FileNotFoundError, ValueError, RuntimeError) as exc:
            st.error(str(exc))
            return
        st.success(f"Wrote {package.directory}")


def main() -> None:
    render_review_page()


def _launch() -> None:
    import subprocess
    import sys

    script = Path(__file__).resolve()
    raise SystemExit(
        subprocess.call(
            [sys.executable, "-m", "streamlit", "run", str(script), *sys.argv[1:]]
        )
    )


if __name__ == "__main__":
    try:
        from streamlit.runtime.scriptrunner import get_script_run_ctx
    except ImportError:
        raise SystemExit("streamlit is required. Install with: pip install -r requirements.txt")
    if get_script_run_ctx() is not None:
        main()
    else:
        _launch()
