"""
Command-line version of the clip generator. The web app (run.ps1 / app.py) does the same with a UI.

Usage:
    python clipper.py "https://www.youtube.com/watch?v=..."     # analyse, render and export
    python clipper.py video.mp4 --clips 5 --min 20 --max 45
    python clipper.py video.mp4 --no-subtitles --framing center
    python clipper.py --check                                   # check dependencies
    python clipper.py "https://www.twitch.tv/videos/..." --campaign example --clips 10
    python clipper.py --campaigns                               # list saved campaign profiles
"""
import argparse
import re
import sys

from clipper_core import campaigns, pipeline
from clipper_core.config import FRAMING, QUALITY, ClipperError, Settings, check_dependencies, load_env


def main():
    p = argparse.ArgumentParser(description="Find the best moments in a video and render vertical clips.")
    p.add_argument("source", nargs="?", help="YouTube link or local video file")
    p.add_argument("--clips", type=int, default=3)
    p.add_argument("--min", type=int, default=30, help="minimum clip length in seconds")
    p.add_argument("--max", type=int, default=60, help="maximum clip length in seconds")
    p.add_argument("--aspect", default="9:16", choices=["9:16", "1:1", "4:5", "16:9"])
    p.add_argument("--framing", default="smart", choices=list(FRAMING))
    p.add_argument("--quality", default="standard", choices=list(QUALITY))
    p.add_argument("--no-subtitles", action="store_true")
    p.add_argument("--whisper", default="base", help="Whisper model for videos without captions")
    p.add_argument("--model", default="", help="Gemini model (default: GEMINI_MODEL or gemini-3.5-flash)")
    p.add_argument("--check", action="store_true", help="check dependencies and exit")
    p.add_argument("--campaign", default="", help="campaign profile id (see --campaigns); applies its rules")
    p.add_argument("--campaigns", action="store_true", help="list saved campaign profiles and exit")
    args = p.parse_args()
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # titles and checks use emoji and other scripts

    if args.campaigns:
        found = campaigns.list_all()
        for c in found:
            print(f"{c['id']:24} {c['name']}")
        if not found:
            print(f"No campaigns yet. Create one in the web app or add a JSON file to {campaigns.CAMPAIGNS_DIR}")
        return

    if args.check or not args.source:
        for name, (ok, detail) in check_dependencies().items():
            print(f"{'OK ' if ok else '-- '} {name}: {detail}")
        if not args.source:
            p.print_usage()
        return

    load_env()
    settings = Settings(num_clips=args.clips, min_seconds=args.min, max_seconds=args.max, aspect=args.aspect,
                        framing=args.framing, quality=args.quality, subtitles=not args.no_subtitles,
                        whisper_model=args.whisper, model=args.model, campaign=args.campaign)
    try:
        campaign = campaigns.for_settings(settings)
        if campaign:
            settings = campaigns.apply_settings(settings, campaign)
            print(f"Campaign: {campaign['name']} ({settings.min_seconds}-{settings.max_seconds}s, "
                  f"{settings.framing} layout)")
        settings.validate()
    except ClipperError as e:
        sys.exit(f"Error: {e}")

    last = {"key": None}

    def progress(stage, fraction, message=""):
        # print a line when the step changes or every 5%, not on every downloaded fragment
        text = re.sub(r"\d+%", "", message).strip()
        key = (stage, text, int(fraction * 20))
        if key != last["key"]:
            last["key"] = key
            print(f"[{stage:>10}] {fraction:4.0%}  {message}", flush=True)

    try:
        if args.source.startswith(("http://", "https://")):
            job, info = pipeline.open_url(args.source, progress)
        else:
            job, info = pipeline.open_file(args.source, progress), None
        pipeline.analyze(job, settings, info, progress)
        if job.data.get("notes"):
            print("\nNotes:\n" + job.data["notes"])
        if not job.data["candidates"]:
            sys.exit(1)
        print()
        for c in job.data["candidates"]:
            print(f"{c['id']}  {c['start']:.1f}-{c['end']:.1f}s  score {c['score']}  {c['title']}")
        print()
        pipeline.render(job, settings, progress=progress)
        zip_path = job.export()
        if campaign:
            print("\nCampaign check:")
            for c in [c for c in job.data["candidates"] if c.get("selected")]:
                checks = campaigns.check(c, campaign, job.data["source"], settings)
                print(f"  {c['id']}  {campaigns.summary(checks)}")
                for status, text in checks:
                    if status == "fail":
                        print(f"      FAIL: {text}")
            print(f"  Full checklist: {job.export_dir / 'CHECKLIST.txt'}")
        print(f"\nDone. Clips and metadata: {job.export_dir}\nZip: {zip_path}")
    except ClipperError as e:
        sys.exit(f"\nError: {e}")


if __name__ == "__main__":
    main()
