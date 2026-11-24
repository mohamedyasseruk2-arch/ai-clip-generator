"""The steps the UI and CLI share: open a source, analyse it, render clips.

`progress(stage, fraction, message)` is called throughout; stage is one of
"download", "transcribe", "analyze", "render", "done".
"""
import shutil
from pathlib import Path

from . import campaigns, highlights, loudness, sources, trends
from .config import LONG_SOURCE_SECONDS, ClipperError, Settings, load_env
from .jobs import Job, job_id_for_file, job_id_for_url
from .render import hook_text_for, render_clip, render_signature
from .transcribe import whisper, whisper_windows

SECTION_PAD = 1.0  # seconds downloaded either side of a clip, so a nudge of the times doesn't need a new download
VIDEO_EXTENSIONS ={".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v", ".mpg", ".mpeg", ".wmv", ".flv", ".ts"}


def _noop(stage, fraction, message=""):
    pass


def open_url(url, progress=_noop, info=None):
    """`info` from an earlier sources.probe_url(url) saves probing the link twice."""
    progress("download", 0.05, "Reading the link...")
    url = sources.validate_url(url)
    info = info or sources.probe_url(url)
    source = sources.source_from_info(url, info)
    job = Job.create(job_id_for_url(source["extractor"], source["video_id"]), source)
    job.log(f"Opened link: {source['title']} ({source['duration']:.0f}s)")
    return job, info


def open_file(path, progress=_noop):
    path = Path(path)
    if path.suffix.lower() not in VIDEO_EXTENSIONS:
        raise ClipperError(f"{path.suffix or 'That'} files aren't supported. Use MP4, MOV, MKV, WEBM or AVI.")
    progress("download", 0.05, "Reading the file...")
    media = sources.probe_file(path)
    job_id = job_id_for_file(path)
    job = Job(job_id)
    job.dir.mkdir(parents=True, exist_ok=True)
    stored = job.dir / f"source{path.suffix.lower()}"
    if path.resolve() != stored.resolve() and not stored.exists():
        shutil.copy2(path, stored)
    job = Job.create(job_id, {"type": "file", "path": str(stored), "filename": path.name,
                              "title": path.stem, "duration": media["duration"]})
    job.log(f"Opened file: {path.name} ({media['duration']:.0f}s)")
    return job


def media_for(job, progress=_noop, section=None):
    """Media to render from. For links with a (start, end) section, only that part is downloaded unless the
    whole video is already on disk; media["offset"] is where the file starts in the source."""
    src = job.data["source"]
    full = job.dir / "video" / "video.mp4"
    if src["type"] == "url" and section and not full.exists():
        start, end = max(section[0] - SECTION_PAD, 0), section[1] + SECTION_PAD
        progress("render", 0.0, "Downloading just this clip...")
        try:
            path = sources.download_section(src["url"], start, end, job.dir / "sections" / f"{start:.2f}-{end:.2f}.mp4")
            return {**sources.probe_file(path), "offset": start}
        except ClipperError as e:  # fall back to the whole video rather than fail the clip
            job.log(f"Section download failed ({e}); downloading the whole video instead")
    if src["type"] == "url":
        path = sources.download_video(src["url"], job.dir / "video",
                                      lambda f: progress("render", 0.0, f"Downloading the video... {f:.0%}"))
        return sources.probe_file(path)
    return sources.probe_file(src["path"])


def get_transcript(job, settings, info=None, progress=_noop, force=False):
    """Cached transcript, else YouTube captions, else Whisper."""
    cached = job.load_transcript()
    if cached and not force:
        progress("transcribe", 1.0, f"Using saved transcript ({cached['source']})")
        return cached
    src = job.data["source"]
    transcript = None
    if src["type"] == "url":
        progress("download", 0.3, "Looking for captions...")
        transcript = sources.fetch_captions(info or sources.probe_url(src["url"]))
        if transcript:
            job.log(f"Using the video's own captions ({transcript['source']})")
        else:
            job.log("No usable captions; downloading audio for Whisper")
            audio = sources.download_audio(src["url"], job.dir / "cache",
                                           lambda f: progress("download", 0.3 + 0.7 * f, f"Downloading audio... {f:.0%}"),
                                           until=src.get("duration") if src.get("in_progress") else None)
            media_path = audio
    else:
        media_path = src["path"]
    progress("download", 1.0, "Source ready")
    duration = src.get("duration") or 0
    if not transcript and duration > LONG_SOURCE_SECONDS:
        # hours of stream: transcribe only around the loud moments (minutes instead of hours on a CPU)
        progress("transcribe", 0.0, f"Finding the loud moments in {duration / 3600:.1f} hours of audio...")
        db = loudness.levels(media_path, duration,
                             lambda f: progress("transcribe", 0.25 * f, f"Finding the loud moments... {f:.0%}"))
        windows = loudness.loud_windows(db)
        minutes = sum(e - s for s, e in windows) / 60
        job.log(f"Long source: transcribing {len(windows)} loud moments ({minutes:.0f} min of audio)")
        transcript = whisper_windows(
            media_path, windows, settings.whisper_model,
            lambda f: progress("transcribe", 0.25 + 0.75 * f,
                               f"Transcribing {len(windows)} loud moments ({minutes:.0f} min)... {f:.0%}"))
        job.log(f"Transcribed with {transcript['source']}: {len(transcript['words'])} words")
    elif not transcript:
        progress("transcribe", 0.0, f"Transcribing with Whisper ({settings.whisper_model}); this can take a while...")
        transcript = whisper(media_path, settings.whisper_model,
                             lambda f: progress("transcribe", f, f"Transcribing... {f:.0%}"))
        job.log(f"Transcribed with {transcript['source']}: {len(transcript['words'])} words")
    transcript["segments"] = sources.segments_from_words(transcript["words"])
    job.save_transcript(transcript)
    progress("transcribe", 1.0, "Transcript ready")
    return transcript


def viewer_hotspots(job, duration, progress=_noop):
    """Moments viewers timestamp in the top comments of a link; [] for files or when comments can't be read."""
    src = job.data["source"]
    if src["type"] != "url" or not sources.is_youtube(src["url"]):  # other sites have no top-N comment limit
        return []
    progress("analyze", 0.05, "Reading viewer comments for timestamped moments...")
    try:
        spots = trends.comment_hotspots(sources.fetch_comments(src["url"]), duration)
    except Exception as e:  # noqa: BLE001 - comments are a bonus signal; never fail the analysis over them
        job.log(f"Couldn't read comments ({e}); analysing without viewer hotspots")
        return []
    if spots:
        job.log("Viewer hotspots: " + ", ".join(f"{s['time']}s x{s['mentions']}" for s in spots))
    return spots


def analyze(job, settings, info=None, progress=_noop, force_transcript=False):
    """Transcript -> Gemini candidates, saved in the job."""
    load_env()
    campaign = campaigns.for_settings(settings)
    if campaign:
        settings = campaigns.apply_settings(settings, campaign)
    settings.validate()
    try:
        job.set_status("analyzing")
        transcript = get_transcript(job, settings, info, progress, force_transcript)
        cleanup_cache(job)
        duration = job.data["source"].get("duration") or transcript["segments"][-1]["end"]
        hotspots = viewer_hotspots(job, duration, progress)
        progress("analyze", 0.1, f"Asking Gemini ({settings.gemini_model()}) for the best moments...")
        candidates, notes = highlights.find_highlights(
            transcript["segments"], duration, settings, hotspots=hotspots, campaign=campaign,
            progress=lambda f, m: progress("analyze", 0.1 + 0.9 * f, m))
        if campaign:
            notes = "\n".join(n for n in [campaign_warning(job, campaign), notes] if n)
        for old in job.data.get("candidates", []):  # a new analysis replaces the clips: drop their old renders
            if (old.get("render") or {}).get("file"):
                (job.dir / old["render"]["file"]).unlink(missing_ok=True)
        job.data.update(candidates=candidates, notes=notes, settings=settings.to_dict(),
                        transcript_source=transcript["source"], hotspots=hotspots)
        job.set_status("analyzed")
        job.log(f"Gemini picked {len(candidates)} clips")
        progress("analyze", 1.0, f"Found {len(candidates)} clips")
        return job
    except Exception as e:
        job.log(f"Analysis failed: {e}")
        job.set_status("failed", str(e))
        raise


def campaign_warning(job, campaign):
    """A note when the source is older than the campaign allows (it wouldn't be paid)."""
    earliest, published = campaign.get("earliest_source_date"), job.data["source"].get("published")
    if earliest and published and published < earliest:
        return (f"⚠️ This video is from {published}, but the campaign only counts sources from {earliest} on. "
                "Clips from it won't be approved.")
    return ""


def recut(job, cid, settings):
    load_env()
    transcript = job.load_transcript()
    if not transcript:
        raise ClipperError("No transcript saved for this job; run the analysis again.")
    campaign = campaigns.for_settings(settings)
    if campaign:
        settings = campaigns.apply_settings(settings, campaign)
    old = job.candidate(cid)
    new = highlights.recut(old, transcript["segments"], job.data["source"].get("duration"), settings,
                           campaign=campaign)
    old.update({k: v for k, v in new.items() if k not in ("id", "selected", "render")})
    job.save()
    job.log(f"AI re-cut {cid}: {old['start']:.1f}-{old['end']:.1f}s")
    return old


def render(job, settings, cids=None, progress=_noop):
    """Render the given clips (default: all selected). Unchanged clips that already exist are skipped."""
    campaign = campaigns.for_settings(settings)
    if campaign:
        settings = campaigns.apply_settings(settings, campaign)
    settings.validate()
    transcript = job.load_transcript() or {}
    todo = [c for c in job.data["candidates"] if (c["id"] in cids if cids else c.get("selected"))]
    if not todo:
        raise ClipperError("No clips selected to render.")
    duration = job.data["source"].get("duration") or 0
    for c in todo:  # validate everything before spending time rendering
        if not (0 <= c["start"] < c["end"]) or (duration and c["end"] > duration + 0.5):
            raise ClipperError(f"Clip {c['id']} has invalid times {c['start']}-{c['end']}s (video is {duration:.0f}s).")
    job.set_status("rendering")
    full_media = None
    failures = []
    for n, c in enumerate(todo):
        sig = render_signature(c, settings)
        out_name = f"renders/{c['id']}-{sig}.mp4"
        if c.get("render", {}).get("signature") == sig and (job.dir / out_name).exists():
            progress("render", (n + 1) / len(todo), f"{c['id']} unchanged, skipped")
            continue
        progress("render", n / len(todo), f"Rendering {c['id']}: {c['title']}")
        try:
            media = full_media or media_for(job, progress, (c["start"], c["end"]))
            if "offset" not in media:
                full_media = media  # whole file on hand: reuse it for the remaining clips
            report = {}
            render_clip(media, c["start"], c["end"], job.dir / out_name, settings, transcript.get("words"),
                        lambda f, n=n: progress("render", (n + f) / len(todo), f"Rendering {c['id']}... {f:.0%}"),
                        hook_text=hook_text_for(c, settings), report=report)
        except ClipperError as e:
            failures.append(f"{c['id']}: {e}")
            c["render"] = {"status": "failed", "error": str(e)}
            job.log(f"Render of {c['id']} failed: {e}")
            job.save()
            continue
        old = (job.dir / c["render"]["file"]) if c.get("render", {}).get("file") else None
        if old and old.exists() and old.name != Path(out_name).name:
            old.unlink()  # remove the previous version of this clip
        c["render"] = {"status": "done", "file": out_name, "signature": sig, "layout": report.get("layout")}
        if settings.framing == "stack" and report.get("layout") != "stack":
            job.log(f"{c['id']}: no camera box on screen (IRL or full-camera part), used the face-following crop")
        job.log(f"Rendered {c['id']} -> {out_name}")
        job.save()
    job.data["settings"] = settings.to_dict()
    job.set_status("rendered" if not failures else "render_failed", "\n".join(failures) or None)
    progress("done", 1.0, "Finished" if not failures else f"{len(failures)} clip(s) failed")
    if failures:
        raise ClipperError("Some clips failed to render:\n" + "\n".join(failures))
    return job


def cleanup_cache(job):
    """Delete downloaded audio once a transcript exists; it's only needed for Whisper."""
    cache = job.dir / "cache"
    if cache.exists() and job.transcript_file.exists():
        shutil.rmtree(cache, ignore_errors=True)


def settings_for(job):
    return Settings.from_dict(job.data.get("settings"))
