"""Cutting, reframing and encoding clips with ffmpeg (one pass per clip, straight from the source)."""
import dataclasses
import hashlib
import json
import subprocess
import tempfile
import threading
import uuid
from pathlib import Path

from .config import ASPECTS, QUALITY, ClipperError, find_tool, output_size, parse_cam_box
from .subtitles import HOOK_SECONDS, build_ass, phrases

FACE_SAMPLES = 5


def compute_crop(src_w, src_h, aspect, center_x=0.5, center_y=0.45):
    """Largest crop of the target aspect inside the source, centred on (center_x, center_y) where possible."""
    a, b = ASPECTS[aspect]
    target = a / b
    if src_w / src_h > target:  # source is wider: crop the sides
        h = src_h - src_h % 2
        w = min(src_w, int(h * target) // 2 * 2)
    else:                       # source is taller: crop top and bottom
        w = src_w - src_w % 2
        h = min(src_h, int(w / target) // 2 * 2)
    x = int(min(max(center_x * src_w - w / 2, 0), src_w - w))
    y = int(min(max(center_y * src_h - h / 2, 0), src_h - h))
    return w, h, x, y


def _headers_args(headers):
    if not headers:
        return []
    return ["-headers", "".join(f"{k}: {v}\r\n" for k, v in headers.items())]


def detect_subject_x(media, start, end):
    """Horizontal centre (0-1) of the main face across a few frames, or None if unavailable."""
    try:
        import cv2
    except ImportError:
        return None
    if not hasattr(cv2, "CascadeClassifier"):
        return None  # OpenCV 5 dropped Haar cascades; fall back to a centre crop instead of failing the render
    src, headers = media["inputs"][0]
    start, end = start - media.get("offset", 0), end - media.get("offset", 0)
    cascade = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    centers = []
    with tempfile.TemporaryDirectory() as tmp:
        for i in range(FACE_SAMPLES):
            t = start + (end - start) * (i + 0.5) / FACE_SAMPLES
            frame = Path(tmp) / f"f{i}.jpg"
            cmd = [find_tool("ffmpeg"), "-v", "error", "-y", *_headers_args(headers), "-ss", f"{t:.2f}",
                   "-i", src, "-frames:v", "1", "-vf", "scale=640:-2", str(frame)]
            try:
                subprocess.run(cmd, capture_output=True, timeout=60, check=True)
            except (subprocess.SubprocessError, OSError):
                continue
            img = cv2.imread(str(frame))
            if img is None:
                continue
            gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
            faces = cascade.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=6, minSize=(40, 40))
            if len(faces):
                x, _, w, _ = max(faces, key=lambda f: f[2] * f[3])  # biggest face is usually the speaker
                centers.append((x + w / 2) / img.shape[1])
    if not centers:
        return None
    centers.sort()
    return centers[len(centers) // 2]


STACK_TOP = 0.40  # share of the height the facecam gets in the stream layout


def _fit_aspect(x, y, w, h, target):
    """Largest box of aspect `target` (w/h) inside (x, y, w, h), centred on it; whole even pixels."""
    if w / h > target:
        nw = h * target
        x, w = x + (w - nw) / 2, nw
    else:
        nh = w / target
        y, h = y + (h - nh) / 2, nh
    w, h = max(2, int(w) // 2 * 2), max(2, int(h) // 2 * 2)
    return w, h, int(x), int(y)


OVERLAY_EDGE_RATIO = 6.0  # the camera box's border is this many times sharper than the picture around it


def overlay_edge_score(gray, cam_box):
    """How sharp the camera box's inner edges (right and bottom, or left/top at the frame's far side) are compared
    with the picture next to them. Large when the facecam overlay is on screen, about 1 when it isn't."""
    import numpy as np

    H, W = gray.shape
    x, y, w, h = parse_cam_box(cam_box)
    x0, y0, x1, y1 = int(x * W), int(y * H), int(round((x + w) * W)), int(round((y + h) * H))
    g = gray.astype(float)
    scores = []
    for edge, inside in ((x1, True), (x0, False)):  # vertical edges that aren't the frame border
        if 3 < edge < W - 4:
            col = lambda c: np.abs(g[y0:y1, c] - g[y0:y1, c - 1]).mean()  # noqa: E731
            at = max(col(edge + d) for d in range(-6, 7) if 1 <= edge + d < W)
            near = np.mean([col(edge + d) for d in (-20, -15, -10, 10, 15, 20) if 1 <= edge + d < W]) + 0.5
            scores.append(at / near)
    for edge in (y1, y0):  # horizontal edges
        if 3 < edge < H - 4:
            row = lambda r: np.abs(g[r, x0:x1] - g[r - 1, x0:x1]).mean()  # noqa: E731
            at = max(row(edge + d) for d in range(-6, 7) if 1 <= edge + d < H)
            near = np.mean([row(edge + d) for d in (-20, -15, -10, 10, 15, 20) if 1 <= edge + d < H]) + 0.5
            scores.append(at / near)
    return min(scores) if scores else 0.0


def overlay_visible(media, start, end, cam_box, samples=FACE_SAMPLES):
    """True when the facecam box is on screen in most of the clip (gameplay with camera), False for scenes that are
    all camera or all game (IRL segments, cutscenes), None when frames can't be read."""
    try:
        import cv2
    except ImportError:
        return None
    src, headers = media["inputs"][0]
    start, end = start - media.get("offset", 0), end - media.get("offset", 0)
    votes = []
    with tempfile.TemporaryDirectory() as tmp:
        for i in range(samples):
            t = start + (end - start) * (i + 0.5) / samples
            frame = Path(tmp) / f"o{i}.png"
            cmd = [find_tool("ffmpeg"), "-v", "error", "-y", *_headers_args(headers), "-ss", f"{t:.2f}",
                   "-i", src, "-frames:v", "1", str(frame)]
            try:
                subprocess.run(cmd, capture_output=True, timeout=60, check=True)
            except (subprocess.SubprocessError, OSError):
                continue
            img = cv2.imread(str(frame), cv2.IMREAD_GRAYSCALE)
            if img is not None:
                votes.append(overlay_edge_score(img, cam_box) >= OVERLAY_EDGE_RATIO)
    if not votes:
        return None
    return sum(votes) * 2 > len(votes)


def extract_frame(path, t, out):
    """Save the frame at t seconds of a local video as an image file."""
    cmd = [find_tool("ffmpeg"), "-v", "error", "-y", "-ss", f"{t:.2f}", "-i", str(path), "-frames:v", "1", str(out)]
    try:
        subprocess.run(cmd, capture_output=True, timeout=60, check=True)
    except (subprocess.SubprocessError, OSError) as e:
        raise ClipperError(f"Couldn't read a frame from the video: {e}") from e
    return out


def stack_layout(src_w, src_h, cam_box, out_w, out_h, game_box=""):
    """Crops for the stream layout: (top_h, cam crop, gameplay crop), each crop as (w, h, x, y) in source pixels.

    game_box ("x,y,w,h" fractions) limits the gameplay to the part of the frame without chat or other overlays;
    by default the gameplay is the frame's centre, moved off the camera box.
    """
    cx, cy, cw, ch = parse_cam_box(cam_box)
    top_h = int(out_h * STACK_TOP) // 2 * 2
    bottom_h = out_h - top_h
    cam = _fit_aspect(cx * src_w, cy * src_h, cw * src_w, ch * src_h, out_w / top_h)
    if game_box:
        ax, ay, aw, ah = parse_cam_box(game_box)
        return top_h, cam, _fit_aspect(ax * src_w, ay * src_h, aw * src_w, ah * src_h, out_w / bottom_h)
    gw, gh, gx, gy = _fit_aspect(0, 0, src_w, src_h, out_w / bottom_h)
    # slide the gameplay crop sideways off the camera box when there's room, so no sliver of it shows twice
    box_l, box_r = cx * src_w, (cx + cw) * src_w
    if gx < box_r <= src_w - gw and box_l < gx + gw and cx < 0.5:
        gx = int(box_r + 1)
    elif box_l < gx + gw and gx < box_l and box_l - gw >= 0 and cx >= 0.5:
        gx = int(box_l - gw - 1)
    return top_h, cam, (gw, gh, gx, gy)


def build_filter(media, settings, out_w, out_h, center_x, subs_name):
    if settings.framing == "stack":
        top_h, (cw, ch, cx, cy), (gw, gh, gx, gy) = stack_layout(media["width"], media["height"], settings.cam_box,
                                                                  out_w, out_h, settings.game_box)
        chain = (
            f"[0:v]split[cam][game];"
            f"[cam]crop={cw}:{ch}:{cx}:{cy},scale={out_w}:{top_h}:flags=lanczos,setsar=1[top];"
            f"[game]crop={gw}:{gh}:{gx}:{gy},scale={out_w}:{out_h - top_h}:flags=lanczos,setsar=1[bottom];"
            f"[top][bottom]vstack,setsar=1"
        )
    elif settings.framing == "fit":
        chain = (
            f"[0:v]split[bg][fg];"
            f"[bg]scale={out_w}:{out_h}:force_original_aspect_ratio=increase,crop={out_w}:{out_h},boxblur=20:2[bgb];"
            f"[fg]scale={out_w}:{out_h}:force_original_aspect_ratio=decrease[fgs];"
            f"[bgb][fgs]overlay=(W-w)/2:(H-h)/2,setsar=1"
        )
    else:
        w, h, x, y = compute_crop(media["width"], media["height"], settings.aspect, center_x)
        chain = f"[0:v]crop={w}:{h}:{x}:{y},scale={out_w}:{out_h}:flags=lanczos,setsar=1"
    if subs_name:
        chain += f",subtitles={subs_name}"  # relative name; ffmpeg runs in the subtitle's folder
    return chain + "[v]"


def render_signature(candidate, settings):
    """Changes whenever anything affecting the rendered file changes, so unchanged clips are skipped."""
    relevant = {k: v for k, v in settings.to_dict().items()
                if k not in ("num_clips", "min_seconds", "max_seconds", "whisper_model", "model", "hook_overlay",
                             "campaign", "cam_box", "game_box")}
    if settings.framing == "stack":  # only matters for the stream layout; old renders keep their signatures
        relevant["cam_box"] = settings.cam_box
        if settings.game_box:
            relevant["game_box"] = settings.game_box
    parts = [candidate["start"], candidate["end"], relevant]
    hook = hook_text_for(candidate, settings)
    if hook:  # only added when there is one, so clips rendered before hooks existed don't all look outdated
        parts.append(hook)
    data = json.dumps(parts, sort_keys=True)
    return hashlib.sha1(data.encode()).hexdigest()[:12]


def hook_text_for(candidate, settings):
    return (candidate.get("hook_text") or "").strip() if settings.hook_overlay else ""


def render_clip(media, start, end, out_path, settings, words=None, progress=None, hook_text="", report=None):
    """Render [start, end] of the source to out_path. Raises ClipperError on failure.

    start/end are source times; media["offset"] (default 0) is where media's file begins in the source.
    """
    ffmpeg = find_tool("ffmpeg")
    offset = media.get("offset", 0)
    if media.get("duration") and end - offset > media["duration"] + 0.5:
        raise ClipperError(f"The clip ends at {end:.1f}s but the video is only {media['duration']:.1f}s long.")
    if end - start < 1:
        raise ClipperError("The clip is shorter than one second.")
    duration = end - start
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_w, out_h = output_size(settings.aspect, settings.quality)
    q = QUALITY[settings.quality]

    if settings.framing == "stack" and not overlay_visible(media, start, end, settings.cam_box):
        # an IRL or full-camera part of the stream: no camera box to put on top, so follow the face instead
        settings = dataclasses.replace(settings, framing="smart")
    if report is not None:
        report["layout"] = settings.framing
    center_x = 0.5
    if settings.framing == "smart":
        center_x = detect_subject_x(media, start, end) or 0.5

    # temp files get a per-render token: a Streamlit rerun can start a new render of the same clip while the
    # interrupted one is still cleaning up, and it must not delete the new render's files
    token = uuid.uuid4().hex[:8]
    subs_name = None
    plist = phrases(words, start, end) if settings.subtitles and words else []
    if plist or hook_text:
        subs_name = f"{out_path.stem}.{token}.ass"
        # in the stream layout the hook sits on the seam between camera and gameplay, not over the face
        hook_y = int(out_h * STACK_TOP) if settings.framing == "stack" else None
        ass = build_ass(plist, out_w, out_h, settings, hook_text, min(HOOK_SECONDS, duration), hook_y=hook_y)
        (out_path.parent / subs_name).write_text(ass, encoding="utf-8")

    cmd = [ffmpeg, "-hide_banner", "-nostdin", "-y", "-progress", "pipe:1", "-nostats"]
    for src, headers in media["inputs"]:
        cmd += [*_headers_args(headers), "-ss", f"{start - offset:.3f}", "-t", f"{duration:.3f}", "-i", src]
    cmd += ["-filter_complex", build_filter(media, settings, out_w, out_h, center_x, subs_name), "-map", "[v]"]
    if media.get("has_audio"):
        audio_input = len(media["inputs"]) - 1  # separate audio stream is the last input
        cmd += ["-map", f"{audio_input}:a:0?", "-c:a", "aac", "-b:a", "160k", "-ar", "48000"]
    tmp_out = out_path.with_name(f"{out_path.stem}.{token}.part.mp4")
    tail = ["-movflags", "+faststart", "-t", f"{duration:.3f}", str(tmp_out)]

    try:
        global _use_qsv
        if _use_qsv:
            try:
                _run_ffmpeg(cmd + _video_args(q, qsv=True) + tail, out_path.parent, duration, progress)
            except ClipperError:
                _use_qsv = False  # no Intel Quick Sync here (or it broke): use the CPU from now on
        if not _use_qsv:
            _run_ffmpeg(cmd + _video_args(q, qsv=False) + tail, out_path.parent, duration, progress)
        if not tmp_out.exists() or tmp_out.stat().st_size < 10_000:
            raise ClipperError("FFmpeg finished but produced an empty file.")
        tmp_out.replace(out_path)
    finally:
        tmp_out.unlink(missing_ok=True)
        if subs_name:
            (out_path.parent / subs_name).unlink(missing_ok=True)
    return out_path


# Intel Quick Sync encodes on the graphics chip: much cooler and faster than libx264 on a laptop CPU
_use_qsv = True


def _video_args(q, qsv):
    if qsv:  # global_quality is roughly crf + 2 for the same look
        return ["-c:v", "h264_qsv", "-preset", "veryfast", "-global_quality", str(q["crf"] + 2), "-pix_fmt", "nv12"]
    return ["-c:v", "libx264", "-preset", q["preset"], "-crf", str(q["crf"]), "-pix_fmt", "yuv420p"]


def _run_ffmpeg(cmd, cwd, duration, progress):
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace") as err:
        proc = subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=err, text=True)
        timer = threading.Timer(max(600, duration * 30), proc.kill)  # stuck network streams shouldn't hang forever
        timer.start()
        try:
            for line in proc.stdout:
                if progress and line.startswith("out_time_us=") and line.strip()[12:].isdigit():
                    progress(min(int(line.strip()[12:]) / 1e6 / duration, 1.0))
            proc.wait()
        finally:
            timer.cancel()
            if proc.poll() is None:  # stopped from the UI mid-render: don't leave ffmpeg running
                proc.kill()
                proc.wait()
        if proc.returncode != 0:
            err.seek(0)
            tail = "\n".join(err.read().strip().splitlines()[-8:])
            raise ClipperError(f"FFmpeg failed (exit code {proc.returncode}):\n{tail}")
