"""Local transcription. faster-whisper is optional and never calls a cloud API."""

from __future__ import annotations

import os
import re
import sys
import tempfile
import time
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
            segments=[(start + offset, end + offset, text) for start, end, text in self.segments],
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
# One CPU thread. Parallel beam search does not repeat the same words.
_CPU_THREADS = 1
_NUM_WORKERS = 1
_WHISPER_VAD = {
    "threshold": 0.5,
    "neg_threshold": None,
    "min_speech_duration_ms": 0,
    "max_speech_duration_s": float("inf"),
    "min_silence_duration_ms": 2000,
    "speech_pad_ms": 400,
}


# Windows wheels ship these under site-packages/nvidia/<name>/bin.
# nvidia.cublas.lib is not a module in those wheels.
_NVIDIA_DLL_PACKAGES = ("cublas", "cudnn", "cuda_nvrtc")


def nvidia_dll_dirs(search_paths: list[Path] | None = None) -> list[Path]:
    """bin folders that hold cublas64_12.dll and the matching cuDNN/NVRTC DLLs."""
    roots = list(search_paths) if search_paths is not None else [Path(entry) for entry in sys.path]
    found: list[Path] = []
    seen: set[Path] = set()
    for root in roots:
        nvidia = root / "nvidia"
        if not nvidia.is_dir():
            continue
        for name in _NVIDIA_DLL_PACKAGES:
            folder = (nvidia / name / "bin").resolve()
            if folder in seen or not folder.is_dir():
                continue
            seen.add(folder)
            found.append(folder)
    return found


def enable_nvidia_dlls(search_paths: list[Path] | None = None) -> list[str]:
    """Put CUDA DLL folders on PATH before ctranslate2 is imported.

    ``os.add_dll_directory`` alone does not let ctranslate2 load
    ``cublas64_12.dll``. Prepending the bin folders to ``PATH`` does.
    Windows only. The process environment is changed; the user PATH is not.
    """
    if sys.platform != "win32":
        return []
    folders = nvidia_dll_dirs(search_paths)
    if not folders:
        return []
    texts = [str(folder) for folder in folders]
    current = os.environ.get("PATH", "")
    prefix = os.pathsep.join(texts)
    if not current.startswith(prefix):
        os.environ["PATH"] = prefix + (os.pathsep + current if current else "")
    if hasattr(os, "add_dll_directory"):
        for folder in texts:
            try:
                os.add_dll_directory(folder)
            except OSError:
                continue
    return texts


def _segment_is_loop(segment: object) -> bool:
    """Drop a window segment the model marked as compressed or repetitive."""
    from ytp.splice import window_is_hallucination

    ratio = getattr(segment, "compression_ratio", None)
    if ratio is not None and float(ratio) > 2.4:
        return True
    heard: list[Word] = []
    for token in list(getattr(segment, "words", None) or []):
        text = str(getattr(token, "word", "")).strip()
        if text:
            heard.append(Word(text, float(token.start), float(token.end)))
    if not heard:
        text = str(getattr(segment, "text", "") or "").strip()
        if text:
            heard.append(Word(text, 0.0, 0.0))
    return window_is_hallucination(heard)


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


# Lines Whisper invents over music, noise, or silence (training-data captions).
# `ytp transcribe` drops a short segment that is only one of these.
STOCK_HALLUCINATIONS = frozenset(
    {
        "thank you",
        "thank you very much",
        "thanks",
        "thanks for watching",
        "thank you for watching",
        "thank you so much for watching",
        "please subscribe",
        "like and subscribe",
        "subscribe",
        "bye",
        "you",
        "well be right back",
        "ill see you next time",
        "see you next time",
        "see you in the next video",
        "thanks for listening",
        "subtitles by the amaraorg community",
        "transcription by castingwords",
    }
)
# A real sentence of thanks can be longer; a stock line over noise is short.
STOCK_MAX_SECONDS = 4.0
# Portable keys: faster-whisper 1.1 and 1.2 name the thresholds differently.
_QUICK_VAD = {"min_silence_duration_ms": 2000, "speech_pad_ms": 400}


def _normal_text(text: str) -> str:
    normal = re.sub(r"[^a-z0-9 ]+", "", text.lower().replace("-", " "))
    return " ".join(normal.split())


def repeated_runs(texts: list[str], min_run: int = 3) -> set[int]:
    """Indexes of segments in a run of ``min_run`` or more identical texts in a row.

    Whisper loops a line ("It's a good idea." x19) over music or engine noise.
    Real speech that repeats ("Go." between other calls) is not consecutive.
    """
    drop: set[int] = set()
    index = 0
    while index < len(texts):
        end = index + 1
        key = _normal_text(texts[index])
        while end < len(texts) and key and _normal_text(texts[end]) == key:
            end += 1
        if key and end - index >= min_run:
            drop.update(range(index, end))
        index = end
    return drop


def is_stock_hallucination(text: str, start: float, end: float) -> bool:
    """True for a short segment that is only a stock Whisper line ("Thank you.")."""
    if end - start > STOCK_MAX_SECONDS:
        return False
    normal = _normal_text(text)
    return bool(normal) and normal in STOCK_HALLUCINATIONS


class FasterWhisperTranscriber(Transcriber):
    """Local CTranslate2 model. CUDA float16 on the RTX 4070 Super, int8 on CPU."""

    def __init__(
        self,
        model: str = "base",
        device: str = "auto",
        language: str = "en",
        window_model: str | None = None,
        *,
        quick: bool = False,
        vad: bool = False,
    ) -> None:
        # quick: `ytp transcribe`. Silence-hallucination skip plus the stock-line,
        # loop, and repeated-run filters. vad adds Silero VAD (quick mode only); it
        # drops speech under loud noise, so it is opt-in.
        self.quick = quick
        self.vad = vad
        self.compute_type = ""
        self.load_seconds = 0.0
        self.dropped_segments = 0
        self.model_name = model
        self.window_model_name = window_model or model
        self.device_request = device
        self.forced_language = language or "en"
        self._models: dict[str, object] = {}
        self.name = model

    def _load(self, model_name: str):
        cached = self._models.get(model_name)
        if cached is not None:
            return cached
        import importlib.util

        if importlib.util.find_spec("faster_whisper") is None:
            raise RuntimeError(
                "faster-whisper is not installed. pip install -r requirements-ytp-whisper.txt"
            )
        device = self.device_request
        if device == "auto":
            device = _cuda_or_cpu()
        compute = "float16" if device == "cuda" else "int8"
        started = time.monotonic()
        # Pin the pool before CTranslate2 reads it. cpu_threads=0 means "all cores".
        os.environ["OMP_NUM_THREADS"] = str(_CPU_THREADS)
        os.environ["CT2_VERBOSE"] = "0"
        os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
        # PATH first. Importing faster-whisper loads ctranslate2.
        enable_nvidia_dlls()
        from faster_whisper import WhisperModel

        model = WhisperModel(
            model_name,
            device=device,
            device_index=0,
            compute_type=compute,
            cpu_threads=_CPU_THREADS,
            num_workers=_NUM_WORKERS,
        )
        self.compute_type = compute
        self.load_seconds = time.monotonic() - started
        self._models[model_name] = model
        return model

    def load(self) -> None:
        """Load the whole-file model now, so its load time can be logged on its own."""
        self._load(self.model_name)

    def transcribe(
        self,
        media: Path,
        *,
        start: float | None = None,
        end: float | None = None,
    ) -> Transcript:
        window = start is not None or end is not None
        model_name = self.window_model_name if window else self.model_name
        model = self._load(model_name)
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
            kwargs = dict(
                word_timestamps=True,
                # "auto" lets Whisper detect the language (ytp transcribe --language auto).
                language=None if self.forced_language == "auto" else self.forced_language,
                task="transcribe",
                beam_size=_WHISPER_BEAM_SIZE,
                best_of=1,
                patience=1.0,
                length_penalty=1.0,
                repetition_penalty=1.0,
                no_repeat_ngram_size=3 if window else 0,
                temperature=_WHISPER_TEMPERATURE,
                compression_ratio_threshold=2.4,
                log_prob_threshold=-1.0,
                no_speech_threshold=0.6,
                condition_on_previous_text=False,
                prompt_reset_on_temperature=0.5,
                suppress_blank=True,
                without_timestamps=False,
                vad_filter=self.quick and self.vad and not window,
                vad_parameters=dict(_QUICK_VAD if self.quick else _WHISPER_VAD),
                hallucination_silence_threshold=2.0 if (window or self.quick) else None,
            )
            segments, info = model.transcribe(str(temporary), **kwargs)
            self.dropped_segments = 0
            repeats: set[int] = set()
            if self.quick:
                segments = list(segments)
                repeats = repeated_runs([str(segment.text or "") for segment in segments])
            for position, segment in enumerate(segments):
                if window and _segment_is_loop(segment):
                    continue
                if self.quick and (
                    position in repeats
                    or is_stock_hallucination(
                        str(segment.text or ""), float(segment.start), float(segment.end)
                    )
                    or _segment_is_loop(segment)
                ):
                    self.dropped_segments += 1
                    continue
                pieces.append((float(segment.start), float(segment.end), segment.text.strip()))
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
        text = " ".join(piece for _start, _end, piece in pieces if piece)
        transcript = Transcript(
            text=text,
            words=words,
            language=language,
            segments=[(start, end, piece) for start, end, piece in pieces if piece],
        )
        return transcript.shifted(offset)


CUDA_RUNTIME_DLLS = ("cublas64_12.dll", "cublasLt64_12.dll", "cudnn64_9.dll")
CUDA_RUNTIME_HINT = (
    "install the CUDA 12 runtime wheels into this venv: "
    'pip install -e ".[whisper]" (nvidia-cublas-cu12, nvidia-cudnn-cu12)'
)


def missing_cuda_runtime(loader=None) -> list[str]:
    """CUDA 12 DLLs ctranslate2 needs that this process can't load. Windows only.

    ``get_cuda_device_count`` only needs the driver, so a missing cuBLAS shows up
    later as a failed GPU run. Checking first lets `ytp` say why and what to install.
    """
    if sys.platform != "win32" and loader is None:
        return []
    enable_nvidia_dlls()
    if loader is None:
        import ctypes

        def loader(name: str) -> None:
            ctypes.WinDLL(name, winmode=0)  # standard search, PATH included

    missing = []
    for name in CUDA_RUNTIME_DLLS:
        try:
            loader(name)
        except OSError:
            missing.append(name)
    return missing


def _cuda_or_cpu() -> str:
    forced = os.environ.get("YTP_WHISPER_DEVICE", "").strip()
    if forced:
        return forced
    enable_nvidia_dlls()
    try:
        import ctranslate2
    except ImportError:
        return "cpu"
    try:
        if ctranslate2.get_cuda_device_count() <= 0:
            return "cpu"
    except Exception:
        return "cpu"
    missing = missing_cuda_runtime()
    if missing:
        print(
            f"warning: GPU found but {', '.join(missing)} can't be loaded, so Whisper runs "
            f"on CPU; {CUDA_RUNTIME_HINT}",
            file=sys.stderr,
        )
        return "cpu"
    return "cuda"
