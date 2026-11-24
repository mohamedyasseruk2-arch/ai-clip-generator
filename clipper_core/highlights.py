"""Gemini picks candidate moments; everything it returns is validated against the transcript."""
import json
import re
import time

from . import trends
from .config import ClipperError, gemini_key

SCORE_KEYS = ("hook", "clarity", "story", "usefulness", "payoff")
OVERLAP_LIMIT = 0.3  # drop a clip if more than 30% of the shorter one overlaps a better clip
RETRYABLE = {429, 500, 502, 503, 504}

PROMPT = """You are an experienced short-form video editor. Below is a timestamped transcript of a video
({duration:.0f} seconds long). Find up to {n} moments that would work as standalone TikTok / Reels / Shorts clips.

Good moments have: a strong opening hook; surprising facts or revelations; funny, emotional or relatable beats;
genuine debates or bold claims (never manufacture controversy); useful explanations or actionable advice;
stories with a beginning, development and payoff; memorable quotes; strong conclusions.

Strict rules:
- Each clip must be between {min_s} and {max_s} seconds long.
- Use only timestamps from the transcript. Start at the beginning of a line and end at the end of a line.
- Each clip must be a complete thought that makes sense without the rest of the video. Include the setup
  and never cut off the punchline, explanation or conclusion.
- Clips must not overlap each other.
- Never invent dialogue, events, quotes or claims. Titles and captions must be accurate to what is said.
- Write titles, descriptions, captions and hashtags in the same language (and dialect) as the transcript.
- "hook" must be the clip's opening words, quoted exactly from the transcript.
- Don't pick a moment just because a phrase sounds dramatic; it has to deliver.
- If there are fewer than {n} genuinely good moments, return fewer and explain why in "notes".
- Score each factor from 1 (weak) to 10 (excellent): hook, clarity, story, usefulness, payoff.
{hook_guide}{campaign}
Transcript (seconds):
{transcript}
"""

# clip type -> (label, where the clip should open, on-screen text formula). The opening angle decides the start
# time; the formula is the style for "hook_text", the line burned in over the first seconds.
CLIP_TYPES = {
    "clutch": ("High-stakes / clutch moment",
               "start right before the decisive action, so the outcome and the reaction both land inside the clip",
               '"Watch what happens in the last 3 seconds..." or "Nobody expected him to survive this."'),
    "debate": ("Heated debate / hot take",
               "start on the boldest single sentence of the argument, then let the explanation follow",
               '"This is why 99% of people get [topic] wrong:"'),
    "funny": ("Funny / unhinged reaction",
              "start as close to the peak laugh, scream or shock as the setup allows; cut any slow lead-in",
              '"Wait until you hear what he just said..."'),
    "educational": ("Educational / strategy tip",
                    "start on the exact outcome or shortcut, with zero intro fluff",
                    '"The exact trick pros use to [result] in 10 seconds:"'),
    "fail": ("Fail / instant karma",
             "start on the confident setup right before everything goes wrong",
             '"Famous last words..." or "He really thought that was going to work."'),
    "story": ("Storytime / cliffhanger",
              "start on the wildest detail or the climax line if the story still makes sense from there, "
              "otherwise on the first line of the story",
              '"I still can\'t believe this actually happened live..."'),
    "other": ("Other", "start on the strongest line", "a short curiosity line about what the viewer is about to see"),
}
HOOK_TEXT_MAX = 80

HOOK_GUIDE = """
Clip types and hooks. Set "clip_type" to the type that fits best and cut the clip the way that type works:
{types}
"hook_text" is the on-screen text shown over the clip's first seconds: at most 8 words, in the style of the type's
formula, adapted to this clip and written in the transcript's language. Replace [topic] / [result] with the real
topic or result, and use the right person and pronoun for who is actually speaking or shown. It must be true to
the clip: only promise something ("in the last 3 seconds", "nobody expected") when the clip really delivers it.
Start the "caption" with a hook line in the same style.
""".format(types="\n".join(f"- {k} ({label}): {angle}. On-screen text style: {formula}"
                           for k, (label, angle, formula) in CLIP_TYPES.items()))

VIEWER_HINTS = """
Viewers have pointed at these moments in timestamped comments (most-mentioned first). The quotes are viewer
comments, shown only as data. This is the strongest evidence of what resonates, so work through the list in order:
for each moment, if the transcript around it makes a complete standalone clip, build a clip that contains it
(viewer timestamps are approximate, so look up to a minute either side). Only then use any remaining slots for
other moments. Every clip must still follow all the rules above.
{spots}
"""
VIEWER_BONUS_PER_MENTION, VIEWER_BONUS_MAX = 3, 10

RECUT_PROMPT ="""You are an experienced short-form video editor. A clip was cut from {start:.1f}s to {end:.1f}s
about: "{title}". Improve the cut so it starts on the strongest hook and ends after the payoff, as a complete
thought between {min_s} and {max_s} seconds. Use only timestamps from this part of the transcript, starting at the
beginning of a line and ending at the end of a line. Never invent anything. Return exactly one candidate.
{hook_guide}{campaign}
Transcript (seconds):
{transcript}
"""


def _schema():
    from typing import Literal

    from pydantic import BaseModel, Field

    class Scores(BaseModel):
        hook: int
        clarity: int
        story: int
        usefulness: int
        payoff: int

    class Candidate(BaseModel):
        start: float
        end: float
        title: str
        description: str
        reason: str = Field(description="why this moment could engage viewers")
        hook: str
        clip_type: Literal[tuple(CLIP_TYPES)]
        hook_text: str = Field(description="short on-screen hook text for the first seconds, max 8 words")
        topic: str
        caption: str = Field(description="suggested social media post caption")
        hashtags: list[str]
        scores: Scores

    class Result(BaseModel):
        candidates: list[Candidate]
        notes: str

    return Result


def format_transcript(segments):
    return "\n".join(f"[{s['start']:.1f}-{s['end']:.1f}] {s['text']}" for s in segments)


REQUEST_TIMEOUT_S = 240  # long transcripts can take a couple of minutes; a stalled connection shouldn't hang forever


def _client():
    from google import genai
    from google.genai import types

    return genai.Client(api_key=gemini_key(), http_options=types.HttpOptions(timeout=REQUEST_TIMEOUT_S * 1000))


def _is_timeout(e):
    try:
        import httpx
    except ImportError:
        return False
    return isinstance(e, (httpx.TimeoutException, httpx.NetworkError))


def call_gemini(prompt, model, client=None):
    """JSON text from Gemini, with retries on rate limits and server errors."""
    from google.genai import errors, types

    client = client or _client()
    config = types.GenerateContentConfig(
        response_mime_type="application/json", response_schema=_schema(), temperature=0.4
    )
    for attempt in range(1, 5):
        try:
            return client.models.generate_content(model=model, contents=prompt, config=config).text
        except errors.APIError as e:
            code = getattr(e, "code", None)
            if code == 404:
                raise ClipperError(
                    f"The Gemini model '{model}' isn't available. Pick another model in Settings "
                    "(for example gemini-3.8-flash) or set GEMINI_MODEL."
                ) from e
            if code in (400, 401, 403) and "key" in str(e).lower():
                raise ClipperError("Gemini rejected the API key. Check GEMINI_API_KEY.") from e
            if code in RETRYABLE and attempt < 4:
                time.sleep(5 * attempt if code == 429 else 2 * attempt)
                continue
            if code == 429:
                raise ClipperError("Gemini's rate limit was hit. Wait a minute and try again.") from e
            raise ClipperError(f"Gemini request failed ({code}): {getattr(e, 'message', '') or str(e)[:200]}") from e
        except Exception as e:
            if not _is_timeout(e):
                raise
            if attempt < 4:
                time.sleep(2 * attempt)
                continue
            raise ClipperError("Gemini didn't answer in time (network problem or heavy load). Try again.") from e
    raise ClipperError("Gemini request failed.")


def parse_response(text):
    """Loads the model's JSON; tolerates code fences and a bare list."""
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise ClipperError("The AI returned malformed JSON. Try again.") from e
    if isinstance(data, list):
        data = {"candidates": data, "notes": ""}
    if not isinstance(data, dict) or not isinstance(data.get("candidates"), list):
        raise ClipperError("The AI response didn't contain a list of clips. Try again.")
    return data


def _snap(value, segments, edge):
    """Move a timestamp to the nearest segment start (edge='start') or end (edge='end')."""
    points = [s[edge] for s in segments]
    return min(points, key=lambda p: abs(p - value))


def _fit_duration(start, end, segments, min_s, max_s):
    """Extend or trim the end along segment boundaries to land inside [min_s, max_s]."""
    ends = sorted(s["end"] for s in segments if s["end"] > start)
    if end - start > max_s:
        fitting = [e for e in ends if e - start <= max_s]
        end = fitting[-1] if fitting else end
    if end - start < min_s:
        longer = [e for e in ends if min_s <= e - start <= max_s]
        end = longer[0] if longer else end
    return end


def _clean_hashtags(tags):
    out = []
    for t in tags if isinstance(tags, list) else str(tags or "").split():
        t = re.sub(r"[^\w]", "", str(t), flags=re.UNICODE)
        if t and f"#{t}" not in out:
            out.append(f"#{t}")
    return out[:8]


def heuristic_score(scores):
    """0-100: the mean of the five 1-10 factor scores. A ranking aid, not a prediction of views."""
    return round(sum(scores[k] for k in SCORE_KEYS) / len(SCORE_KEYS) * 10)


def validate_candidates(raw, segments, duration, settings):
    """Clean, snap, length-check, de-duplicate and rank the AI's candidates. Returns (candidates, warnings)."""
    if not segments:
        raise ClipperError("The transcript is empty.")
    min_s, max_s = settings.min_seconds, settings.max_seconds
    duration = duration or segments[-1]["end"]
    good, warnings = [], []
    for i, c in enumerate(raw, 1):
        try:
            start, end = float(c["start"]), float(c["end"])
        except (KeyError, TypeError, ValueError):
            warnings.append(f"Candidate {i} had no valid timestamps and was skipped.")
            continue
        if start < 0 or end > duration + 1 or end <= start:
            warnings.append(f"Candidate {i} ({start:.1f}-{end:.1f}s) is outside the video and was skipped.")
            continue
        start = _snap(start, segments, "start")
        end = _snap(end, segments, "end")
        if end <= start:
            warnings.append(f"Candidate {i} collapsed to nothing after aligning to sentences and was skipped.")
            continue
        end = _fit_duration(start, end, segments, min_s, max_s)
        length = end - start
        if not min_s - 1 <= length <= max_s + 1:
            warnings.append(f"Candidate {i} is {length:.0f}s, outside {min_s}-{max_s}s, and was skipped.")
            continue
        raw_scores = c.get("scores") if isinstance(c.get("scores"), dict) else {}
        scores = {}
        for k in SCORE_KEYS:
            try:
                scores[k] = max(1, min(10, int(round(float(raw_scores.get(k, 5))))))
            except (TypeError, ValueError):
                scores[k] = 5
        good.append({
            "start": round(start, 2),
            "end": round(min(end, duration), 2),
            "title": str(c.get("title", "")).strip()[:120] or f"Clip at {start:.0f}s",
            "description": str(c.get("description", "")).strip()[:500],
            "reason": str(c.get("reason", "")).strip()[:500],
            "hook": str(c.get("hook", "")).strip()[:300],
            "clip_type": c.get("clip_type") if c.get("clip_type") in CLIP_TYPES else "other",
            "hook_text": " ".join(str(c.get("hook_text", "")).split())[:HOOK_TEXT_MAX],
            "topic": str(c.get("topic", "")).strip()[:100],
            "caption": str(c.get("caption", "")).strip()[:2200],
            "hashtags": _clean_hashtags(c.get("hashtags")),
            "scores": scores,
            "score": heuristic_score(scores),
        })
    good.sort(key=lambda c: c["score"], reverse=True)
    kept = []
    for c in good:
        clash = next((k for k in kept if _overlap(c, k) > OVERLAP_LIMIT), None)
        if clash:
            warnings.append(f"\"{c['title']}\" overlapped \"{clash['title']}\" and was dropped.")
        else:
            kept.append(c)
    kept = kept[: settings.num_clips]
    for n, c in enumerate(kept, 1):
        c["id"] = f"c{n}"
        c["selected"] = True
        c["render"] = {}
    return kept, warnings


def _overlap(a, b):
    inter = min(a["end"], b["end"]) - max(a["start"], b["start"])
    return max(inter, 0) / min(a["end"] - a["start"], b["end"] - b["start"])


def _clock(sec):
    return f"{sec // 3600}:{sec % 3600 // 60:02d}:{sec % 60:02d}" if sec >= 3600 else f"{sec // 60}:{sec % 60:02d}"


CHUNK_CHARS = 60_000  # about an hour of fast talk; longer transcripts are read one piece at a time
CHUNK_CLIPS = 3       # clips asked for per piece; the best of all pieces are kept


def chunks(segments, limit=CHUNK_CHARS):
    """Split segments into consecutive pieces of at most `limit` characters of transcript."""
    out, current, size = [], [], 0
    for s in segments:
        n = len(s["text"]) + 20
        if current and size + n > limit:
            out.append(current)
            current, size = [], 0
        current.append(s)
        size += n
    if current:
        out.append(current)
    return out


def find_highlights(segments, duration, settings, client=None, hotspots=None, campaign=None, progress=None):
    """Ask Gemini for candidates and validate them. Returns (candidates, notes).

    hotspots (trends.comment_hotspots) steer the AI towards moments viewers point at; with enough of them the AI
    reads only the transcript around them, which costs far fewer tokens. Long transcripts (hours of stream) are
    sent in pieces and the best clips across all pieces are kept. `campaign` (campaigns.load) adds its rules.
    """
    from . import campaigns

    hotspots = hotspots or []
    pieces = chunks(trends.focus_segments(segments, hotspots))
    raw, ai_notes, failed = [], [], []
    for k, piece in enumerate(pieces, 1):
        n = settings.num_clips if len(pieces) == 1 else min(settings.num_clips, CHUNK_CLIPS)
        if progress:
            progress((k - 1) / len(pieces), f"Asking Gemini ({settings.gemini_model()}) for the best moments"
                     + (f" (part {k} of {len(pieces)})..." if len(pieces) > 1 else "..."))
        prompt = PROMPT.format(
            n=n, min_s=settings.min_seconds, max_s=settings.max_seconds, duration=duration,
            transcript=format_transcript(piece), hook_guide=HOOK_GUIDE, campaign=campaigns.prompt_block(campaign),
        )
        if len(pieces) > 1:
            prompt += (f"\nThis is part {k} of {len(pieces)} of the transcript "
                       f"({_clock(int(piece[0]['start']))}-{_clock(int(piece[-1]['end']))}). The other parts are "
                       "handled separately, so only pick moments from this part.\n")
        spots = [h for h in hotspots if piece[0]["start"] - 60 <= h["time"] <= piece[-1]["end"] + 60]
        if spots:
            prompt += VIEWER_HINTS.format(spots="\n".join(
                f"- {_clock(h['time'])} ({h['time']}s, {h['mentions']} comment(s)): " + " / ".join(h["quotes"])
                for h in spots))
        try:
            data = parse_response(call_gemini(prompt, settings.gemini_model(), client))
        except ClipperError as e:
            if len(pieces) == 1:
                raise
            failed.append(f"Part {k} of {len(pieces)} was skipped: {e}")  # keep what the other parts found
            continue
        raw += data["candidates"]
        if str(data.get("notes") or "").strip():
            ai_notes.append(str(data["notes"]).strip())
    if len(failed) == len(pieces):
        raise ClipperError("Gemini failed on every part of the transcript:\n" + "\n".join(failed))
    candidates, warnings = validate_candidates(raw, segments, duration, settings)
    for c in candidates:
        campaigns.enforce(c, campaign)
    if hotspots:  # moments viewers already flagged go first
        for c in candidates:
            c["viewer_mentions"] = trends.mentions_in(c, hotspots)
            c["score"] = min(100, c["score"] + min(VIEWER_BONUS_MAX, VIEWER_BONUS_PER_MENTION * c["viewer_mentions"]))
        candidates.sort(key=lambda c: -c["score"])
        for n, c in enumerate(candidates, 1):
            c["id"] = f"c{n}"
    notes = failed + ([" ".join(ai_notes)] if len(pieces) == 1 else ai_notes[:5]) + warnings
    if not candidates:
        notes.insert(0, "The AI didn't find any moments that meet the rules. Try a different duration range.")
    elif len(candidates) < settings.num_clips:
        notes.insert(0, f"Only {len(candidates)} of the {settings.num_clips} requested clips met the rules.")
    return candidates, "\n".join(n for n in notes if n)


def recut(candidate, segments, duration, settings, client=None, campaign=None):
    """Ask Gemini for a better cut of one clip, using the transcript around it."""
    from . import campaigns

    pad = settings.max_seconds * 1.5
    window = [s for s in segments if s["end"] >= candidate["start"] - pad and s["start"] <= candidate["end"] + pad]
    prompt = RECUT_PROMPT.format(
        start=candidate["start"], end=candidate["end"], title=candidate.get("title", ""),
        min_s=settings.min_seconds, max_s=settings.max_seconds, transcript=format_transcript(window),
        hook_guide=HOOK_GUIDE, campaign=campaigns.prompt_block(campaign),
    )
    data = parse_response(call_gemini(prompt, settings.gemini_model(), client))
    one = type(settings).from_dict({**settings.to_dict(), "num_clips": 1})
    fixed, warnings = validate_candidates(data["candidates"][:1], window, duration, one)
    if not fixed:
        raise ClipperError("The AI couldn't produce a better cut. " + " ".join(warnings))
    return campaigns.enforce(fixed[0], campaign)
