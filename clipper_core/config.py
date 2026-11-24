"""Settings, environment variables and external tool discovery."""
import importlib.util
import os
import shutil
import sys
from dataclasses import asdict, dataclass, fields
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
JOBS_DIR = ROOT / "jobs"
DEFAULT_MODEL = "gemini-3.5-flash"
MAX_SOURCE_SECONDS = 30 * 3600  # refuse sources longer than this (24/7 stream VODs run past a day)
LONG_SOURCE_SECONDS = 90 * 60   # without captions, longer sources are transcribed only around their loud moments
MAX_UPLOAD_MB = 4000

ASPECTS = {"9:16": (9, 16), "1:1": (1, 1), "4:5": (4, 5), "16:9": (16, 9)}
QUALITY = {
    "high": {"label": "High (1080p, slower)", "short_side": 1080, "crf": 18, "preset": "medium"},
    "standard": {"label": "Standard (1080p)", "short_side": 1080, "crf": 21, "preset": "veryfast"},
    "small": {"label": "Small file (720p)", "short_side": 720, "crf": 23, "preset": "veryfast"},
}
FRAMING = {
    "smart": "Smart (follow faces, fall back to centre)",
    "center": "Centre crop",
    "fit": "Fit whole frame (blurred background)",
    "stack": "Stream layout (camera on top, gameplay below)",
}


class ClipperError(Exception):
    """An error whose message is safe and useful to show the user."""


class NotReady(ClipperError):
    """The source exists but can't be used yet (a live stream or an upcoming premiere); try again later."""


@dataclass
class Settings:
    num_clips: int = 3
    min_seconds: int = 30
    max_seconds: int = 60
    aspect: str = "9:16"
    framing: str = "smart"
    quality: str = "standard"
    subtitles: bool = True
    sub_font_size: int = 72          # in pixels at 1080 px output width
    sub_color: str = "#FFFFFF"
    sub_highlight: str = "#FFD400"
    sub_position: str = "lower"      # lower | middle | upper
    sub_background: str = "outline"  # outline | box | none
    sub_emphasis: str = "word"       # word | caps | none
    hook_overlay: bool = True        # burn the AI's on-screen hook text over the first seconds
    cam_box: str = ""                # "x,y,w,h" (0-1 fractions of the frame): where the facecam is, for "stack"
    game_box: str = ""               # "x,y,w,h": the gameplay area for "stack" (leaves out chat); empty = auto
    campaign: str = ""               # campaign profile id (campaigns/<id>.json); empty means none
    whisper_model: str = "base"
    model: str = ""                  # empty means GEMINI_MODEL or DEFAULT_MODEL

    @classmethod
    def from_dict(cls, data):
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in (data or {}).items() if k in known})

    def to_dict(self):
        return asdict(self)

    def gemini_model(self):
        return self.model.strip() or os.environ.get("GEMINI_MODEL", "").strip() or DEFAULT_MODEL

    def validate(self):
        if not 1 <= self.num_clips <= 20:
            raise ClipperError("Number of clips must be between 1 and 20.")
        if not 5 <= self.min_seconds < self.max_seconds <= 600:
            raise ClipperError("Clip duration range must satisfy 5 <= min < max <= 600 seconds.")
        if self.aspect not in ASPECTS:
            raise ClipperError(f"Unknown aspect ratio {self.aspect!r}.")
        if self.quality not in QUALITY:
            raise ClipperError(f"Unknown quality {self.quality!r}.")
        if self.framing not in FRAMING:
            raise ClipperError(f"Unknown framing {self.framing!r}.")
        if self.framing == "stack":
            parse_cam_box(self.cam_box)
        return self


def parse_cam_box(text):
    """'x,y,w,h' fractions -> a tuple, or a ClipperError saying how to set it."""
    try:
        x, y, w, h = (float(v) for v in str(text or "").split(","))
    except ValueError:
        raise ClipperError("The stream layout needs the camera position. Set it in the campaign "
                           "(Camera position) or pick another framing.") from None
    if not (0 <= x < 1 and 0 <= y < 1 and 0.02 <= w <= 1 - x + 1e-6 and 0.02 <= h <= 1 - y + 1e-6):
        raise ClipperError(f"The camera position {text!r} is outside the frame.")
    return x, y, w, h


def output_size(aspect, quality):
    """Output width and height (even numbers) for an aspect ratio and quality preset."""
    a, b = ASPECTS[aspect]
    short = QUALITY[quality]["short_side"]
    w, h = (short, short * b / a) if a <= b else (short * a / b, short)
    return int(round(w / 2) * 2), int(round(h / 2) * 2)


def load_env():
    """Read .env next to the app, then the Windows user environment, without overriding real env vars."""
    env_file = ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
    # terminals opened before `setx` don't see the new value; read it from the registry instead
    if not os.environ.get("GEMINI_API_KEY") and sys.platform == "win32":
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k:
                os.environ["GEMINI_API_KEY"] = winreg.QueryValueEx(k, "GEMINI_API_KEY")[0]
        except OSError:
            pass


def gemini_key():
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not key:
        raise ClipperError(
            "GEMINI_API_KEY is not set. Get a free key at https://aistudio.google.com/apikey, "
            "then put it in a .env file (see .env.example) or run: setx GEMINI_API_KEY \"your-key\""
        )
    return key


def _add_winget_links():
    """winget installs (ffmpeg, deno) land here; old terminals don't have it on PATH yet."""
    links = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WinGet" / "Links"
    if links.is_dir() and str(links) not in os.environ.get("PATH", ""):
        os.environ["PATH"] = str(links) + os.pathsep + os.environ.get("PATH", "")


def find_tool(name):
    """Path to ffmpeg/ffprobe, or a ClipperError explaining how to install it."""
    _add_winget_links()
    found = shutil.which(name)
    if found:
        return found
    packages = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WinGet" / "Packages"
    for exe in packages.glob(f"Gyan.FFmpeg*/*/bin/{name}.exe"):
        # yt-dlp's section downloader only looks on PATH, so add it there too
        os.environ["PATH"] = str(exe.parent) + os.pathsep + os.environ["PATH"]
        return str(exe)
    raise ClipperError(f"{name} was not found. Install FFmpeg with: winget install Gyan.FFmpeg")


def check_dependencies():
    """Name -> (ok, detail) for the startup check. Optional items don't block processing."""
    load_env()
    status = {}
    for tool in ("ffmpeg", "ffprobe"):
        try:
            status[tool] = (True, find_tool(tool))
        except ClipperError as e:
            status[tool] = (False, str(e))
    for label, module, hint in [
        ("yt-dlp", "yt_dlp", "pip install yt-dlp"),
        ("google-genai", "google.genai", "pip install google-genai"),
        ("faster-whisper (optional, for videos without captions)", "faster_whisper", "pip install faster-whisper"),
        ("opencv (optional, smart framing)", "cv2", "pip install opencv-python"),
    ]:
        try:
            ok = importlib.util.find_spec(module) is not None
        except ModuleNotFoundError:
            ok = False
        status[label] = (ok, "installed" if ok else f"missing: {hint}")
    status["deno (optional, more reliable YouTube)"] = (
        bool(shutil.which("deno")), "installed" if shutil.which("deno") else "missing: winget install DenoLand.Deno"
    )
    has_key = bool(os.environ.get("GEMINI_API_KEY", "").strip())
    status["GEMINI_API_KEY"] = (has_key, "set" if has_key else "not set (see .env.example)")
    return status
