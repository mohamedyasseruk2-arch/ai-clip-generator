"""Where videos come from: public links (via yt-dlp) and local files (via ffprobe).

Any site yt-dlp can read works (TikTok, Instagram, Facebook, X, Twitch, Vimeo, Reddit, direct .mp4 links and
~1800 more). YouTube links get an extra check so truncated addresses fail with a clear message.
"""
import json
import re
import subprocess
import time
from urllib.parse import parse_qs, urlparse

from .config import MAX_SOURCE_SECONDS, ClipperError, NotReady, find_tool

YOUTUBE_HOSTS = ("youtube.com", "youtu.be", "music.youtube.com")
# best H.264 up to 1080p (decodes everywhere), then anything up to 1080p
FORMAT = "bv*[height<=1080][vcodec^=avc1]+ba[ext=m4a]/bv*[height<=1080]+ba/b[height<=1080]/b"
RETRIES = 3


def validate_url(url):
    url = (url or "").strip()
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ClipperError("That doesn't look like a link. Paste a full address starting with https://")
    host = host_of(url)
    if host in YOUTUBE_HOSTS and not _is_youtube_video(host, parsed):
        raise ClipperError("That isn't a link to a single YouTube video (it may be cut off). "
                           "Copy the full address, e.g. https://www.youtube.com/watch?v=...")
    return url


def host_of(url):
    """'www.m.youtube.com:443' -> 'youtube.com'."""
    host = urlparse((url or "").strip()).netloc.lower().split(":")[0]
    host = host[4:] if host.startswith("www.") else host
    return host[2:] if host.startswith("m.") else host


def is_youtube(url):
    return host_of(url) in YOUTUBE_HOSTS


def _is_youtube_video(host, parsed):
    """Catches truncated links like youtube.com/wat, which yt-dlp would treat as a channel name."""
    if host == "youtu.be":
        return re.fullmatch(r"/[\w-]+/?", parsed.path)
    if parsed.path.rstrip("/") == "/watch":
        return parse_qs(parsed.query).get("v", [""])[0]
    return re.fullmatch(r"/(shorts|live|embed)/[\w-]+/?", parsed.path)


def _ydl(extra=None):
    import yt_dlp

    find_tool("ffmpeg")  # puts ffmpeg (and deno via winget links) on PATH for yt-dlp
    # ejs:github lets yt-dlp fetch YouTube's JS challenge solver; without it some videos report "not available"
    opts = {"quiet": True, "no_warnings": True, "noplaylist": True, "socket_timeout": 30,
            "remote_components": ["ejs:github"]}
    opts.update(extra or {})
    return yt_dlp.YoutubeDL(opts)


def _friendly_download_error(e):
    msg = re.sub(r"\x1b\[[0-9;]*m", "", str(e)).replace("ERROR: ", "")
    lower = msg.lower()
    if "unsupported url" in lower:
        return ("The downloader can't read videos from that site or page. Check it's a link to a single public "
                "video, or download the video and use Upload file instead.")
    if "404" in lower:
        return "The site says that page doesn't exist. Check the link is complete."
    if "private" in lower:
        return "This video is private."
    if re.search(r"sign in|logged-in|log in|login|cookies|\bage\b|age-restrict", lower):
        return "This video needs a signed-in account (age-restricted or members-only), so it can't be used."
    if "unavailable" in lower or "not available" in lower or "removed" in lower:
        return "This video is unavailable (deleted, region-locked or blocked for this app)."
    if "drm" in lower:
        return "This video is DRM-protected and can't be processed."
    return f"Couldn't read that link: {msg[:300]}"


def _with_retries(fn, what):
    from yt_dlp.utils import DownloadError

    for attempt in range(1, RETRIES + 1):
        try:
            return fn()
        except DownloadError as e:
            text = str(e).lower()
            if "will begin" in text or "premieres in" in text:
                raise NotReady("This video is an upcoming premiere or live event.") from e
            permanent = any(w in text for w in ("private", "sign in", "unavailable", "not available", "removed", "drm",
                                                "unsupported", "404"))
            if permanent or attempt == RETRIES:
                raise ClipperError(_friendly_download_error(e)) from e
            time.sleep(2 * attempt)
    raise ClipperError(f"{what} failed.")


def probe_url(url):
    """Metadata for a link, without downloading the video."""
    url = validate_url(url)

    def run():
        with _ydl() as ydl:
            return ydl.extract_info(url, download=False)

    info = _with_retries(run, "Reading the link")
    if info.get("_type") == "playlist":
        raise ClipperError("That's a playlist. Paste a link to a single video.")
    if info.get("live_status") == "is_upcoming":
        raise NotReady("This stream hasn't started yet.")
    if info.get("is_live"):
        if not is_recording_vod(url, info):
            raise NotReady("Live streams can't be clipped until they've finished. Use the stream's VOD link "
                           "(twitch.tv/videos/...) to clip the part that's already happened.")
        info["_in_progress"] = True  # a VOD of a stream that's still going: use what's recorded so far
    duration = info.get("duration") or 0
    if duration > MAX_SOURCE_SECONDS:
        raise ClipperError(f"This video is {duration / 3600:.1f} hours long; the limit is {MAX_SOURCE_SECONDS / 3600:.0f} hours.")
    return info


def is_recording_vod(url, info):
    """A Twitch VOD whose stream is still live: the recorded part can be clipped, it just keeps growing."""
    return (info.get("extractor_key") == "TwitchVod" or "/videos/" in url) and bool(info.get("duration"))


def probe_meta(url):
    """Views, upload time and subscriber count without resolving formats (about twice as fast as probe_url)."""
    def run():
        with _ydl() as ydl:
            return ydl.extract_info(url, download=False, process=False)

    return _with_retries(run, "Reading the video")


def fetch_comments(url, limit=200):
    """The top `limit` comments (text and like counts) of a video."""
    opts = {"skip_download": True, "getcomments": True,
            "extractor_args": {"youtube": {"max_comments": [str(limit), "all", "0", "0"], "comment_sort": ["top"]}}}

    def run():
        with _ydl(opts) as ydl:
            return ydl.extract_info(validate_url(url), download=False)

    return _with_retries(run, "Reading comments").get("comments") or []


def search(url):
    """Flat results of a YouTube search page: id, title, channel, views and duration, with no per-video requests."""
    def run():
        with _ydl({"extract_flat": True, "noplaylist": False, "playlistend": 20}) as ydl:
            return ydl.extract_info(url, download=False)

    entries = _with_retries(run, "Searching YouTube").get("entries") or []
    return [{"id": e["id"], "url": f"https://www.youtube.com/watch?v={e['id']}", "title": e.get("title") or "",
             "channel": e.get("channel") or "", "channel_url": e.get("channel_url") or "",
             "views": e.get("view_count") or 0, "duration": e.get("duration") or 0}
            for e in entries if e.get("id") and e.get("ie_key", "Youtube") == "Youtube"]


def source_from_info(url, info):
    return {
        "type": "url",
        "url": url,
        "extractor": info.get("extractor_key", "web"),
        "video_id": info.get("id"),
        "title": info.get("title", ""),
        "duration": float(info.get("duration") or 0),
        "channel": info.get("channel") or info.get("uploader") or "",
        "published": published_date(info),
        "in_progress": bool(info.get("_in_progress")),
    }


def published_date(info):
    """'YYYY-MM-DD' the video or stream went up (UTC), or "" if the site doesn't say."""
    if info.get("timestamp"):
        return time.strftime("%Y-%m-%d", time.gmtime(info["timestamp"]))
    d = str(info.get("upload_date") or "")
    return f"{d[:4]}-{d[4:6]}-{d[6:8]}" if re.fullmatch(r"\d{8}", d) else ""


# ---------- captions ----------

def pick_caption_track(info):
    """(lang, url) of the best json3 caption track: human subtitles first, then auto captions in the spoken language."""
    for kind in ("subtitles", "automatic_captions"):
        tracks = {k: v for k, v in (info.get(kind) or {}).items() if k != "live_chat"}
        if not tracks:
            continue
        lang = next((l for l in tracks if l.endswith("-orig")), None)
        lang = lang or next((l for l in (info.get("language"), "en") if l and l in tracks), None)
        if not lang and kind == "automatic_captions":
            continue  # auto captions in a random translated language are worse than Whisper
        lang = lang or next(iter(tracks))
        fmt = next((f for f in tracks[lang] if f.get("ext") == "json3"), None)
        if fmt:
            return lang, fmt["url"]
    return None, None


def fetch_captions(info):
    """Word-level transcript from YouTube captions, or None if the video has no usable captions."""
    lang, url = pick_caption_track(info)
    if not url:
        return None

    def run():
        with _ydl() as ydl:
            return json.loads(ydl.urlopen(url).read())

    data = _with_retries(run, "Downloading captions")
    words = parse_json3(data)
    if len(words) < 20:
        return None
    return {"source": f"captions:{lang}", "language": lang.replace("-orig", ""), "words": words}


def parse_json3(data):
    """YouTube json3 captions -> [{start, end, text}] words, sorted and non-overlapping."""
    words = []
    for e in data.get("events", []):
        segs = e.get("segs")
        if not segs or "tStartMs" not in e:
            continue
        t0 = e["tStartMs"] / 1000
        t_end = t0 + e.get("dDurationMs", 0) / 1000
        pieces = [(t0 + s.get("tOffsetMs", 0) / 1000, s.get("utf8", "")) for s in segs]
        for i, (start, text) in enumerate(pieces):
            text = text.replace("\n", " ").strip()
            if not text or text.startswith("[") and text.endswith("]"):  # skip [Music], [Applause]
                continue
            end = pieces[i + 1][0] if i + 1 < len(pieces) else t_end
            # human subtitles put a whole line in one piece: spread its words across the time
            parts = [p for p in text.split() if p not in (">>", "&gt;&gt;")]  # YouTube's speaker-change marks
            if not parts:
                continue
            step = max(end - start, 0.01) / len(parts)
            for j, w in enumerate(parts):
                words.append({"start": round(start + j * step, 3), "end": round(start + (j + 1) * step, 3), "text": w})
    words.sort(key=lambda w: w["start"])
    for a, b in zip(words, words[1:]):
        if a["end"] > b["start"] >= a["start"]:
            a["end"] = b["start"]  # auto captions overlap on screen; words shouldn't
    return [w for w in words if w["end"] > w["start"]]


def segments_from_words(words, max_words=20, max_gap=1.2):
    """Group words into sentence-like segments for the AI prompt and timestamp snapping."""
    segments, current = [], []
    for w in words:
        if current and (w["start"] - current[-1]["end"] > max_gap or len(current) >= max_words):
            segments.append(current)
            current = []
        current.append(w)
        if w["text"][-1:] in ".?!":
            segments.append(current)
            current = []
    if current:
        segments.append(current)
    return [
        {"start": s[0]["start"], "end": s[-1]["end"], "text": " ".join(w["text"] for w in s)}
        for s in segments
    ]


# ---------- media for whisper and rendering ----------

def download_audio(url, dest_dir, progress=None, until=None):
    """Audio only (much smaller than the video), for Whisper when there are no captions.

    `until` (seconds, set for a VOD that's still recording): download the part recorded so far as a normal VOD.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    existing = [p for p in dest_dir.glob("audio.*") if not p.name.endswith((".part", ".ytdl"))]
    if existing:
        return existing[0]

    def hook(d):
        total = d.get("total_bytes") or d.get("total_bytes_estimate")
        if progress and d.get("status") == "downloading":
            if total:
                progress(min(d["downloaded_bytes"] / total, 1.0))
            elif d.get("fragment_count"):  # HLS streams (Twitch) report fragments, not bytes
                progress(min((d.get("fragment_index") or 0) / d["fragment_count"], 1.0))

    # 8 fragments at a time: a 10-hour Twitch VOD's audio (~750 MB) takes about 2 minutes instead of 20+
    opts = {"format": "ba[ext=m4a]/ba/b", "outtmpl": str(dest_dir / "audio.%(ext)s"), "progress_hooks": [hook],
            "noprogress": True, "concurrent_fragment_downloads": 8}

    def run():
        with _ydl(opts) as ydl:
            if not until:
                return ydl.extract_info(url)
            # a VOD that's still recording is flagged live, which makes the downloader follow it in real time
            # (forever); download the fragments recorded so far as an ordinary VOD instead
            info = ydl.extract_info(url, download=False)
            info.update(is_live=False, live_status="was_live")
            return ydl.process_ie_result(info, download=True)

    info = _with_retries(run, "Downloading audio")
    expected = dest_dir / f"audio.{info['ext']}"
    if expected.exists():
        return expected
    saved = [p for p in dest_dir.glob("audio.*") if not p.name.endswith((".part", ".ytdl"))]
    if not saved:
        raise ClipperError("The audio download finished but no file was saved.")
    return saved[0]


def download_video(url, dest_dir, progress=None):
    """The whole video as a local mp4, downloaded once and reused for every render.

    YouTube refuses (403) stream URLs opened directly by ffmpeg, so yt-dlp fetches the file itself.
    """
    dest = dest_dir / "video.mp4"
    if dest.exists():
        return dest
    dest_dir.mkdir(parents=True, exist_ok=True)

    def hook(d):
        total = d.get("total_bytes") or d.get("total_bytes_estimate")
        if progress and d.get("status") == "downloading" and total:
            progress(min(d["downloaded_bytes"] / total, 1.0))

    opts = {"format": FORMAT, "merge_output_format": "mp4", "outtmpl": str(dest_dir / "video.%(ext)s"),
            "progress_hooks": [hook], "noprogress": True}

    def run():
        with _ydl(opts) as ydl:
            return ydl.extract_info(validate_url(url))

    _with_retries(run, "Downloading the video")
    if not dest.exists():
        raise ClipperError("The video download finished but no file was saved.")
    return dest


def download_section(url, start, end, dest):
    """Just [start, end] of a video (a few MB instead of the whole file). Timing is exact, so subtitles stay in sync."""
    from yt_dlp.utils import download_range_func

    if dest.exists():
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    opts = {"format": FORMAT, "merge_output_format": "mp4", "outtmpl": str(dest.with_suffix(".%(ext)s")),
            "download_ranges": download_range_func(None, [(start, end)]), "noprogress": True}

    def run():
        with _ydl(opts) as ydl:
            return ydl.extract_info(validate_url(url))

    _with_retries(run, "Downloading the clip")
    if not dest.exists():
        raise ClipperError("The clip download finished but no file was saved.")
    return dest


def probe_file(path):
    """Duration, size and audio presence of a local file via ffprobe."""
    cmd = [find_tool("ffprobe"), "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=60, check=True).stdout
    except subprocess.CalledProcessError as e:
        raise ClipperError(f"That file isn't a video FFmpeg can read ({e.stderr.strip()[:200]}).") from e
    data = json.loads(out)
    video = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), None)
    if not video:
        raise ClipperError("That file has no video track.")
    width, height = int(video["width"]), int(video["height"])
    rotation = abs(int(video.get("tags", {}).get("rotate", 0) or 0))
    for sd in video.get("side_data_list", []):
        rotation = abs(int(sd.get("rotation", rotation) or rotation))
    if rotation in (90, 270):  # phone videos are often stored sideways
        width, height = height, width
    duration = float(data.get("format", {}).get("duration") or video.get("duration") or 0)
    if duration <= 0:
        raise ClipperError("Couldn't read the video's duration.")
    if duration > MAX_SOURCE_SECONDS:
        raise ClipperError(f"This video is {duration / 3600:.1f} hours long; the limit is {MAX_SOURCE_SECONDS / 3600:.0f} hours.")
    return {
        "inputs": [(str(path), {})],
        "width": width,
        "height": height,
        "has_audio": any(s.get("codec_type") == "audio" for s in data.get("streams", [])),
        "duration": duration,
    }
