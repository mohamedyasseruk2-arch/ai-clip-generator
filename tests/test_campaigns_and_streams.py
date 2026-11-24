"""Campaign profiles, long-stream handling (loud moments, chunked AI) and the stream layout."""
import json
import subprocess

import pytest

from clipper_core import campaigns, config, highlights, loudness, render, sources
from clipper_core.config import ClipperError, Settings
from clipper_core.jobs import Job

NOVA = {
    "name": "Nova – GTA 6", "brief": "Your captions must use the hashtag: #NOVA", "look_for": ["funny moments"],
    "avoid": ["shows Nova in a negative light"], "required_hashtags": ["NOVA"], "required_mentions": ["Nova"],
    "caption_language": "English", "earliest_source_date": "2026-11-18", "min_seconds": 20, "max_seconds": 60,
    "aspect": "9:16", "framing": "stack", "cam_box": "0,0,0.2578,0.2583",
    "manual_checks": ["No logos on screen"],
}


@pytest.fixture
def campaign_dir(tmp_path, monkeypatch):
    d = tmp_path / "campaigns"
    monkeypatch.setattr(campaigns, "CAMPAIGNS_DIR", d)
    return d


def clip(**kw):
    return {"id": "c1", "start": 100.0, "end": 140.0, "title": "Nova loses it", "description": "", "caption": "No way",
            "hook_text": "Wait for it", "hashtags": ["#gta6"], "selected": True, **kw}


# ---------- campaign profiles ----------

def test_save_load_and_apply(campaign_dir):
    c = campaigns.save(NOVA)
    assert c["id"] == "nova-gta-6" and c["required_hashtags"] == ["#NOVA"]
    assert campaigns.load("nova-gta-6")["name"] == "Nova – GTA 6"
    assert [x["id"] for x in campaigns.list_all()] == ["nova-gta-6"]
    s = campaigns.apply_settings(Settings(min_seconds=30, max_seconds=90), c)
    assert (s.min_seconds, s.max_seconds, s.framing, s.campaign) == (20, 60, "stack", "nova-gta-6")
    s.validate()


def test_bad_campaigns_are_rejected(campaign_dir):
    with pytest.raises(ClipperError, match="name"):
        campaigns.save({**NOVA, "name": " "})
    with pytest.raises(ClipperError, match="camera position"):
        campaigns.save({**NOVA, "cam_box": ""})
    with pytest.raises(ClipperError, match="date"):
        campaigns.save({**NOVA, "earliest_source_date": "18/11/2026"})
    with pytest.raises(ClipperError, match="not found"):
        campaigns.load("nope")


def test_enforce_adds_hashtags_and_name():
    c = campaigns._clean(NOVA)
    x = campaigns.enforce(clip(hashtags=["#gta6", "#nova"]), c)
    assert x["hashtags"] == ["#NOVA", "#gta6"]  # required first, no case-duplicate
    assert x["caption"].startswith("Nova") and "No way" in x["caption"]
    y = campaigns.enforce(clip(caption="NOVA can't drive"), c)
    assert y["caption"] == "NOVA can't drive"  # already mentioned: unchanged


def test_check_reports_ok_fail_and_manual():
    c = campaigns._clean(NOVA)
    s = campaigns.apply_settings(Settings(), c)
    good = campaigns.check(campaigns.enforce(clip(), c), c, {"published": "2026-11-19"}, s)
    assert not [t for st, t in good if st == "fail"]
    assert ("manual", "No logos on screen") in good
    assert campaigns.summary(good).startswith("✅")
    fails = [t for st, t in campaigns.check(clip(caption="ラシー最高", end=200.0), c, {"published": "2026-11-10"}, s)
             if st == "fail"]
    assert any("Length" in t for t in fails) and any("#NOVA" in t for t in fails)
    assert any("English" in t for t in fails) and any("Source date" in t for t in fails)


def test_prompt_block_quotes_the_brief():
    block = campaigns.prompt_block(campaigns._clean(NOVA))
    assert "#NOVA" in block and "funny moments" in block and "negative light" in block
    assert "<<<" in block and "in English" in block
    assert campaigns.prompt_block(None) == ""


# ---------- chunked AI for long transcripts ----------

def test_long_transcripts_are_sent_in_parts_and_campaign_applied(monkeypatch):
    # 6 hours of speech, a 20-word line every 10 seconds
    segments = [{"start": i * 10.0, "end": i * 10.0 + 9.5, "text": "word " * 60} for i in range(2160)]
    prompts = []

    def fake_gemini(prompt, model, client=None):
        prompts.append(prompt)
        lines = [l for l in prompt.splitlines() if l.startswith("[")]
        first = float(lines[0][1:].split("-")[0])
        return json.dumps({"notes": "", "candidates": [
            {"start": first + 100, "end": first + 139.5, "title": f"part at {first}", "description": "",
             "reason": "", "hook": "", "clip_type": "funny", "hook_text": "", "topic": "", "caption": "lol",
             "hashtags": ["#gta6"], "scores": {"hook": 9, "clarity": 8, "story": 7, "usefulness": 6, "payoff": 8}}]})

    monkeypatch.setattr(highlights, "call_gemini", fake_gemini)
    c = campaigns._clean(NOVA)
    seen = []
    out, notes = highlights.find_highlights(segments, 21600, Settings(num_clips=5, min_seconds=20, max_seconds=60),
                                            campaign=c, progress=lambda f, m: seen.append(m))
    assert len(prompts) > 3 and all("part " in p and "#NOVA" in p for p in prompts)
    assert len(out) == 5 and all(x["hashtags"][0] == "#NOVA" and x["caption"].startswith("Nova") for x in out)
    assert any("part 2 of" in m for m in seen)


def test_one_failed_part_does_not_sink_the_stream(monkeypatch):
    segments = [{"start": i * 10.0, "end": i * 10.0 + 9.5, "text": "word " * 60} for i in range(600)]
    calls = {"n": 0}

    def flaky(prompt, model, client=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ClipperError("Gemini request failed (503)")
        first = float([l for l in prompt.splitlines() if l.startswith("[")][0][1:].split("-")[0])
        return json.dumps({"notes": "", "candidates": [{"start": first, "end": first + 39.5, "title": "x",
                                                        "scores": {}}]})

    monkeypatch.setattr(highlights, "call_gemini", flaky)
    out, notes = highlights.find_highlights(segments, 6000, Settings(num_clips=3, min_seconds=20, max_seconds=60))
    assert out and "Part 1 of" in notes and "skipped" in notes


# ---------- loud moments ----------

def test_loud_windows_find_the_peaks():
    db = [-30.0] * 7200
    for t in (1000, 5000):
        db[t:t + 6] = [-6.0] * 6
    windows = loudness.loud_windows(db)
    assert len(windows) == 2
    assert all(s <= p <= e for (s, e), p in zip(windows, (1000, 5000)))
    assert windows[0] == (1000 - loudness.BEFORE + 1, 1000 + loudness.AFTER + 1) or windows[0][1] - windows[0][0] > 100


def test_levels_reads_real_audio(tmp_path):
    ff = config.find_tool("ffmpeg")
    path = tmp_path / "a.m4a"
    subprocess.run([ff, "-v", "error", "-y", "-f", "lavfi", "-i",
                    "aevalsrc='sin(440*2*PI*t)*if(between(t,30,35),0.9,0.03)':s=16000:d=60", str(path)], check=True)
    db = loudness.levels(path, 60)
    assert 58 <= len(db) <= 61
    assert 30 <= max(range(len(db)), key=db.__getitem__) <= 35
    (start, end), = loudness.loud_windows(db)
    assert start <= 31 and end >= 34


# ---------- stream layout ----------

def _stream_video(path, with_cam=True, seconds=6):
    """Blurred 'gameplay' with (optionally) a sharp-edged facecam box in the top-left corner."""
    ff = config.find_tool("ffmpeg")
    cmd = [ff, "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc2=size=1280x720:rate=25:duration={seconds}",
           "-f", "lavfi", "-i", f"sine=frequency=300:duration={seconds}"]
    chain = "[0:v]boxblur=20:3[bg]"
    if with_cam:
        cmd += ["-f", "lavfi", "-i", f"color=c=0x2050c0:size=330x186:rate=25:duration={seconds}"]
        chain += ";[bg][2:v]overlay=0:0[v]"
    else:
        chain += ";[bg]null[v]"
    cmd += ["-filter_complex", chain, "-map", "[v]", "-map", "1:a", "-c:v", "libx264", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-shortest", str(path)]
    subprocess.run(cmd, check=True)
    return path


def test_stack_layout_keeps_gameplay_off_the_camera():
    top_h, cam, game = render.stack_layout(1920, 1080, "0,0,0.2573,0.2574", 1080, 1920)
    assert top_h == 768
    cw, ch, cx, cy = cam
    assert cx >= 0 and cy >= 0 and cx + cw <= 494 and cy + ch <= 278
    gw, gh, gx, gy = game
    assert gx >= 494 and gx + gw <= 1920 and gh == 1080


@pytest.mark.parametrize("with_cam,layout", [(True, "stack"), (False, "smart")])
def test_stack_render_uses_layout_only_when_camera_box_shows(tmp_path, with_cam, layout):
    src = _stream_video(tmp_path / "s.mp4", with_cam)
    media = sources.probe_file(src)
    report = {}
    out = render.render_clip(media, 1.0, 5.0, tmp_path / "c.mp4",
                             Settings(framing="stack", cam_box="0,0,0.2578,0.2583", quality="small"),
                             [{"start": 1.5, "end": 2.0, "text": "hello"}], hook_text="Watch this", report=report)
    assert report["layout"] == layout
    video = next(s for s in json.loads(subprocess.run(
        [config.find_tool("ffprobe"), "-v", "error", "-show_streams", "-of", "json", str(out)],
        capture_output=True, text=True, check=True).stdout)["streams"] if s["codec_type"] == "video")
    assert (video["width"], video["height"]) == (720, 1280)


def test_stack_needs_a_camera_position():
    with pytest.raises(ClipperError, match="camera position"):
        Settings(framing="stack").validate()


# ---------- export checklist ----------

def test_export_writes_campaign_checklist(tmp_path, campaign_dir):
    c = campaigns.save(NOVA)
    job = Job.create("twitch-x", {"type": "url", "url": "https://www.twitch.tv/videos/1", "duration": 36000,
                                  "published": "2026-11-19", "title": "GTA 6"}, jobs_dir=tmp_path)
    (job.dir / "renders").mkdir()
    (job.dir / "renders" / "c1.mp4").write_bytes(b"x" * 20000)
    job.data["candidates"] = [campaigns.enforce(clip(render={"file": "renders/c1.mp4"}), c),
                              clip(id="c2", render={"file": "renders/c1.mp4"})]
    job.data["settings"] = Settings(campaign=c["id"]).to_dict()
    job.save()
    job.export()
    text = (job.export_dir / "CHECKLIST.txt").read_text(encoding="utf-8")
    assert "Nova – GTA 6" in text and "[CHECK] No logos on screen" in text
    assert any(l.strip().startswith("[FAIL]") and "Hashtag #NOVA" in l for l in text.splitlines())  # c2 not enforced
    meta = json.loads((job.export_dir / "metadata.json").read_text(encoding="utf-8"))
    assert meta["campaign"] == "Nova – GTA 6" and meta["clips"][0]["compliance"]
