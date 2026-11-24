"""
Free video clipping pipeline with Gemini picking the highlights.

Setup:
    pip install faster-whisper google-genai yt-dlp
    export GEMINI_API_KEY="your-key"      (Windows: setx GEMINI_API_KEY "your-key")

Usage:
    python clipper.py fast "https://youtu.be/..."  # quickest: YouTube captions, downloads only the clips
    python clipper.py auto video.mp4            # does everything
    python clipper.py auto "https://youtu.be/..."  # same, from a YouTube link
    python clipper.py download "https://youtu.be/..."  # just download the video
    python clipper.py transcribe video.mp4      # step 1 only
    python clipper.py pick video.transcript.txt # step 2 only -> clips.json
    python clipper.py clip video.mp4 clips.json # step 3 only
"""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent  # all output goes next to this script
MODEL = "gemini-3.5-flash"   # change here if Google renames or limits it
NUM_CLIPS = 3

PROMPT = """You are a short-form video editor. Below is a timestamped transcript.
Pick the {n} most engaging moments for TikTok/Reels/Shorts.

Rules:
- Each clip must be 30-60 seconds long.
- Start on a strong hook (a bold claim, question, or story beat).
- Start and end at natural sentence boundaries.
- Use only timestamps that appear in the transcript, in seconds.
- Return ONLY a JSON array like:
  [{{"start": 12.5, "end": 58.0, "reason": "why this works"}}]

Transcript:
{transcript}
"""


def find_ffmpeg():
    """ffmpeg on PATH, or the winget install (terminals opened before installing don't see it)."""
    found = shutil.which("ffmpeg")
    if found:
        return found
    winget = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WinGet" / "Packages"
    for exe in winget.glob("Gyan.FFmpeg*/*/bin/ffmpeg.exe"):
        # yt-dlp's partial downloader only looks on PATH, so add it there too
        os.environ["PATH"] = str(exe.parent) + os.pathsep + os.environ["PATH"]
        return str(exe)
    sys.exit("ffmpeg not found. Install it with: winget install Gyan.FFmpeg")


def load_gemini_key():
    """Fall back to the key saved with setx when this terminal predates it."""
    if os.environ.get("GEMINI_API_KEY") or sys.platform != "win32":
        return
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k:
            os.environ["GEMINI_API_KEY"] = winreg.QueryValueEx(k, "GEMINI_API_KEY")[0]
    except OSError:
        pass


FFMPEG = find_ffmpeg()


def download(url):
    import yt_dlp

    opts = {
        "format": "bv*[height<=1080]+ba/b[height<=1080]/b",
        "merge_output_format": "mp4",
        "ffmpeg_location": FFMPEG,
        "outtmpl": str(HERE / "%(id)s.%(ext)s"),  # video id keeps the filename safe
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url)
    video = info["requested_downloads"][0]["filepath"]
    print(f"Downloaded: {video}")
    return video


def captions(url):
    """Grab YouTube's own captions as a timestamped transcript, no video download."""
    import yt_dlp

    with yt_dlp.YoutubeDL({"quiet": True}) as ydl:
        info = ydl.extract_info(url, download=False)
        tracks = info.get("subtitles") or info.get("automatic_captions") or {}
        if not tracks:
            sys.exit("This video has no captions. Use: python clipper.py auto <link>")
        # prefer the spoken language over YouTube's machine translations
        lang = next((l for l in tracks if l.endswith("-orig")), None)
        lang = lang or next((l for l in (info.get("language"), "en") if l in tracks), None)
        lang = lang or next(iter(tracks))
        fmt = next(f for f in tracks[lang] if f["ext"] == "json3")
        data = json.loads(ydl.urlopen(fmt["url"]).read())

    out = HERE / (info["id"] + ".transcript.txt")
    with open(out, "w", encoding="utf-8") as f:
        for e in data.get("events", []):
            text = "".join(s.get("utf8", "") for s in e.get("segs", [])).replace("\n", " ").strip()
            if text:
                start = e["tStartMs"] / 1000
                f.write(f"[{start:.1f} - {start + e.get('dDurationMs', 0) / 1000:.1f}] {text}\n")
    print(f"Transcript ({lang}) saved: {out}")
    return str(out)


def clip_from_link(url, clips_file):
    """Download only the picked sections, then crop each to 9:16."""
    import yt_dlp
    from yt_dlp.utils import download_range_func

    clips = json.load(open(clips_file, encoding="utf-8"))
    (HERE / "clips").mkdir(exist_ok=True)
    for i, c in enumerate(clips, 1):
        raw = HERE / "clips" / f"raw_{i}.mp4"
        opts = {
            "format": "bv*[height<=1080]+ba/b[height<=1080]/b",
            "merge_output_format": "mp4",
            "ffmpeg_location": FFMPEG,
            "outtmpl": str(raw),
            "download_ranges": download_range_func(None, [(c["start"], c["end"])]),
            "force_keyframes_at_cuts": True,
            "overwrites": True,
        }
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])
        out = str(HERE / "clips" / f"clip_{i}.mp4")
        subprocess.run([
            FFMPEG, "-y", "-i", str(raw),
            "-vf", "crop=ih*9/16:ih,scale=1080:1920",  # center crop to 9:16
            "-c:v", "libx264", "-c:a", "aac",
            out,
        ], check=True)
        raw.unlink()
        print(f"Saved {out}")


def resolve(arg):
    """Download the video first if we were given a link instead of a file."""
    return download(arg) if arg.startswith(("http://", "https://")) else arg


def transcribe(video, model_size="small"):
    from faster_whisper import WhisperModel

    model = WhisperModel(model_size, compute_type="int8")
    segments, _ = model.transcribe(video)
    out = HERE / (Path(video).stem + ".transcript.txt")
    with open(out, "w", encoding="utf-8") as f:
        for s in segments:
            f.write(f"[{s.start:.1f} - {s.end:.1f}] {s.text.strip()}\n")
    print(f"Transcript saved: {out}")
    return str(out)


def pick(transcript_file, clips_file=HERE / "clips.json"):
    from google import genai
    from google.genai import types

    transcript = Path(transcript_file).read_text(encoding="utf-8")
    load_gemini_key()
    client = genai.Client()  # reads GEMINI_API_KEY from the environment
    resp = client.models.generate_content(
        model=MODEL,
        contents=PROMPT.format(n=NUM_CLIPS, transcript=transcript),
        config=types.GenerateContentConfig(response_mime_type="application/json"),
    )
    clips = json.loads(resp.text)
    # basic sanity check on what the model returned
    clips = [c for c in clips if 15 <= c["end"] - c["start"] <= 90]
    if not clips:
        sys.exit("Gemini returned no usable clips. Try running pick again.")
    Path(clips_file).write_text(json.dumps(clips, indent=2), encoding="utf-8")
    for c in clips:
        print(f'{c["start"]}-{c["end"]}: {c.get("reason", "")}')
    return clips_file


def clip(video, clips_file):
    clips = json.load(open(clips_file, encoding="utf-8"))
    (HERE / "clips").mkdir(exist_ok=True)
    for i, c in enumerate(clips, 1):
        out = str(HERE / "clips" / f"clip_{i}.mp4")
        cmd = [
            FFMPEG, "-y",
            "-ss", str(c["start"]), "-t", str(c["end"] - c["start"]),
            "-i", video,
            "-vf", "crop=ih*9/16:ih,scale=1080:1920",  # center crop to 9:16
            "-c:v", "libx264", "-c:a", "aac",
            out,
        ]
        subprocess.run(cmd, check=True)
        print(f"Saved {out}")


if __name__ == "__main__":
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    cmd, arg = sys.argv[1], sys.argv[2]
    if cmd == "auto":
        video = resolve(arg)
        t = transcribe(video)
        c = pick(t)
        clip(video, c)
    elif cmd == "fast":
        clip_from_link(arg, pick(captions(arg)))
    elif cmd == "download":
        download(arg)
    elif cmd == "transcribe":
        transcribe(resolve(arg))
    elif cmd == "pick":
        pick(arg)
    elif cmd == "clip" and len(sys.argv) > 3:
        clip(resolve(arg), sys.argv[3])
    else:
        sys.exit(__doc__)