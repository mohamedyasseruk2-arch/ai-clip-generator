import json

import pytest

from clipper_core import highlights
from clipper_core.config import ClipperError, Settings

# 20 ten-second sentences: 0-10, 10-20, ... 190-200
SEGMENTS = [{"start": i * 10.0, "end": i * 10.0 + 9.5, "text": f"Sentence {i}."} for i in range(20)]
SETTINGS = Settings(num_clips=3, min_seconds=30, max_seconds=60)


def cand(start, end, title="A clip", **scores):
    return {"start": start, "end": end, "title": title, "description": "d", "reason": "r", "hook": "Sentence",
            "topic": "t", "caption": "c", "hashtags": ["fun", "#ai", "two words"],
            "scores": {"hook": 8, "clarity": 7, "story": 6, "usefulness": 5, "payoff": 9, **scores}}


def test_valid_candidates_are_snapped_and_scored():
    out, warnings = highlights.validate_candidates([cand(11, 48)], SEGMENTS, 200, SETTINGS)
    assert warnings == []
    c = out[0]
    assert (c["start"], c["end"]) == (10.0, 49.5)  # snapped to sentence boundaries
    assert c["score"] == 70
    assert c["hashtags"] == ["#fun", "#ai", "#twowords"]
    assert c["id"] == "c1" and c["selected"] is True


def test_out_of_range_and_inverted_are_rejected():
    out, warnings = highlights.validate_candidates([cand(-5, 40), cand(150, 260), cand(80, 50)], SEGMENTS, 200, SETTINGS)
    assert out == []
    assert len(warnings) == 3


def test_too_long_is_trimmed_and_too_short_is_extended():
    out, _ = highlights.validate_candidates([cand(0, 120), cand(100, 110)], SEGMENTS, 200, SETTINGS)
    by_start = {c["start"]: c for c in out}
    assert by_start[0.0]["end"] - 0.0 <= 60
    assert 30 <= by_start[100.0]["end"] - 100.0 <= 60


def test_overlapping_lower_score_is_dropped():
    good = cand(10, 50, "Best", hook=10, payoff=10)
    worse = cand(20, 60, "Overlap", hook=2)
    separate = cand(120, 160, "Separate")
    out, warnings = highlights.validate_candidates([worse, good, separate], SEGMENTS, 200, SETTINGS)
    assert [c["title"] for c in out] == ["Best", "Separate"]
    assert any("overlapped" in w for w in warnings)


def test_limits_to_requested_number_ranked_by_score():
    raw = [cand(i * 40, i * 40 + 35, f"c{i}", hook=i + 1) for i in range(5)]
    out, _ = highlights.validate_candidates(raw, SEGMENTS, 200, Settings(num_clips=2, min_seconds=30, max_seconds=60))
    assert [c["title"] for c in out] == ["c4", "c3"]


def test_missing_fields_and_bad_scores_are_tolerated():
    out, _ = highlights.validate_candidates([{"start": 10, "end": 45, "scores": {"hook": "x", "clarity": 99}}],
                                            SEGMENTS, 200, SETTINGS)
    assert out[0]["scores"]["hook"] == 5 and out[0]["scores"]["clarity"] == 10
    assert out[0]["title"]


def test_missing_timestamps_skipped():
    out, warnings = highlights.validate_candidates([{"title": "no times"}], SEGMENTS, 200, SETTINGS)
    assert out == [] and "no valid timestamps" in warnings[0]


@pytest.mark.parametrize("text", ["not json", "{\"foo\": 1}", "[1, 2"])
def test_malformed_responses_raise(text):
    with pytest.raises(ClipperError):
        highlights.parse_response(text)


def test_code_fenced_and_bare_list_responses_parse():
    assert highlights.parse_response("```json\n[]\n```") == {"candidates": [], "notes": ""}
    assert highlights.parse_response('{"candidates": [], "notes": "few"}')["notes"] == "few"


class FakeModels:
    def __init__(self, text=None, error=None):
        self.text, self.error, self.calls = text, error, 0

    def generate_content(self, model, contents, config):
        self.calls += 1
        if self.error:
            raise self.error
        return type("R", (), {"text": self.text})()


class FakeClient:
    def __init__(self, **kw):
        self.models = FakeModels(**kw)


def test_find_highlights_with_mocked_gemini():
    text = json.dumps({"candidates": [cand(10, 50), cand(100, 140, "Second")], "notes": "Only two strong moments."})
    out, notes = highlights.find_highlights(SEGMENTS, 200, SETTINGS, client=FakeClient(text=text))
    assert len(out) == 2
    assert "Only 2 of the 3" in notes and "Only two strong moments." in notes


def test_find_highlights_reports_when_nothing_usable():
    out, notes = highlights.find_highlights(SEGMENTS, 200, SETTINGS,
                                            client=FakeClient(text='{"candidates": [], "notes": "Mostly silence."}'))
    assert out == [] and "didn't find any" in notes


def _api_error(code):
    from google.genai import errors

    return errors.ClientError(code, {"error": {"code": code, "message": f"err {code}", "status": "X"}})


def test_unknown_model_gives_clear_error():
    with pytest.raises(ClipperError, match="isn't available"):
        highlights.call_gemini("p", "gemini-nope", client=FakeClient(error=_api_error(404)))


def test_rate_limit_is_retried(monkeypatch):
    monkeypatch.setattr(highlights.time, "sleep", lambda s: None)
    client = FakeClient(error=_api_error(429))
    with pytest.raises(ClipperError, match="rate limit"):
        highlights.call_gemini("p", "m", client=client)
    assert client.models.calls == 4


def test_missing_api_key(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    with pytest.raises(ClipperError, match="GEMINI_API_KEY"):
        highlights.call_gemini("p", "m")


def test_clip_type_and_hook_text_are_kept_and_cleaned():
    raw = [cand(10, 50), cand(100, 140)]
    raw[0].update(clip_type="fail", hook_text="  Famous   last words...  ")
    raw[1].update(clip_type="made-up", hook_text="x" * 200)
    out, _ = highlights.validate_candidates(raw, SEGMENTS, 200, SETTINGS)
    by_start = {c["start"]: c for c in out}
    assert by_start[10.0]["clip_type"] == "fail" and by_start[10.0]["hook_text"] == "Famous last words..."
    assert by_start[100.0]["clip_type"] == "other" and len(by_start[100.0]["hook_text"]) == highlights.HOOK_TEXT_MAX


def test_prompt_teaches_every_clip_type_and_its_formula():
    seen = {}

    class Recorder(FakeModels):
        def generate_content(self, model, contents, config):
            seen["prompt"] = contents
            return super().generate_content(model, contents, config)

    client = FakeClient(text=json.dumps({"candidates": [cand(10, 50)], "notes": ""}))
    client.models = Recorder(text=client.models.text)
    highlights.find_highlights(SEGMENTS, 200, SETTINGS, client=client)
    for key, (_, angle, formula) in highlights.CLIP_TYPES.items():
        assert f"- {key} (" in seen["prompt"] and angle in seen["prompt"] and formula in seen["prompt"]
    assert '"hook_text"' in seen["prompt"]
