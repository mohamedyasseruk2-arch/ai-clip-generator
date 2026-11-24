"""Signals for choosing what to clip: how fast a video is growing, and which moments viewers point at.

Everything here uses public pages (channel feeds, search, comments) through yt-dlp, so it costs no API quota.
"""
import math
import re
import statistics
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote

from .config import ClipperError

TIMESTAMP = re.compile(r"(?<![\d:])(?:(\d{1,2}):)?(\d{1,2}):(\d{2})(?![\d:])")
BIDI_MARKS = re.compile("[‎‏‪-‮⁦-⁩]")  # invisible direction marks in RTL comments
HOTSPOT_MERGE = 45  # seconds: mentions this close together point at the same moment
FOCUS_WINDOW = 240   # transcript seconds kept either side of a hotspot when the AI only gets the hot parts
MIN_RELIABLE_FOLLOWERS = 1000
FOCUS_MIN_SPOTS = 3 # fewer hotspots than this: too little evidence, so the AI reads the whole transcript
# uploaded this week and longer than 20 minutes: fresh, clippable material
SEARCH_URL = "https://www.youtube.com/results?search_query={}&sp=EgQIAxgC"


# ---------- what viewers point at ----------

def parse_timestamps(text, duration):
    """Seconds for every m:ss / h:mm:ss in text that fits inside the video."""
    out = []
    for h, m, s in TIMESTAMP.findall(text):
        if int(s) >= 60 or (h and int(m) >= 60):
            continue
        sec = int(h or 0) * 3600 + int(m) * 60 + int(s)
        if 0 < sec <= duration:
            out.append(sec)
    return out


def comment_hotspots(comments, duration, top=8):
    """Moments viewers timestamp in comments, merged and ranked by mentions weighted by likes."""
    points = []
    for c in comments:
        text = c.get("text") or ""
        times = set(parse_timestamps(text, duration))
        if not times or len(TIMESTAMP.findall(text)) > 3:  # a list of chapters isn't a vote for one moment
            continue
        weight = 1 + math.log10(1 + (c.get("like_count") or 0))
        quote_text = re.sub(r"\s+", " ", BIDI_MARKS.sub("", TIMESTAMP.sub("", text))).strip()[:80]
        points += [(t, weight, quote_text) for t in times]
    spots = []
    for t, weight, quote_text in sorted(points):
        if spots and t - spots[-1]["times"][-1] <= HOTSPOT_MERGE:
            spot = spots[-1]
        else:
            spot = {"times": [], "weight": 0.0, "quotes": []}
            spots.append(spot)
        spot["times"].append(t)
        spot["weight"] += weight
        if quote_text and len(spot["quotes"]) < 2:
            spot["quotes"].append(quote_text)
    return sorted(({"time": round(statistics.median(s["times"])), "mentions": len(s["times"]),
                    "weight": round(s["weight"], 1), "quotes": s["quotes"]} for s in spots),
                  key=lambda s: -s["weight"])[:top]


def focus_segments(segments, hotspots):
    """Only the transcript around viewer hotspots (far fewer tokens); the whole transcript if evidence is thin."""
    if len(hotspots) < FOCUS_MIN_SPOTS:
        return segments
    kept = [s for s in segments if any(abs(s["start"] - h["time"]) <= FOCUS_WINDOW for h in hotspots)]
    return kept or segments


def mentions_in(clip, hotspots, slack=5):
    return sum(h["mentions"] for h in hotspots if clip["start"] - slack <= h["time"] <= clip["end"] + slack)


# ---------- how fast videos grow ----------

def views_per_hour(views, published, now=None):
    return (views or 0) / max(((now or time.time()) - published) / 3600, 1)


def breakout_ratios(videos, now=None):
    """id -> this video's views/hour divided by the channel's median, from one channel's feed.

    Above 1 means faster than the channel's usual pace. New uploads are compared with older ones whose pace has
    settled, so treat it as a ranking signal, not a forecast.
    """
    rated = {v["id"]: views_per_hour(v["views"], v["published"], now)
             for v in videos if v.get("views") is not None and v.get("published")}
    if not rated:
        return {}
    base = statistics.median(rated.values()) or 1
    return {vid: round(vph / base, 2) for vid, vph in rated.items()}


def discover(keywords, per_keyword=5, now=None):
    """Fast-growing long videos from this week for each keyword, best first, plus the channels behind them.

    Each keyword gets its own share of candidates, so a niche or non-English keyword isn't drowned out by
    giant channels from another.
    """
    from . import sources

    found, errors = {}, []
    for kw in keywords:
        try:
            results = sorted(sources.search(SEARCH_URL.format(quote(kw))), key=lambda e: -e["views"])
        except ClipperError as e:
            errors.append(f"{kw}: {e}")
            continue
        for e in [e for e in results if e["id"] not in found][:per_keyword]:
            found[e["id"]] = {**e, "keyword": kw}
    candidates = list(found.values())

    def rate(e):  # upload time and subscribers aren't in search results: one quick request per candidate
        try:
            info = sources.probe_meta(e["url"])
        except ClipperError:
            return None
        if not info.get("timestamp"):
            return None
        views = info.get("view_count") or e["views"]
        followers = info.get("channel_follower_count") or 0
        vph = views_per_hour(views, info["timestamp"], now)
        # collaboration uploads can report a guest channel's tiny count: treat that as unknown
        reach = round(views / followers, 2) if followers >= MIN_RELIABLE_FOLLOWERS else None
        return {**e, "views": views, "followers": followers, "views_per_hour": round(vph), "reach": reach,
                "channel_url": e["channel_url"] or info.get("channel_url") or "",
                "age_hours": round(((now or time.time()) - info["timestamp"]) / 3600),
                # pace, boosted when the video travels beyond the channel's own subscribers
                "momentum": round(vph * (1 + min(reach if reach is not None else 1, 4)))}

    with ThreadPoolExecutor(4) as ex:
        videos = sorted((v for v in ex.map(rate, candidates) if v), key=lambda v: -v["momentum"])
    channels = {}
    for v in videos:
        ch = channels.setdefault(v["channel_url"] or v["channel"], {
            "channel": v["channel"], "url": v["channel_url"], "followers": v["followers"], "hits": 0, "best": 0})
        ch["hits"] += 1
        ch["best"] = max(ch["best"], v["momentum"])
        if v["followers"] > ch["followers"]:  # a collaboration upload can carry the guest's name and count
            ch.update(channel=v["channel"], followers=v["followers"])
    return {"videos": videos, "channels": sorted(channels.values(), key=lambda c: (-c["hits"], -c["best"])),
            "errors": errors}
