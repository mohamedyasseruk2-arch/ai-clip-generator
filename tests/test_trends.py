import json

from clipper_core import highlights, trends
from clipper_core.config import Settings


def test_parse_timestamps():
    assert trends.parse_timestamps("best part 1:08:51 and 42:45, not 99:99 or 12:345", 5000) == [4131, 2565]
    assert trends.parse_timestamps("at 2:00:00", 3600) == []  # past the end of the video


def test_hotspots_merge_nearby_mentions_and_skip_chapter_lists():
    comments = [
        {"text": "12:30 😂😂", "like_count": 900},
        {"text": "12:50 this part", "like_count": 10},
        {"text": "40:00 wow", "like_count": 0},
        {"text": "0:00 intro 5:00 a 10:00 b 15:00 c", "like_count": 5000},  # chapters: ignored
    ]
    spots = trends.comment_hotspots(comments, 3600)
    assert spots[0]["mentions"] == 2 and 750 <= spots[0]["time"] <= 770
    assert [s["time"] for s in spots] == [spots[0]["time"], 2400]
    assert "intro" not in json.dumps(spots)


def test_focus_segments_needs_enough_evidence():
    segs = [{"start": t, "end": t + 5, "text": "x"} for t in range(0, 3600, 10)]
    spots = [{"time": 600}, {"time": 1800}]
    assert trends.focus_segments(segs, spots) is segs  # 2 hotspots: read everything
    spots.append({"time": 3000})
    kept = trends.focus_segments(segs, spots)
    assert len(kept) < len(segs) / 2 and all(any(abs(s["start"] - h["time"]) <= 240 for h in spots) for s in kept)


def test_breakout_ratio_against_channel_median():
    now = 1_000_000.0
    vids = [{"id": "a", "published": now - 10 * 3600, "views": 100_000},   # 10k/h
            {"id": "b", "published": now - 100 * 3600, "views": 100_000},  # 1k/h
            {"id": "c", "published": now - 100 * 3600, "views": 100_000},
            {"id": "x", "published": now - 3600, "views": None}]
    r = trends.breakout_ratios(vids, now)
    assert r["a"] == 10 and r["b"] == 1 and "x" not in r


class FakeClient:
    def __init__(self, payload):
        self.prompts = []
        self.models = self
        self.payload = payload

    def generate_content(self, model, contents, config):
        self.prompts.append(contents)
        return type("R", (), {"text": json.dumps(self.payload)})()


def _cand(start, end, title):
    return {"start": start, "end": end, "title": title, "description": "", "reason": "", "hook": "", "topic": "",
            "caption": "", "hashtags": [], "scores": dict.fromkeys(highlights.SCORE_KEYS, 7)}


def test_viewer_hotspots_steer_prompt_and_rank_clips():
    segs = [{"start": t, "end": t + 10, "text": f"line {t}"} for t in range(0, 3600, 10)]
    spots = [{"time": 2000, "mentions": 3, "weight": 6.0, "quotes": ["so funny"]},
             {"time": 600, "mentions": 1, "weight": 1.0, "quotes": []},
             {"time": 3000, "mentions": 1, "weight": 1.0, "quotes": []}]
    client = FakeClient({"candidates": [_cand(100, 140, "no hotspot"), _cand(1990, 2030, "viewers loved")],
                         "notes": ""})
    cands, _ = highlights.find_highlights(segs, 3600, Settings(min_seconds=25, max_seconds=60), client, spots)
    prompt = client.prompts[0]
    assert "so funny" in prompt and "33:20" in prompt
    assert "line 100]" not in prompt.replace("[100.0-110.0] line 100", "")  # far from hotspots: not sent
    assert cands[0]["title"] == "viewers loved" and cands[0]["id"] == "c1"
    assert cands[0]["score"] == 79 and cands[0]["viewer_mentions"] == 3  # 70 + 3 mentions x 3
