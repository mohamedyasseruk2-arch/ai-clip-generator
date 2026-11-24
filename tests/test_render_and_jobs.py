import json
import subprocess

import pytest

from clipper_core import config, pipeline, render, sources
from clipper_core.config import ClipperError, Settings, output_size
from clipper_core.jobs import Job, job_id_for_file


@pytest.mark.parametrize("w,h,aspect,expected", [
    (1920, 1080, "9:16", (606, 1080)),   # landscape -> vertical: crop the sides
    (1080, 1920, "9:16", (1080, 1920)),  # already vertical: keep everything
    (1080, 1080, "9:16", (606, 1080)),   # square
    (1080, 1920, "16:9", (1080, 606)),   # portrait -> landscape: crop top/bottom
])
def test_crop_keeps_aspect_without_stretching(w, h, aspect, expected):
    cw, ch, x, y = render.compute_crop(w, h, aspect)
    assert (cw, ch) == expected
    assert 0 <= x <= w - cw and 0 <= y <= h - ch


def test_crop_follows_subject_but_stays_inside_frame():
    _, _, x_left, _ = render.compute_crop(1920, 1080, "9:16", center_x=0.0)
    _, _, x_right, _ = render.compute_crop(1920, 1080, "9:16", center_x=1.0)
    assert x_left == 0 and x_right == 1920 - 606


def test_output_sizes():
    assert output_size("9:16", "standard") == (1080, 1920)
    assert output_size("1:1", "small") == (720, 720)
    assert output_size("16:9", "high") == (1920, 1080)


def test_render_signature_changes_with_times_and_settings():
    c = {"start": 1.0, "end": 5.0}
    base = render.render_signature(c, Settings())
    assert base == render.render_signature(c, Settings(num_clips=7))  # analysis-only setting
    assert base != render.render_signature({"start": 1.5, "end": 5.0}, Settings())
    assert base != render.render_signature(c, Settings(sub_color="#000000"))


def test_missing_ffmpeg_gives_install_hint(monkeypatch, tmp_path):
    monkeypatch.setattr(config.shutil, "which", lambda name: None)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    with pytest.raises(ClipperError, match="winget install Gyan.FFmpeg"):
        config.find_tool("ffmpeg")


def test_settings_validation():
    with pytest.raises(ClipperError):
        Settings(min_seconds=60, max_seconds=30).validate()
    with pytest.raises(ClipperError):
        Settings(aspect="3:2").validate()


# ---------- real ffmpeg rendering on a generated test video ----------

def _make_video(path, audio=True, size="640x360", seconds=8):
    ffmpeg = config.find_tool("ffmpeg")
    cmd = [ffmpeg, "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc2=size={size}:rate=25:duration={seconds}"]
    if audio:
        cmd += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}"]
    cmd += ["-c:v", "libx264", "-pix_fmt", "yuv420p", *(["-c:a", "aac"] if audio else []), "-shortest", str(path)]
    subprocess.run(cmd, check=True)
    return path


def _probe(path):
    out = subprocess.run([config.find_tool("ffprobe"), "-v", "error", "-show_streams", "-show_format", "-of", "json",
                          str(path)], capture_output=True, text=True, check=True).stdout
    return json.loads(out)


WORDS = [{"start": 2 + i * 0.5, "end": 2.45 + i * 0.5, "text": w} for i, w in enumerate("testing one two three four five".split())]


@pytest.mark.parametrize("framing,size", [("smart", "640x360"), ("fit", "640x360"), ("center", "360x640")])
def test_render_vertical_clip_with_subtitles(tmp_path, framing, size):
    src = _make_video(tmp_path / "src.mp4", size=size)
    media = sources.probe_file(src)
    out = tmp_path / "out" / "clip.mp4"
    seen = []
    render.render_clip(media, 2.0, 6.0, out, Settings(framing=framing, quality="small"), WORDS, seen.append)
    info = _probe(out)
    video = next(s for s in info["streams"] if s["codec_type"] == "video")
    assert (video["width"], video["height"]) == (720, 1280)
    assert any(s["codec_type"] == "audio" for s in info["streams"])
    assert 3.5 <= float(info["format"]["duration"]) <= 4.5
    assert seen and seen[-1] > 0.5
    assert not list(out.parent.glob("*.ass")) and not list(out.parent.glob("*.part.mp4"))  # temp files cleaned


def test_render_source_without_audio(tmp_path):
    src = _make_video(tmp_path / "silent.mp4", audio=False)
    media = sources.probe_file(src)
    assert media["has_audio"] is False
    out = render.render_clip(media, 1.0, 4.0, tmp_path / "c.mp4", Settings(quality="small", subtitles=False))
    assert not any(s["codec_type"] == "audio" for s in _probe(out)["streams"])


def test_render_from_section_uses_source_times(tmp_path):
    """A downloaded section starting at 100 s in the source: source times 102-106 are file times 2-6."""
    media = {**sources.probe_file(_make_video(tmp_path / "sec.mp4")), "offset": 100.0}
    words = [{**w, "start": w["start"] + 100, "end": w["end"] + 100} for w in WORDS]
    out = render.render_clip(media, 102.0, 106.0, tmp_path / "c.mp4", Settings(quality="small"), words)
    assert 3.5 <= float(_probe(out)["format"]["duration"]) <= 4.5
    with pytest.raises(ClipperError, match="only"):
        render.render_clip(media, 105.0, 112.0, tmp_path / "d.mp4", Settings())


def test_media_for_downloads_only_the_section(monkeypatch, tmp_path):
    clip = _make_video(tmp_path / "piece.mp4")
    job = Job.create("youtube-x", {"type": "url", "url": "https://youtu.be/x", "duration": 600}, jobs_dir=tmp_path)
    calls = []
    monkeypatch.setattr(sources, "download_section", lambda url, s, e, dest: calls.append((s, e)) or clip)
    media = pipeline.media_for(job, section=(120.0, 150.0))
    assert calls == [(119.0, 151.0)] and media["offset"] == 119.0


def test_render_rejects_range_past_end(tmp_path):
    media = sources.probe_file(_make_video(tmp_path / "s.mp4"))
    with pytest.raises(ClipperError, match="only"):
        render.render_clip(media, 5.0, 20.0, tmp_path / "c.mp4", Settings())


def test_probe_rejects_non_video(tmp_path):
    bad = tmp_path / "x.mp4"
    bad.write_text("not a video")
    with pytest.raises(ClipperError):
        sources.probe_file(bad)


# ---------- jobs, metadata and the render/export pipeline ----------

def test_full_local_pipeline_render_skip_and_export(tmp_path):
    src = _make_video(tmp_path / "talk.mp4")
    job = Job.create(job_id_for_file(src), {"type": "file", "path": str(src), "title": "talk", "duration": 8.0},
                     tmp_path / "jobs")
    job.save_transcript({"source": "test", "words": WORDS, "segments": []})
    job.data["candidates"] = [
        {"id": "c1", "start": 1.0, "end": 4.0, "title": "First one", "caption": "cap", "hashtags": ["#a"],
         "selected": True, "render": {}},
        {"id": "c2", "start": 4.0, "end": 7.0, "title": "Second", "caption": "", "hashtags": [], "selected": False,
         "render": {}},
    ]
    job.save()
    settings = Settings(quality="small", framing="center")

    pipeline.render(job, settings)
    c1 = job.candidate("c1")
    assert c1["render"]["status"] == "done" and (job.dir / c1["render"]["file"]).exists()
    assert job.candidate("c2")["render"] == {}  # unselected clips aren't rendered

    messages = []
    pipeline.render(job, settings, progress=lambda s, f, m="": messages.append(m))
    assert any("unchanged, skipped" in m for m in messages)

    zip_path = job.export()
    meta = json.loads((job.export_dir / "metadata.json").read_text(encoding="utf-8"))
    assert meta["source"]["title"] == "talk" and len(meta["clips"]) == 1
    assert meta["clips"][0]["file"] == "01-first-one.mp4"
    assert (job.export_dir / "01-first-one.mp4").exists() and zip_path.exists()
    saved = json.loads(job.meta_file.read_text(encoding="utf-8"))
    assert saved["status"] == "rendered"


def test_render_rejects_invalid_times(tmp_path):
    job = Job.create("t", {"type": "file", "path": "x", "duration": 10.0}, tmp_path)
    job.data["candidates"] = [{"id": "c1", "start": 5, "end": 50, "title": "", "selected": True, "render": {}}]
    with pytest.raises(ClipperError, match="invalid times"):
        pipeline.render(job, Settings())


def test_slugify_keeps_non_latin_titles():
    from clipper_core.jobs import slugify

    assert slugify("حقيقة كلاب الشارع") == "حقيقة-كلاب-الشارع"
    assert slugify('Bad: "name"/?*') == "bad-name"
    assert slugify("!!!") == "video"


def test_job_ids_differ_per_source(tmp_path):
    a, b = tmp_path / "a.mp4", tmp_path / "b.mp4"
    a.write_bytes(b"1")
    b.write_bytes(b"22")
    assert job_id_for_file(a) != job_id_for_file(b)


def test_export_requires_rendered_clips(tmp_path):
    job = Job.create("j", {"type": "file"}, tmp_path)
    job.data["candidates"] = [{"id": "c1", "selected": True, "render": {}}]
    with pytest.raises(ClipperError, match="Render these"):
        job.export()


def test_hook_text_changes_the_signature_only_when_shown():
    c = {"start": 1.0, "end": 5.0}
    base = render.render_signature(c, Settings())
    hooked = {**c, "hook_text": "Wait until you hear this..."}
    assert render.render_signature(hooked, Settings()) != base
    assert render.render_signature(hooked, Settings(hook_overlay=False)) == base
    assert render.render_signature({**c, "hook_text": ""}, Settings()) == base  # old renders don't go stale
