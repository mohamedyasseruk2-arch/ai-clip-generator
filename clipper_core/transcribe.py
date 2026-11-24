"""Local speech-to-text with Faster-Whisper, used when a video has no usable captions."""
import os
import subprocess
import tempfile
from pathlib import Path

from .config import ClipperError, find_tool


def _model(model_size):
    try:
        from faster_whisper import WhisperModel
    except ImportError as e:
        raise ClipperError("This video has no captions and faster-whisper isn't installed: pip install faster-whisper") from e

    # "auto" picks CUDA whenever an NVIDIA GPU exists, and then the whole process dies if cuDNN/cuBLAS
    # aren't installed (not catchable from Python). CPU is the safe default; set WHISPER_DEVICE=cuda to opt in.
    device = os.environ.get("WHISPER_DEVICE", "").strip() or "cpu"
    return WhisperModel(model_size, device=device, compute_type="int8")


def _words(model, path, offset=0.0, progress=None):
    segments, info = model.transcribe(str(path), word_timestamps=True, vad_filter=True)
    words = []
    for s in segments:
        for w in s.words or []:
            if w.word.strip():
                words.append({"start": round(w.start + offset, 3), "end": round(w.end + offset, 3),
                              "text": w.word.strip()})
        if progress and info.duration:
            progress(min(s.end / info.duration, 1.0))
    return words, info.language


def whisper(media_path, model_size="base", progress=None):
    """Word-level transcript of an audio or video file."""
    words, language = _words(_model(model_size), media_path, progress=progress)
    if not words:
        raise ClipperError("No speech was found in this video, so there's nothing for the AI to pick from.")
    return {"source": f"whisper:{model_size}", "language": language, "words": words}


def whisper_windows(media_path, windows, model_size="base", progress=None):
    """Word-level transcript of only the given (start, end) windows of a long file, in source time."""
    if not windows:
        raise ClipperError("No loud moments were found to transcribe.")
    model = _model(model_size)
    ffmpeg = find_tool("ffmpeg")
    total = sum(e - s for s, e in windows)
    done, words, languages = 0.0, [], []
    with tempfile.TemporaryDirectory() as tmp:
        for n, (start, end) in enumerate(windows):
            piece = Path(tmp) / f"w{n}.wav"
            cmd = [ffmpeg, "-v", "error", "-nostdin", "-y", "-ss", f"{start:.2f}", "-t", f"{end - start:.2f}",
                   "-i", str(media_path), "-vn", "-ac", "1", "-ar", "16000", str(piece)]
            try:
                subprocess.run(cmd, capture_output=True, timeout=600, check=True)
            except (subprocess.SubprocessError, OSError):
                continue  # one unreadable window shouldn't sink the whole stream
            part, language = _words(model, piece, offset=start,
                                    progress=progress and (lambda f, s=start, e=end: progress((done + f * (e - s)) / total)))
            words += part
            languages.append(language)
            done += end - start
            if progress:
                progress(done / total)
    if not words:
        raise ClipperError("No speech was found around the stream's loud moments.")
    language = max(set(languages), key=languages.count)
    return {"source": f"whisper:{model_size} ({len(windows)} loud moments)", "language": language, "words": words,
            "windows": [[s, e] for s, e in windows]}
