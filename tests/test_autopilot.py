from datetime import datetime
from types import SimpleNamespace

import pytest

from clipper_core import autopilot as ap
from clipper_core import social

CFG = {**ap.DEFAULT_CONFIG, "post_times": ["20:00", "10:00"]}
FEED = """<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:yt="http://www.youtube.com/xml/schemas/2015"
      xmlns:media="http://search.yahoo.com/mrss/">
  <entry><yt:videoId>new1</yt:videoId><published>2026-01-02T00:00:00+00:00</published>
    <media:group><media:community><media:statistics views="5000"/></media:community></media:group></entry>
  <entry><yt:videoId>old1</yt:videoId><published>2026-01-01T00:00:00+00:00</published></entry>
</feed>"""


@pytest.fixture(autouse=True)
def isolated_files(monkeypatch, tmp_path):
    """Tests must never touch the real log, state, config, inbox or tokens."""
    monkeypatch.setattr(ap, "HOME", tmp_path)
    monkeypatch.setattr(ap, "LOG_FILE", tmp_path / "autopilot.log")
    monkeypatch.setattr(ap, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(ap, "CONFIG_FILE", tmp_path / "autopilot.json")
    monkeypatch.setattr(ap, "INBOX_FILE", tmp_path / "inbox.txt")
    monkeypatch.setattr(social, "TOKENS_FILE", tmp_path / "tokens.json")


def test_next_slots_wraps_to_following_days():
    slots = ap.next_slots(datetime(2026, 1, 1, 12, 0), ["20:00", "10:00"], 4)
    assert slots == ["2026-01-01 20:00", "2026-01-02 10:00", "2026-01-02 20:00", "2026-01-03 10:00"]


def test_parse_feed_newest_first_with_views():
    videos = ap.parse_feed(FEED)
    assert [v["id"] for v in videos] == ["new1", "old1"]
    assert videos[0]["views"] == 5000 and videos[1]["views"] is None
    assert videos[0]["published"] - videos[1]["published"] == 86400


def _channel_state(seen):
    return {"channels": {"ch": {"id": "UC" + "x" * 22, "seen": seen}}, "sources": [], "done": {}, "posts": []}


def test_poll_queues_breakouts_and_waits_on_young_slow_videos(monkeypatch):
    now = 1_000_000.0
    feed = [{"id": "hot", "published": now - 2 * 3600, "views": 20000},     # 10k/h: a breakout
            {"id": "slow", "published": now - 3 * 3600, "views": 300},      # young and slow: wait
            {"id": "old1", "published": now - 100 * 3600, "views": 100000},
            {"id": "old2", "published": now - 200 * 3600, "views": 200000}]
    monkeypatch.setattr(ap, "latest_videos", lambda cid: feed)
    state = _channel_state(["old1", "old2"])
    ap.poll_channels(state, {**CFG, "channels": ["ch"], "min_breakout": 2}, now)
    assert [s["url"][-3:] for s in state["sources"]] == ["hot"] and state["sources"][0]["priority"] > 2
    assert "slow" not in state["channels"]["ch"]["seen"]  # re-checked next pass


def test_inbox_requeues_a_video_you_add_again():
    state = {"channels": {}, "sources": [], "done": {"https://x/v": {"status": "clipped", "at": "2026-01-01 10:00"}},
             "posts": []}
    ap.INBOX_FILE.write_text("https://x/v\nhttps://x/new\n", encoding="utf-8")
    ap.read_inbox(state)
    assert [(s["url"], s["fresh"]) for s in state["sources"]] == [("https://x/v", True), ("https://x/new", False)]
    assert "https://x/v" not in state["done"] and "x/v" not in ap.INBOX_FILE.read_text(encoding="utf-8")


def test_next_source_prefers_requested_then_fastest():
    state = {"sources": [{"url": "a", "priority": 1.5}, {"url": "b", "priority": 9.0},
                         {"url": "c", "priority": 3.0, "wait_until": "2999-01-01 00:00"}]}
    assert ap.next_source(state, ap._now())["url"] == "b"
    state["sources"].append({"url": "mine", "priority": None})
    assert ap.next_source(state, ap._now())["url"] == "mine"


def test_add_source_skips_duplicates_and_done():
    state = {"channels": {}, "sources": [], "done": {"https://x/done": {}}, "posts": []}
    assert ap.add_source(state, "https://x/a", "inbox")
    assert not ap.add_source(state, "https://x/a ", "inbox")
    assert not ap.add_source(state, "https://x/done", "inbox")
    assert len(state["sources"]) == 1


@pytest.mark.parametrize("size,expected", [
    (30_000_000, (30_000_000, 1)),                 # one chunk up to 64 MB
    (95_000_000, (10_000_000, 9)),                  # remainder folded into the last chunk
])
def test_tiktok_chunks(size, expected):
    chunk, count = social.tiktok_chunks(size)
    assert (chunk, count) == expected
    assert size - chunk * (count - 1) < 64 * 1024 * 1024  # last chunk stays within TikTok's limit


def test_compose_respects_platform_limits():
    clip = {"title": "A <great> moment " * 10, "caption": "Watch this", "hashtags": [f"#t{i}" for i in range(40)]}
    yt = social.compose(clip, "youtube")
    assert len(yt["title"]) <= 100 and "<" not in yt["title"] and len(yt["tags"]) == 15
    ig = social.compose(clip, "instagram")["caption"]
    assert ig.count("#") == 30 and ig.startswith("Watch this")


def _queue(monkeypatch, require_approval):
    monkeypatch.setattr(social, "connected", lambda: ["youtube", "tiktok"])
    state = {"channels": {}, "sources": [], "done": {}, "posts": []}
    clips = [{"id": "c1", "score": 60}, {"id": "c2", "score": 90}]
    n = ap.enqueue(state, {**CFG, "require_approval": require_approval}, SimpleNamespace(id="job"), clips)
    return state, n


def test_enqueue_schedules_best_clip_first_on_all_platforms(monkeypatch):
    state, n = _queue(monkeypatch, require_approval=False)
    assert n == 4
    due = {p["id"]: p["due"] for p in state["posts"]}
    assert due["job/c2/youtube"] == due["job/c2/tiktok"] < due["job/c1/youtube"]
    assert all(p["status"] == "scheduled" for p in state["posts"])


def test_approval_flow(monkeypatch):
    state, _ = _queue(monkeypatch, require_approval=True)
    assert all(p["status"] == "approval" and not p["due"] for p in state["posts"])
    assert ap.approve(state, CFG, "job/c2") == 2
    assert ap.skip(state, "job/c1") == 2
    assert sorted(p["status"] for p in state["posts"]) == ["scheduled", "scheduled", "skipped", "skipped"]


def test_failed_post_retries_then_fails(monkeypatch, tmp_path):
    monkeypatch.setattr(ap, "save_state", lambda state: None)
    clip_file = tmp_path / "c1.mp4"
    clip_file.write_bytes(b"x")
    job = SimpleNamespace(dir=tmp_path, candidate=lambda cid: {"id": cid, "render": {"file": "c1.mp4"}})
    monkeypatch.setattr(ap, "Job", lambda job_id: job)

    def boom(*a):
        raise RuntimeError("network down")
    monkeypatch.setattr(social, "post", boom)
    p = {"id": "job/c1/youtube", "job": "job", "clip": "c1", "platform": "youtube", "status": "scheduled",
         "due": "", "attempts": 0}
    for _ in range(ap.MAX_ATTEMPTS):
        ap._post_one({}, CFG, p)
    assert p["status"] == "failed" and p["attempts"] == ap.MAX_ATTEMPTS


def test_not_connected_fails_without_retry(monkeypatch, tmp_path):
    monkeypatch.setattr(ap, "save_state", lambda state: None)
    (tmp_path / "c1.mp4").write_bytes(b"x")
    monkeypatch.setattr(ap, "Job", lambda job_id: SimpleNamespace(
        dir=tmp_path, candidate=lambda cid: {"id": cid, "render": {"file": "c1.mp4"}}))

    def not_connected(*a):
        raise social.NotConnected("reconnect")
    monkeypatch.setattr(social, "post", not_connected)
    p = {"id": "x", "job": "job", "clip": "c1", "platform": "tiktok", "status": "scheduled", "due": "", "attempts": 0}
    ap._post_one({}, CFG, p)
    assert p["status"] == "failed" and p["attempts"] == 0


def test_poll_watches_non_youtube_profiles(monkeypatch):
    page = [{"id": "new", "url": "https://www.tiktok.com/@a/video/new", "published": None, "views": None},
            {"id": "old", "url": "https://www.tiktok.com/@a/video/old", "published": None, "views": None}]
    monkeypatch.setattr(ap, "latest_from_page", lambda url: page)
    monkeypatch.setattr(ap, "channel_id", lambda url: pytest.fail("YouTube lookup used for a TikTok profile"))
    url = "https://www.tiktok.com/@a"
    state = {"channels": {url: {"seen": ["old"]}}, "sources": [], "done": {}, "posts": []}
    ap.poll_channels(state, {**CFG, "channels": [url]})
    assert [s["url"] for s in state["sources"]] == ["https://www.tiktok.com/@a/video/new"]
