"""Local transcription. faster-whisper is optional and never calls a cloud API."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from ytp.ff import run_ffmpeg


@dataclass
class Word:
    text: str
    start: float
    end: float


@dataclass
class Transcript:
    text: str
    words: list[Word] = field(default_factory=list)
    language: str = ""

    def shifted(self, offset: float) -> Transcript:
        if not offset:
            return self
        return Transcript(
            text=self.text,
            language=self.language,
            words=[Word(word.text, word.start + offset, word.end + offset) for word in self.words],
        )


class Transcriber:
    """Word timestamps for one media file.

    `start` and `end` are seconds into `media`. Times in the result stay
    relative to the file that was opened, and the caller adds any offset.
    """

    name = "base"

    def transcribe(
        self,
        media: Path,
        *,
        start: float | None = None,
        end: float | None = None,
    ) -> Transcript:
        raise NotImplementedError


class NullTranscriber(Transcriber):
    """Duration-only index. Used when Booth has not installed faster-whisper."""

    name = "none"

    def transcribe(
        self,
        media: Path,
        *,
        start: float | None = None,
        end: float | None = None,
    ) -> Transcript:
        return Transcript(text="", words=[])


def extract_audio(source: Path, dest: Path, *, start: float | None, end: float | None) -> None:
    """Write a mono 16 kHz wav of the requested window. Whisper and the splice check share this."""
    args = ["-y"]
    if start is not None:
        args += ["-ss", f"{start:.3f}"]
    args += ["-i", str(source)]
    if end is not None and start is not None:
        args += ["-t", f"{max(0.01, end - start):.3f}"]
    elif end is not None:
        args += ["-to", f"{end:.3f}"]
    args += ["-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(dest)]
    run_ffmpeg(args)


class FasterWhisperTranscriber(Transcriber):
    """Local CTranslate2 model. CUDA float16 on the RTX 4070 Super, int8 on CPU."""

    def __init__(self, model: str = "base", device: str = "auto") -> None:
        self.model_name = model
        self.device_request = device
        self._model = None
        self.name = model

    def _load(self):
        if self._model is not None:
            return self._model
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:
            raise RuntimeError(
                "faster-whisper is not installed. "
                "pip install -r requirements-ytp-whisper.txt"
            ) from exc
        device = self.device_request
        if device == "auto":
            device = _cuda_or_cpu()
        compute = "float16" if device == "cuda" else "int8"
        self._model = WhisperModel(self.model_name, device=device, compute_type=compute)
        return self._model

    def transcribe(
        self,
        media: Path,
        *,
        start: float | None = None,
        end: float | None = None,
    ) -> Transcript:
        model = self._load()
        segments, info = model.transcribe(str(media), word_timestamps=True)
        words: list[Word] = []
        pieces: list[str] = []
        for segment in segments:
            pieces.append(segment.text.strip())
            segment_words = list(segment.words or [])
            if segment_words:
                for token in segment_words:
                    text = token.word.strip()
                    if text:
                        words.append(Word(text, float(token.start), float(token.end)))
            elif segment.text.strip():
                words.append(
                    Word(segment.text.strip(), float(segment.start), float(segment.end))
                )
        language = getattr(info, "language", "") or ""
        text = " ".join(piece for piece in pieces if piece)
        return Transcript(text=text, words=words, language=language)


def _cuda_or_cpu() -> str:
    forced = os.environ.get("YTP_WHISPER_DEVICE", "").strip()
    if forced:
        return forced
    try:
        import ctranslate2
    except ImportError:
        return "cpu"
    try:
        if ctranslate2.get_cuda_device_count() > 0:
            return "cuda"
    except Exception:
        return "cpu"
    return "cpu"
