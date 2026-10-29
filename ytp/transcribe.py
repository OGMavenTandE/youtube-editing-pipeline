"""Local transcription. faster-whisper is optional and never calls a cloud API."""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from ytp.ff import run_ffmpeg


@dataclass
class Word:
    text: str
    start: float
    end: float
    confidence: float | None = None


@dataclass
class Transcript:
    text: str
    words: list[Word] = field(default_factory=list)
    language: str = ""
    # False for a bracketed line transcript. Splice and articulation need real word times.
    word_timestamps: bool = True
    segments: list[tuple[float, float, str]] = field(default_factory=list)

    def shifted(self, offset: float) -> Transcript:
        if not offset:
            return self
        return Transcript(
            text=self.text,
            language=self.language,
            word_timestamps=self.word_timestamps,
            segments=list(self.segments),
            words=[
                Word(word.text, word.start + offset, word.end + offset, word.confidence)
                for word in self.words
            ],
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


# One temperature, so a failed segment cannot step up to 0.2, 0.4, ...
_WHISPER_TEMPERATURE = (0.0,)
_WHISPER_BEAM_SIZE = 5
_WHISPER_VAD = {
    "threshold": 0.5,
    "neg_threshold": None,
    "min_speech_duration_ms": 0,
    "max_speech_duration_s": float("inf"),
    "min_silence_duration_ms": 2000,
    "speech_pad_ms": 400,
}


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

    def __init__(
        self,
        model: str = "base",
        device: str = "auto",
        language: str = "en",
    ) -> None:
        self.model_name = model
        self.device_request = device
        self.forced_language = language or "en"
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
        # Always decode through ffmpeg. PyAV's read of the render's AAC
        # track is not the same input twice, and it moved pace and captions.
        handle, name = tempfile.mkstemp(suffix=".wav", prefix="ytp-whisper-")
        os.close(handle)
        temporary = Path(name)
        offset = float(start or 0.0)
        words: list[Word] = []
        pieces: list[str] = []
        try:
            extract_audio(media, temporary, start=start, end=end)
            segments, info = model.transcribe(
                str(temporary),
                word_timestamps=True,
                language=self.forced_language,
                task="transcribe",
                beam_size=_WHISPER_BEAM_SIZE,
                best_of=1,
                patience=1.0,
                length_penalty=1.0,
                repetition_penalty=1.0,
                no_repeat_ngram_size=0,
                temperature=_WHISPER_TEMPERATURE,
                compression_ratio_threshold=2.4,
                log_prob_threshold=-1.0,
                no_speech_threshold=0.6,
                condition_on_previous_text=False,
                prompt_reset_on_temperature=0.5,
                suppress_blank=True,
                without_timestamps=False,
                vad_filter=False,
                vad_parameters=dict(_WHISPER_VAD),
            )
            for segment in segments:
                pieces.append(segment.text.strip())
                segment_words = list(segment.words or [])
                if segment_words:
                    for token in segment_words:
                        text = token.word.strip()
                        if text:
                            probability = getattr(token, "probability", None)
                            confidence = None if probability is None else float(probability)
                            words.append(
                                Word(text, float(token.start), float(token.end), confidence)
                            )
                elif segment.text.strip():
                    words.append(
                        Word(segment.text.strip(), float(segment.start), float(segment.end))
                    )
            language = getattr(info, "language", "") or ""
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        text = " ".join(piece for piece in pieces if piece)
        transcript = Transcript(text=text, words=words, language=language)
        return transcript.shifted(offset)


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
