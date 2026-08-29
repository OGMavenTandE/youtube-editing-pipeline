#!/usr/bin/env python3
"""CLI entry for the local YouTube editing pipeline."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from pipeline.config import FFmpegNotFoundError, Settings, load_settings, require_ffmpeg
from pipeline.gemini_director import GeminiConfigError, analyze_video, load_edit_script
from pipeline.media import MediaError, probe_duration, write_json
from pipeline.models import EditScript, SilenceTrimResult
from pipeline.pacing import enforce_pacing, evaluate_pacing
from pipeline.silence_remover import remove_silence


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="run.py",
        description=(
            "Local YouTube editing pipeline: silence trim, Gemini edit script, "
            "then MoviePy composite."
        ),
    )
    parser.add_argument(
        "--input",
        required=True,
        help="Path to the raw talking-head video (e.g. raw_video.mp4).",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Final MP4 path. Defaults to output/<stem>_final.mp4.",
    )
    parser.add_argument(
        "--skip-silence",
        action="store_true",
        help="Skip dead-air trimming and use the input as the working cut.",
    )
    parser.add_argument(
        "--skip-gemini",
        action="store_true",
        help="Skip Gemini and composite with an empty (or --edit-script) plan.",
    )
    parser.add_argument(
        "--edit-script",
        default=None,
        help="Load a previously saved edit-script JSON instead of calling Gemini.",
    )
    parser.add_argument(
        "--transcript",
        default=None,
        help="Optional text transcript to send alongside the audio.",
    )
    parser.add_argument(
        "--broll-dir",
        default=None,
        help="Directory of local B-roll files matched against cue queries.",
    )
    parser.add_argument(
        "--no-auto-editor",
        action="store_true",
        help="Force the pydub + ffmpeg silence backend even if auto-editor is installed.",
    )
    return parser


def resolve_input(raw: str, settings: Settings) -> Path:
    path = Path(raw)
    if path.is_file():
        return path.resolve()
    fallback = settings.input_dir / raw
    if fallback.is_file():
        return fallback.resolve()
    raise FileNotFoundError(
        f"Input video not found: {raw}\n"
        f"Looked at {path.resolve()} and {fallback.resolve()}."
    )


def run_pipeline(args: argparse.Namespace, settings: Settings) -> Path:
    settings.ensure_dirs()
    require_ffmpeg(settings)

    input_path = resolve_input(args.input, settings)
    output_path = (
        Path(args.output).resolve()
        if args.output
        else (settings.output_dir / f"{input_path.stem}_final.mp4").resolve()
    )

    if args.skip_silence:
        duration = probe_duration(input_path, settings)
        trim = SilenceTrimResult.passthrough(input_path, duration)
        print(f"[1/3] Silence trim skipped ({duration:.2f}s).")
    else:
        print("[1/3] Trimming silence / dead air...")
        trim = remove_silence(
            input_path,
            settings,
            prefer_auto_editor=not args.no_auto_editor,
        )
        print(
            f"      backend={trim.backend}  "
            f"{trim.cut_map.original_duration:.2f}s -> "
            f"{trim.cut_map.trimmed_duration:.2f}s  "
            f"kept {len(trim.cut_map.kept_ranges)} range(s)"
        )
        write_json(
            settings.output_dir / f"{input_path.stem}_cut_map.json",
            trim.cut_map.model_dump(),
        )

    if args.edit_script:
        script = load_edit_script(Path(args.edit_script))
        print(f"[2/3] Loaded edit script from {args.edit_script}.")
    elif args.skip_gemini:
        script = EditScript.empty()
        print("[2/3] Gemini skipped (empty edit script).")
    else:
        print(f"[2/3] Asking {settings.gemini_model} for an edit script...")
        transcript = None
        if args.transcript:
            transcript = Path(args.transcript).read_text(encoding="utf-8")
        script = analyze_video(
            trim.output_path,
            settings,
            transcript=transcript,
            duration=trim.cut_map.trimmed_duration,
        )
        print(
            f"      scenes={len(script.scenes)}  "
            f"lower-thirds={len(script.lower_thirds)}  "
            f"b-roll={len(script.broll)}  overlays={len(script.overlays)}"
        )

    script = enforce_pacing(script, trim.cut_map.trimmed_duration, settings)
    report = evaluate_pacing(script, trim.cut_map.trimmed_duration, settings)
    print(
        f"      pacing scenes={report.scene_count} "
        f"(band {report.expected_min_scenes}-{report.expected_max_scenes} "
        f"for {report.duration:.0f}s)  micro-resets={report.micro_event_count}"
    )
    for warning in report.warnings:
        print(f"      pacing note: {warning}")

    script_path = settings.output_dir / f"{input_path.stem}_edit_script.json"
    write_json(script_path, script.model_dump())
    meta_path = settings.output_dir / f"{input_path.stem}_youtube_metadata.json"
    write_json(meta_path, script.metadata.model_dump())

    print("[3/3] Compositing overlays and rendering...")
    from pipeline.compositor import render_video

    final_path = render_video(
        trim.output_path,
        script,
        output_path,
        settings,
        broll_dir=Path(args.broll_dir) if args.broll_dir else None,
    )
    print(f"Done. Video: {final_path}")
    print(f"      Edit script: {script_path}")
    print(f"      YouTube metadata: {meta_path}")
    return final_path


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    settings = load_settings()
    try:
        run_pipeline(args, settings)
    except FFmpegNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except GeminiConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 3
    except (FileNotFoundError, MediaError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
