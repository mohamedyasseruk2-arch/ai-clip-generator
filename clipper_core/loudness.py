"""Find the loud moments of a long stream (laughing, yelling, hype) from its audio level, in a few minutes.

Transcribing a 10-hour VOD on a laptop CPU takes hours. Funny and memorable moments are almost always louder than
the stream around them, so long sources are transcribed only in windows around these peaks.
"""
import subprocess

from .config import ClipperError, find_tool

SILENCE_DB = -70.0
BLOCK_SECONDS = 300      # the "usual" loudness is the median of each 5-minute block
SMOOTH_SECONDS = 3       # a peak must last a few seconds, not one click or bang
WINDOWS_PER_HOUR = 4
MIN_WINDOWS, MAX_WINDOWS = 4, 80
BEFORE, AFTER = 90, 45   # seconds of context kept before and after each peak (setup matters more than aftermath)
MIN_GAP = 240            # peaks closer than this belong to the same moment


def levels(path, duration=None, progress=None):
    """Loudness in dB for every second of the file's audio (one value per second)."""
    cmd = [find_tool("ffmpeg"), "-v", "error", "-nostdin", "-i", str(path), "-vn", "-ac", "1", "-af",
           "aresample=8000,asetnsamples=n=8000:p=0,astats=metadata=1:reset=1,"
           "ametadata=print:key=lavfi.astats.Overall.RMS_level:file=-", "-f", "null", "-"]
    out = []
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, errors="replace")
    for line in proc.stdout:
        if line.startswith("lavfi.astats.Overall.RMS_level="):
            value = line.strip().split("=", 1)[1]
            try:
                db = float(value)
            except ValueError:  # "-inf" for digital silence
                db = SILENCE_DB
            out.append(max(db, SILENCE_DB))
            if progress and duration and len(out) % 600 == 0:
                progress(min(len(out) / duration, 1.0))
    err = proc.stderr.read()
    proc.wait()
    if proc.returncode != 0 or not out:
        raise ClipperError(f"Couldn't read the audio levels: {err.strip()[-300:] or 'no audio found'}")
    return out


def _median(values):
    s = sorted(values)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


def excitement(db):
    """Per second: how much louder than that part of the stream's usual level (smoothed)."""
    n = len(db)
    baseline = []
    for start in range(0, n, BLOCK_SECONDS):
        block = [v for v in db[start:start + BLOCK_SECONDS] if v > SILENCE_DB]
        baseline.append(_median(block) if block else SILENCE_DB)
    half = SMOOTH_SECONDS // 2
    out = []
    for i in range(n):
        lo, hi = max(0, i - half), min(n, i + half + 1)
        smooth = sum(db[lo:hi]) / (hi - lo)
        out.append(smooth - baseline[i // BLOCK_SECONDS])
    return out


def loud_windows(db, per_hour=WINDOWS_PER_HOUR, before=BEFORE, after=AFTER, min_gap=MIN_GAP):
    """[(start, end), ...] in seconds around the stream's biggest loudness peaks, in time order."""
    n = len(db)
    if not n:
        return []
    want = max(MIN_WINDOWS, min(MAX_WINDOWS, round(n / 3600 * per_hour)))
    score = excitement(db)
    peaks = []
    for t in sorted(range(n), key=lambda i: -score[i]):
        if score[t] <= 0:
            break
        if all(abs(t - p) >= min_gap for p in peaks):
            peaks.append(t)
            if len(peaks) >= want:
                break
    windows = []
    for p in sorted(peaks):
        start, end = max(0, p - before), min(n, p + after)
        if windows and start <= windows[-1][1]:
            windows[-1] = (windows[-1][0], max(windows[-1][1], end))
        else:
            windows.append((start, end))
    return [(float(s), float(e)) for s, e in windows]
