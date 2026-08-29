"""One-page Streamlit review for an existing YouTube Studio paste folder.

Desk review only. Does not run silence trim, Gemini, slides, or MoviePy.
Launch with: streamlit run ui.py
"""

from __future__ import annotations

from pathlib import Path

from pipeline.config import Settings, load_settings
from pipeline.models import YouTubeMetadata
from pipeline.repack import list_studio_dirs, resolve_studio_run
from pipeline.studio import clip_title, parse_titles_file, write_studio_package


def _label_for(path: Path) -> str:
    return path.name


def _titles_for_run(run_metadata: YouTubeMetadata, studio_dir: Path, stem: str) -> tuple[list[str], int]:
    titles = [title.strip() for title in run_metadata.titles if title.strip()][:5]
    if not titles:
        titles = [stem.replace("_", " ") or "Untitled"]
    titles_path = studio_dir / "titles.txt"
    selected = 0
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


def render_review_page(settings: Settings | None = None) -> None:
    import streamlit as st

    settings = settings or load_settings()
    st.set_page_config(page_title="Studio review", layout="wide")
    st.title("Studio review")
    st.caption(
        "Pick a finished run and which of the five Gemini titles to paste. "
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
        thumb = run.studio_dir / "thumbnail.jpg"
        if thumb.is_file():
            st.image(thumb.read_bytes(), caption="Current thumbnail")
        else:
            st.warning("No thumbnail.jpg in this folder yet.")
    with media_right:
        if run.video_path.is_file():
            st.video(str(run.video_path))
        else:
            st.warning("Packaged MP4 is missing.")

    if st.button("Rewrite studio folder", type="primary"):
        try:
            package = write_studio_package(
                video_path=run.video_path,
                webcam_path=run.webcam_path,
                metadata=run.metadata,
                dest_dir=run.studio_dir,
                settings=settings,
                fallback_title=run.stem.replace("_", " ").replace("-", " ").strip(),
                title_index=title_index,
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
