"""Hands-free mode: find new videos, clip them, and post the best clips on a schedule.

One `run()` is a single pass: collect new sources (watched channels + inbox.txt), post whatever is due, clip the
next video in the queue, then post again. Windows Task Scheduler repeats it every 15 minutes; a lock makes
overlapping passes exit at once, so a long render never runs twice.
"""
import json
import re
import shutil
import sys
import threading
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

import requests

from . import pipeline, social, sources, trends
from .config import JOBS_DIR, ROOT, ClipperError, NotReady, Settings, load_env
from .jobs import Job

HOME = JOBS_DIR / "_autopilot"
CONFIG_FILE = ROOT / "autopilot.json"
INBOX_FILE = ROOT / "inbox.txt"
STATE_FILE = HOME / "state.json"
LOG_FILE = HOME / "autopilot.log"
MAX_ATTEMPTS = 3
FEED = "https://www.youtube.com/feeds/videos.xml?channel_id={}"
ATOM, YT = "{http://www.w3.org/2005/Atom}", "{http://www.youtube.com/xml/schemas/2015}"
MEDIA = "{http://search.yahoo.com/mrss/}"
DISCOVER_FILE = HOME / "discover.json"
TIME_FMT = "%Y-%m-%d %H:%M"
INBOX_HELP = ("# One video link (YouTube, TikTok, Instagram, Facebook, X, Twitch, Vimeo, direct .mp4...) or video file "
              "path per line. Autopilot picks them up on its next run.\n")

DEFAULT_CONFIG = {
    "channels": [],
    "platforms": list(social.PLATFORMS),
    "post_times": ["10:00", "15:00", "20:00"],
    "min_score": 70,
    "require_approval": False,
    "videos_per_run": 1,
    "min_source_minutes": 4,
    "keep_source_video": False,
    "youtube_privacy": "public",
    "tiktok_privacy": "SELF_ONLY",
    "min_breakout": 0,             # >0: only clip channel uploads growing this many times faster than usual
    "discover_keywords": [],       # search terms for finding fast-growing videos and channels
    "discover_every_hours": 24,
    "discover_auto_add": 0,        # >0: also queue this many top discovered videos for clipping (opt-in)
    "clip_settings": {"num_clips": 5, "min_seconds": 25, "max_seconds": 60, "framing": "smart",
                      "quality": "standard"},
}

_state_lock = threading.Lock()


def log(message):
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S}  {message}"
    if getattr(sys.stdout, "name", None) != str(LOG_FILE):  # the scheduled task's stdout already is the log
        print(line, flush=True)
    HOME.mkdir(parents=True, exist_ok=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def _now():
    return datetime.now().strftime(TIME_FMT)


def _later(minutes):
    return (datetime.now() + timedelta(minutes=minutes)).strftime(TIME_FMT)


# ---------- config and state ----------

def load_config():
    if not CONFIG_FILE.exists():
        CONFIG_FILE.write_text(json.dumps(DEFAULT_CONFIG, indent=2), encoding="utf-8")
    cfg = {**DEFAULT_CONFIG, **json.loads(CONFIG_FILE.read_text(encoding="utf-8"))}
    unknown = set(cfg["platforms"]) - set(social.PLATFORMS)
    if unknown:
        raise ClipperError(f"Unknown platforms in autopilot.json: {', '.join(unknown)}")
    return cfg


def load_state():
    state = json.loads(STATE_FILE.read_text(encoding="utf-8")) if STATE_FILE.exists() else {}
    for key, empty in (("channels", {}), ("sources", []), ("done", {}), ("posts", [])):
        state.setdefault(key, empty)
    return state


def save_state(state):
    with _state_lock:
        HOME.mkdir(parents=True, exist_ok=True)
        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=1, ensure_ascii=False), encoding="utf-8")
        tmp.replace(STATE_FILE)  # atomic: a crash never loses the record of what was already posted


class Busy(ClipperError):
    pass


class RunLock:
    """Exclusive per-machine lock; the OS releases it if the process dies."""

    def __enter__(self):
        HOME.mkdir(parents=True, exist_ok=True)
        self.f = open(HOME / "run.lock", "a+")
        try:
            if sys.platform == "win32":
                import msvcrt
                msvcrt.locking(self.f.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.f.close()
            raise Busy("Autopilot is busy with a run (probably rendering). Try again in a few minutes.") from None
        return self

    def __exit__(self, *exc):
        self.f.close()


# ---------- sources: watched channels + inbox ----------

def add_source(state, url, origin, priority=None):
    """Queue a video once. priority: breakout ratio (higher is clipped first); None = asked for by you, goes first."""
    url = url.strip()
    if not url or url in state["done"] or any(s["url"] == url for s in state["sources"]):
        return False
    state["sources"].append({"url": url, "origin": origin, "added": _now(), "attempts": 0, "priority": priority})
    return True


def read_inbox(state):
    if not INBOX_FILE.exists():
        INBOX_FILE.write_text(INBOX_HELP, encoding="utf-8")
        return
    lines = INBOX_FILE.read_text(encoding="utf-8").splitlines()
    links = [l.strip() for l in lines if l.strip() and not l.lstrip().startswith("#")]
    if links:
        for link in links:
            before = state["done"].pop(link, None)  # you asked for it again: re-do it with a fresh analysis
            if add_source(state, link, "inbox"):
                state["sources"][-1]["fresh"] = bool(before)
                log(f"Inbox: queued {link}" + (f" (clipped before on {before['at']}; doing it again)" if before else ""))
            else:
                log(f"Inbox: {link} is already in the queue")
        INBOX_FILE.write_text(INBOX_HELP, encoding="utf-8")


def channel_id(url):
    if re.fullmatch(r"UC[\w-]{22}", url):
        return url
    with sources._ydl({"extract_flat": True, "playlistend": 1}) as ydl:
        info = ydl.extract_info(url, download=False, process=False)
    cid = info.get("channel_id") or info.get("id")
    if not (cid and cid.startswith("UC")):
        raise ClipperError(f"Couldn't find the YouTube channel for {url}")
    return cid


def parse_feed(xml_text):
    """[{id, published (epoch), views}] from a channel's RSS feed, newest first. The feed carries view counts,
    so growth can be measured without any API quota."""
    videos = []
    for e in ET.fromstring(xml_text).findall(ATOM + "entry"):
        stats = e.find(f"{MEDIA}group/{MEDIA}community/{MEDIA}statistics")
        published = e.findtext(ATOM + "published")
        videos.append({"id": e.findtext(YT + "videoId"),
                       "published": datetime.fromisoformat(published).timestamp() if published else None,
                       "views": int(stats.get("views")) if stats is not None and stats.get("views") else None})
    return videos


def latest_videos(cid):
    """Newest uploads of a channel: its RSS feed (fast, with views), else yt-dlp, because feeds fail now and then."""
    for _ in range(3):
        try:
            r = requests.get(FEED.format(cid), timeout=30)
            if r.ok:
                return parse_feed(r.text)
        except (requests.RequestException, ET.ParseError):
            pass
        time.sleep(2)
    # the channel's "uploads" playlist (UU...) lists exactly what the feed does, so 'seen' stays consistent
    with sources._ydl({"extract_flat": "in_playlist", "playlistend": 15}) as ydl:
        info = ydl.extract_info(f"https://www.youtube.com/playlist?list=UU{cid[2:]}", download=False)
    return [{"id": e["id"], "published": None, "views": None} for e in info.get("entries") or [] if e.get("id")]


def latest_from_page(url):
    """Newest videos on any profile, channel or playlist page yt-dlp can read (TikTok, Instagram, Twitch, Vimeo...)."""
    def run():
        with sources._ydl({"extract_flat": "in_playlist", "noplaylist": False, "playlistend": 15}) as ydl:
            return ydl.extract_info(url, download=False)

    info = sources._with_retries(run, f"Checking {url}")
    if info.get("_type") != "playlist":
        raise ClipperError(f"{url} is a single video, not a channel or profile. Use: python autopilot.py add \"{url}\"")
    videos = []
    for e in info.get("entries") or []:
        link = e.get("webpage_url") or e.get("url") or ""
        if e.get("id") and link.startswith(("http://", "https://")):
            videos.append({"id": e["id"], "url": link, "published": e.get("timestamp"), "views": e.get("view_count")})
    return videos


def poll_channels(state, cfg, now=None):
    now = now or time.time()
    for url in cfg["channels"]:
        ch = state["channels"].setdefault(url, {})
        try:
            if ch.get("id") or sources.is_youtube(url) or re.fullmatch(r"UC[\w-]{22}", url):
                ch["id"] = ch.get("id") or channel_id(url)
                videos = latest_videos(ch["id"])
            else:
                videos = latest_from_page(url)
        except Exception as e:  # noqa: BLE001 - one unreachable channel mustn't stop the others
            log(f"Couldn't check {url}: {e}")
            continue
        ids = [v["id"] for v in videos]
        if "seen" not in ch:  # first look: only videos uploaded from now on, not the whole back catalogue
            ch["seen"] = ids
            log(f"Watching {url} ({len(ids)} existing videos skipped)")
            continue
        ratios = trends.breakout_ratios(videos, now)
        for v in reversed([v for v in videos if v["id"] not in ch["seen"]]):  # oldest first
            ratio = ratios.get(v["id"])
            age_hours = (now - v["published"]) / 3600 if v["published"] else 99
            if cfg["min_breakout"] and ratio is not None and ratio < cfg["min_breakout"]:
                if age_hours < 24:
                    continue  # too early to judge: look again next pass, without marking it seen
                log(f"{url}: skipped {v['id']} (grew {ratio}x the channel's usual pace in its first day)")
            elif add_source(state, v.get("url") or f"https://www.youtube.com/watch?v={v['id']}", "channel", ratio or 0):
                log(f"{url}: queued {v['id']}" + (f" (growing {ratio}x the channel's usual pace)" if ratio else ""))
            ch["seen"].insert(0, v["id"])
        ch["seen"] = ch["seen"][:100]


def maybe_discover(state, cfg):
    """Every discover_every_hours: search the keywords, save the ranking, and optionally queue the top videos."""
    last = state.setdefault("discover", {}).get("at", "")
    if not cfg["discover_keywords"] or (last and last > _later(-60 * cfg["discover_every_hours"])):
        return None
    result = discover(cfg["discover_keywords"])
    state["discover"]["at"] = _now()
    added = [v for v in result["videos"][: cfg["discover_auto_add"]] if add_source(state, v["url"], "discover")]
    log(f"Discovery: {len(result['videos'])} fast-growing videos found, {len(added)} queued "
        f"(see {DISCOVER_FILE.name} or run: python autopilot.py discover)")
    return result


def discover(keywords):
    result = trends.discover(keywords)
    result["at"] = _now()
    HOME.mkdir(parents=True, exist_ok=True)
    DISCOVER_FILE.write_text(json.dumps(result, indent=1, ensure_ascii=False), encoding="utf-8")
    return result


# ---------- clipping ----------

def _clip_source(src, cfg, settings):
    """Analyse and render one source; returns (job, rendered clips worth posting)."""
    url = src["url"]
    if url.startswith(("http://", "https://")):
        info = sources.probe_url(url)
        if src["origin"] == "channel" and (info.get("duration") or 0) < cfg["min_source_minutes"] * 60:
            return None, []  # Shorts and trailers have nothing to clip
        job, info = pipeline.open_url(url, info=info)
    else:
        job, info = pipeline.open_file(url), None
    if src.get("fresh") or not job.data.get("candidates"):  # analysed before: reuse it, unless you asked again
        pipeline.analyze(job, settings, info)
    for c in job.data["candidates"]:
        c["selected"] = c["score"] >= cfg["min_score"]
    job.save()
    picks = [c for c in job.data["candidates"] if c["selected"]]
    if picks:
        try:
            pipeline.render(job, settings)
        except ClipperError as e:  # some clips failed; post the ones that worked
            log(f"{job.id}: {e}")
    if not cfg["keep_source_video"]:
        for folder in ("video", "sections"):
            shutil.rmtree(job.dir / folder, ignore_errors=True)
    return job, [c for c in picks if c.get("render", {}).get("status") == "done"]


def next_source(state, now):
    """Videos you asked for first, then the fastest-growing: the moment that's about to blow up is clipped first."""
    ready = [s for s in state["sources"] if s.get("wait_until", "") <= now]
    return max(ready, key=lambda s: (s.get("priority") is None, s.get("priority") or 0), default=None)


def process_sources(state, cfg):
    settings = Settings.from_dict(cfg["clip_settings"]).validate()
    for _ in range(cfg["videos_per_run"]):
        now = _now()
        src = next_source(state, now)
        if not src:
            return
        log(f"Clipping {src['url']}")
        try:
            job, clips = _clip_source(src, cfg, settings)
        except NotReady as e:
            src["wait_until"] = _later(60)
            log(f"Not ready yet, retrying in an hour: {e}")
            save_state(state)
            continue
        except Exception as e:  # noqa: BLE001 - one bad video must never stop the autopilot
            src["attempts"] += 1
            log(f"Failed ({src['attempts']}/{MAX_ATTEMPTS}): {e}")
            if src["attempts"] >= MAX_ATTEMPTS:
                state["sources"].remove(src)
                state["done"][src["url"]] = {"status": "failed", "error": str(e), "at": now}
            else:
                src["wait_until"] = _later(30 * src["attempts"])
            save_state(state)
            continue
        state["sources"].remove(src)
        state["done"][src["url"]] = {"status": "clipped" if job else "skipped (too short)",
                                     "job": job.id if job else None, "clips": len(clips), "at": now}
        if job:
            queued = enqueue(state, cfg, job, clips)
            log(f"{job.id}: {len(clips)} clip(s) rendered, {queued} post(s) queued")
        save_state(state)


# ---------- scheduling ----------

def next_slots(after, times, n):
    """The next n posting times strictly after `after`, from the daily HH:MM list."""
    daily = sorted(datetime.strptime(t, "%H:%M").time() for t in times)
    slots, day = [], after.date()
    while len(slots) < n:
        slots += [dt for dt in (datetime.combine(day, t) for t in daily) if dt > after][: n - len(slots)]
        day += timedelta(days=1)
    return [s.strftime(TIME_FMT) for s in slots]


def _last_slot(state):
    due = [p["due"] for p in state["posts"] if p["status"] == "scheduled"]
    return max([datetime.now()] + [datetime.strptime(d, TIME_FMT) for d in due])


def _schedule(state, cfg, groups):
    """Give each clip (a list of its per-platform posts) the next free slot; all platforms share it."""
    for slot, posts in zip(next_slots(_last_slot(state), cfg["post_times"], len(groups)), groups):
        for p in posts:
            p.update(status="scheduled", due=slot)


def enqueue(state, cfg, job, clips):
    platforms = [p for p in cfg["platforms"] if p in social.connected()]
    if not platforms:
        log("No platforms connected, so nothing will be posted. Run: python autopilot.py connect youtube")
        return 0
    existing = {p["id"] for p in state["posts"]}
    groups = []
    for c in sorted(clips, key=lambda c: -c["score"]):  # best clip goes out first
        posts = [{"id": f"{job.id}/{c['id']}/{p}", "job": job.id, "clip": c["id"], "platform": p,
                  "status": "approval", "due": "", "attempts": 0}
                 for p in platforms if f"{job.id}/{c['id']}/{p}" not in existing]
        if posts:
            state["posts"] += posts
            groups.append(posts)
    if not cfg["require_approval"]:
        _schedule(state, cfg, groups)
    return sum(map(len, groups))


def _matching(state, status, target):
    """Posts with this status whose id starts with target ('all', 'job', 'job/c2' or 'job/c2/tiktok')."""
    return [p for p in state["posts"] if p["status"] == status and (target == "all" or p["id"].startswith(target))]


def approve(state, cfg, target):
    groups = {}
    for p in _matching(state, "approval", target):
        groups.setdefault((p["job"], p["clip"]), []).append(p)
    _schedule(state, cfg, list(groups.values()))
    return sum(map(len, groups.values()))


def skip(state, target):
    posts = _matching(state, "approval", target) + _matching(state, "scheduled", target)
    for p in posts:
        p["status"] = "skipped"
    return len(posts)


def retry(state, cfg, target):
    """Failed posts go back in the queue at the next free slots."""
    groups = {}
    for p in _matching(state, "failed", target):
        p.update(attempts=0, error=None, retry_at="")
        groups.setdefault((p["job"], p["clip"]), []).append(p)
    _schedule(state, cfg, list(groups.values()))
    return sum(map(len, groups.values()))


# ---------- posting ----------

def _post_one(state, cfg, p):
    job = Job(p["job"])
    clip = job.candidate(p["clip"])
    path = job.dir / (clip.get("render") or {}).get("file", "missing")
    if not path.exists():
        p.update(status="failed", error="The rendered clip file is missing.")
        save_state(state)
        return
    p["status"] = "posting"  # saved first: if the PC dies mid-upload we never blindly post it again
    save_state(state)
    try:
        p.update(status="posted", url=social.post(p["platform"], path, clip, cfg), posted=_now(), error=None)
        log(f"Posted {p['id']} -> {p['url']}")
    except social.NotConnected as e:
        p.update(status="failed", error=str(e))
        log(f"{p['id']}: {e}")
    except Exception as e:  # noqa: BLE001
        p["attempts"] += 1
        if p["attempts"] >= MAX_ATTEMPTS:
            p.update(status="failed", error=str(e))
        else:
            p.update(status="scheduled", error=str(e), retry_at=_later(20 * p["attempts"]))
        log(f"{p['id']} failed ({p['attempts']}/{MAX_ATTEMPTS}): {e}")
    save_state(state)


def post_due(state, cfg):
    now = _now()
    due = [p for p in state["posts"]
           if p["status"] == "scheduled" and p["due"] <= now and p.get("retry_at", "") <= now]
    by_platform = {}
    for p in sorted(due, key=lambda p: p["due"]):
        by_platform.setdefault(p["platform"], []).append(p)
    if not by_platform:
        return

    def worker(posts):  # one thread per platform: platforms upload in parallel, each one in order
        for p in posts:
            _post_one(state, cfg, p)

    with ThreadPoolExecutor(len(by_platform)) as ex:
        list(ex.map(worker, by_platform.values()))


def run():
    load_env()
    with RunLock():
        cfg, state = load_config(), load_state()
        for p in state["posts"]:
            if p["status"] == "posting":  # a previous run died mid-upload
                p.update(status="failed", error="Interrupted during upload. Check the platform; if the post "
                                                "isn't there, run: python autopilot.py retry " + p["id"])
        read_inbox(state)
        poll_channels(state, cfg)
        try:
            maybe_discover(state, cfg)
        except Exception as e:  # noqa: BLE001 - discovery is extra; clipping and posting must go on
            log(f"Discovery failed: {e}")
        save_state(state)
        post_due(state, cfg)
        process_sources(state, cfg)
        post_due(state, cfg)  # clipping can take a while; catch slots that came due meanwhile
