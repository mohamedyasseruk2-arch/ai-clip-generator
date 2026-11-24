import pytest

from clipper_core import sources, subtitles
from clipper_core.config import ClipperError, Settings


@pytest.mark.parametrize("url", [
    "https://www.youtube.com/watch?v=B7ZHYld89sk",
    "https://youtu.be/B7ZHYld89sk",
    "https://m.youtube.com/watch?v=abc",
    "https://www.youtube.com/shorts/B7ZHYld89sk",
    "https://www.youtube.com/watch?v=_GE6zf_hH48",
    "https://vimeo.com/123",
    "https://www.tiktok.com/@someone/video/7300000000000000000",
    "https://example.com/files/talk.mp4",
])
def test_supported_urls(url):
    assert sources.validate_url(url) == url


@pytest.mark.parametrize("url,message", [
    ("not a link", "doesn't look like a link"),
    ("ftp://youtube.com/x", "doesn't look like a link"),
    ("", "doesn't look like a link"),
    ("https://www.youtube.com/wat", "single YouTube video"),
    ("https://www.youtube.com/watch", "single YouTube video"),
])
def test_invalid_urls(url, message):
    with pytest.raises(ClipperError, match=message):
        sources.validate_url(url)


def test_parse_json3_auto_captions():
    data = {"events": [
        {"tStartMs": 1000, "dDurationMs": 4000, "segs": [{"utf8": "hello"}, {"utf8": " world", "tOffsetMs": 500}]},
        {"tStartMs": 2000, "dDurationMs": 10, "aAppend": 1, "segs": [{"utf8": "\n"}]},
        {"tStartMs": 3000, "dDurationMs": 2000, "segs": [{"utf8": "[Music]"}]},
        {"tStartMs": 4000, "dDurationMs": 2000, "segs": [{"utf8": "again."}]},
    ]}
    words = sources.parse_json3(data)
    assert [w["text"] for w in words] == ["hello", "world", "again."]
    assert words[0] == {"start": 1.0, "end": 1.5, "text": "hello"}
    assert words[1]["end"] == 4.0  # trimmed so it doesn't overlap the next word


def test_parse_json3_human_subtitle_lines_are_split():
    words = sources.parse_json3({"events": [{"tStartMs": 0, "dDurationMs": 3000, "segs": [{"utf8": "one two three"}]}]})
    assert [(w["text"], w["start"]) for w in words] == [("one", 0.0), ("two", 1.0), ("three", 2.0)]


def test_caption_track_prefers_original_language():
    info = {"automatic_captions": {"fr": [{"ext": "json3", "url": "fr"}], "en-orig": [{"ext": "json3", "url": "orig"}]}}
    assert sources.pick_caption_track(info) == ("en-orig", "orig")
    assert sources.pick_caption_track({}) == (None, None)


def test_segments_break_on_punctuation_and_gaps():
    words = [{"start": 0, "end": 0.5, "text": "Hi."}, {"start": 0.6, "end": 1, "text": "Next"},
             {"start": 5, "end": 5.5, "text": "later"}]
    segs = sources.segments_from_words(words)
    assert [s["text"] for s in segs] == ["Hi.", "Next", "later"]


WORDS = [{"start": 10 + i * 0.4, "end": 10 + i * 0.4 + 0.35, "text": w}
         for i, w in enumerate("this is a fairly long sentence, that keeps going until the end.".split())]


def test_phrases_are_short_and_clip_relative():
    plist = subtitles.phrases(WORDS, 10.0, 20.0)
    assert plist[0]["start"] == 0.0
    assert all(len(p["words"]) <= subtitles.MAX_WORDS for p in plist)
    assert all(a["end"] <= b["start"] for a, b in zip(plist, plist[1:]))
    assert plist[1]["words"][-1]["text"] == "sentence,"  # breaks after punctuation


def test_ass_file_styles_and_escaping():
    s = Settings(sub_color="#FF0000", sub_emphasis="none", sub_position="upper")
    words = [{"start": 0, "end": 1, "text": "{weird}"}]
    ass = subtitles.build_ass(subtitles.phrases(words, 0, 5), 1080, 1920, s)
    assert "PlayResX: 1080" in ass and "&H000000FF" in ass  # red in ASS BGR order
    assert "(weird)" in ass and "{weird}" not in ass
    assert ",8," in ass.split("Style: Default")[1]  # top alignment


def test_word_highlight_emits_one_event_per_word():
    s = Settings(sub_emphasis="word")
    ass = subtitles.build_ass(subtitles.phrases(WORDS[:3], 10, 20), 1080, 1920, s)
    assert ass.count("Dialogue:") == 3 and "\\c&H" in ass


def test_hook_text_is_a_top_box_for_the_first_seconds():
    ass = subtitles.build_ass([], 1080, 1920, Settings(), hook_text="Famous {last} words...", hook_seconds=3)
    hook = [l for l in ass.splitlines() if l.startswith("Dialogue:") and ",Hook," in l]
    assert len(hook) == 1 and hook[0].startswith("Dialogue: 1,0:00:00.00,0:00:03.00,Hook,")
    assert "Famous (last) words..." in hook[0]
    style = ass.split("Style: Hook,")[1].splitlines()[0].split(",")
    assert style[14] == "3" and style[17] == "8"  # boxed, top-centre
    upper = subtitles.build_ass([], 1080, 1920, Settings(sub_position="upper"), hook_text="x")
    assert upper.split("Style: Hook,")[1].split(",")[17] == "5"  # moves to the middle when subtitles are on top
    assert ",Hook," not in subtitles.build_ass([], 1080, 1920, Settings()).split("[Events]")[1]
