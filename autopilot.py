"""
Autopilot: watch channels and an inbox, clip new videos, and post the best clips to social media on a schedule.

Usage:
    python autopilot.py connect youtube|tiktok|meta     # once per account (meta = Instagram + Facebook)
    python autopilot.py add "<video link from any site>"   # queue a video (or drop links in inbox.txt)
    python autopilot.py watch "https://www.youtube.com/@channel"     # clip every new upload of a channel
                                                        # (also TikTok/Instagram/Twitch/Vimeo profiles, playlists)
    python autopilot.py run                             # one pass (Task Scheduler runs this every 15 min)
    python autopilot.py install                         # create that scheduled task
    python autopilot.py status
    python autopilot.py approve all|<job>[/<clip>]      # when require_approval is on
    python autopilot.py skip <job>[/<clip>[/<platform>]]
    python autopilot.py retry all|<post id>             # re-queue failed posts
    python autopilot.py discover podcast "بودكاست"      # find this week's fastest-growing videos and channels
Settings live in autopilot.json (created on first use).
"""
import argparse
import json
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

from clipper_core import autopilot as ap
from clipper_core import social, sources
from clipper_core.config import ROOT, ClipperError, load_env

TASK_NAME = "CLIPPER Autopilot"


def install(minutes):
    """Scheduled task that runs even on battery and never starts a second copy while one is running."""
    exe = Path(sys.executable).with_name("pythonw.exe")  # no console window popping up every 15 minutes
    exe = exe if exe.exists() else Path(sys.executable)
    script = (
        f"$a = New-ScheduledTaskAction -Execute '{exe}' -Argument '\"{ROOT / 'autopilot.py'}\" run' "
        f"-WorkingDirectory '{ROOT}';"
        f"$t = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes {minutes});"
        "$s = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable "
        "-MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 6);"
        f"Register-ScheduledTask -TaskName '{TASK_NAME}' -Action $a -Trigger $t -Settings $s -Force | Out-Null"
    )
    subprocess.run(["powershell", "-NoProfile", "-Command", script], check=True)
    print(f"Scheduled '{TASK_NAME}' every {minutes} minutes. Log: {ap.LOG_FILE}")


def status():
    cfg, state = ap.load_config(), ap.load_state()
    linked = social.connected()
    print("Posting to:")
    for p in cfg["platforms"]:
        if p not in linked:
            print(f"   {p:10} ✗ not connected (python autopilot.py connect {'meta' if p in ('instagram', 'facebook') else p})")
        else:
            print(f"   {p:10} ✓ {social.account_name(p) or '(connected; reconnect to show the account name)'}")
    print("Watching:", ", ".join(cfg["channels"]) or "no channels")
    print(f"Queue: {len(state['sources'])} video(s) waiting to be clipped")
    for s in state["sources"]:
        print(f"   {s['url']}  ({s['origin']}{', attempt ' + str(s['attempts']) if s['attempts'] else ''})")
    print("Posts:", ", ".join(f"{n} {s}" for s, n in Counter(p["status"] for p in state["posts"]).items()) or "none")
    upcoming = sorted((p for p in state["posts"] if p["status"] in ("scheduled", "approval")),
                      key=lambda p: p["due"] or "~")
    for p in upcoming[:20]:
        print(f"   {p['due'] or 'needs approval':16}  {p['id']}")
    for p in [p for p in state["posts"] if p["status"] == "failed"][-10:]:
        print(f"   FAILED {p['id']}: {p.get('error')}")


def show_discovery(keywords):
    if not keywords:
        sys.exit('Give search keywords, e.g.: python autopilot.py discover podcast "بودكاست"\n'
                 'or set "discover_keywords" in autopilot.json.')
    print(f"Searching this week's long videos for: {', '.join(keywords)} ...")
    result = ap.discover(keywords)
    for err in result["errors"]:
        print("   search failed:", err)
    print("\nFastest-growing videos (momentum = views per hour, boosted when a video reaches beyond its subscribers):")
    for n, v in enumerate(result["videos"], 1):
        reach = f"{v['reach']}x" if v["reach"] is not None else "?"
        print(f"{n:2}. {v['momentum']:>9,}  {v['views_per_hour']:>7,} views/h  {reach:>7} subs  "
              f"{v['age_hours']:>3}h old  {v['channel']} | {v['title'][:60]}\n    {v['url']}")
    print("\nChannels behind them:")
    for c in result["channels"]:
        print(f"   {c['channel']} ({c['followers']:,} subscribers, {c['hits']} hit(s))  {c['url']}")
    print('\nClip a video:     python autopilot.py add "<video link>"'
          '\nWatch a channel:  python autopilot.py watch "<channel link>"'
          "\nOnly clip creators who allow it: reposting without permission gets copyright strikes.")


def main():
    p = argparse.ArgumentParser(description="Clip new videos and post them automatically.")
    p.add_argument("command", choices=["run", "connect", "add", "watch", "unwatch", "status", "approve", "skip",
                                       "retry", "discover", "install", "uninstall"])
    p.add_argument("target", nargs="*", help="a link, id or platform (discover: one or more search keywords)")
    p.add_argument("--every", type=int, default=15, help="minutes between runs (install)")
    args = p.parse_args()
    keywords, args.target = args.target, args.target[0] if args.target else ""
    if sys.stdout is None:  # pythonw (the scheduled task) has no console: keep library output in the log
        ap.HOME.mkdir(parents=True, exist_ok=True)
        sys.stdout = sys.stderr = open(ap.LOG_FILE, "a", encoding="utf-8")
    else:  # older Windows consoles use cp1252, which can't print ✓ or Arabic titles
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    load_env()

    try:
        if args.command == "run":
            ap.run()
        elif args.command == "connect":
            if args.target not in social.CONNECT:
                sys.exit("Choose one: connect youtube | connect tiktok | connect meta")
            print(social.CONNECT[args.target]())
        elif args.command in ("add", "watch", "unwatch") and not args.target:
            sys.exit(f"Give a link: python autopilot.py {args.command} \"https://...\"")
        elif args.command == "add":  # via the inbox, so it never collides with a pass that's running
            with open(ap.INBOX_FILE, "a", encoding="utf-8") as f:
                f.write(args.target.strip() + "\n")
            print("Added to inbox.txt; it's picked up on the next run.")
        elif args.command in ("watch", "unwatch"):
            if args.command == "watch" and re.search(r"youtu\.be/|/watch\?|/shorts/|/live/", args.target):
                sys.exit("That's a link to one video. To clip it, use: python autopilot.py add \"<link>\"\n"
                         "watch is for channels, e.g. https://www.youtube.com/@ChannelName")
            if args.command == "watch" and not sources.is_youtube(args.target):
                found = ap.latest_from_page(args.target)  # raises a clear error for single videos / unreadable pages
                print(f"Found {len(found)} recent video(s) on that page; only new uploads from now on get clipped.")
            cfg = ap.load_config()
            cfg["channels"] = [c for c in cfg["channels"] if c != args.target]
            if args.command == "watch":
                cfg["channels"].append(args.target)
            ap.CONFIG_FILE.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
            print(f"Now watching {len(cfg['channels'])} channel(s).")
        elif args.command == "status":
            status()
        elif args.command in ("approve", "skip", "retry"):
            if not args.target:
                sys.exit(f"Say what to {args.command}: all, a job id, or job/clip (see: python autopilot.py status)")
            with ap.RunLock():  # don't edit the queue under a running pass
                cfg, state = ap.load_config(), ap.load_state()
                n = (ap.approve(state, cfg, args.target) if args.command == "approve" else
                     ap.skip(state, args.target) if args.command == "skip" else ap.retry(state, cfg, args.target))
                ap.save_state(state)
            print(f"{args.command}: {n} post(s)")
        elif args.command == "discover":
            show_discovery(keywords or ap.load_config()["discover_keywords"])
        elif args.command == "install":
            install(args.every)
        elif args.command == "uninstall":
            done = subprocess.run(["schtasks", "/Delete", "/TN", TASK_NAME, "/F"], capture_output=True)
            print("Autopilot stopped: it won't run automatically any more." if done.returncode == 0
                  else "Autopilot wasn't installed, so there was nothing to stop.")
    except ap.Busy as e:
        if args.command != "run":  # a scheduled pass finding the previous one still rendering is normal
            sys.exit(str(e))
    except ClipperError as e:
        if args.command == "run":
            ap.log(f"Run stopped: {e}")
        sys.exit(f"Error: {e}")


if __name__ == "__main__":
    main()
