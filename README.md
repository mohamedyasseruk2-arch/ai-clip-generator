# AI Clip Generator

[![tests](https://github.com/mohamedyasseruk2-arch/ai-clip-generator/actions/workflows/tests.yml/badge.svg)](https://github.com/mohamedyasseruk2-arch/ai-clip-generator/actions/workflows/tests.yml)

Paste a video link (or upload a file), let Gemini find the strongest moments, review and adjust them, then export
vertical clips with burned-in subtitles and ready-to-post captions.

## Quick start (Windows)

```powershell
cd $HOME\Documents\CLIPPER
.\run.ps1
```

Your browser opens at http://localhost:8501. If PowerShell refuses to run the script, run this once:
`Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`. Or start it directly with `python -m streamlit run app.py`.

## First-time setup

1. **Python 3.10+** – already installed if `python --version` works.
2. **Packages** – `python -m pip install -r requirements.txt` (`run.ps1` does this on first run).
3. **FFmpeg** – `winget install Gyan.FFmpeg`
4. **Deno** (optional, makes YouTube downloads more reliable) – `winget install DenoLand.Deno`
5. **Gemini API key** (free tier available) – create one at https://aistudio.google.com/apikey, then either
   - copy `.env.example` to `.env` and paste the key there, or
   - run `setx GEMINI_API_KEY "your-key"` and open a new terminal.

Open **System check** in the app's sidebar to confirm everything is found.

## Using it

1. **Paste link** tab: paste a YouTube link and click **Start processing**. (Or use **Upload file**.)
2. Watch the progress: source → transcript → AI analysis.
3. Review each suggested clip: preview it, tick or untick it, fix the start/end times, and edit the title,
   description, caption and hashtags. **🤖 AI re-cut** asks Gemini for better start/end points for one clip.
4. Click **🎬 Render selected**. Only clips whose times or settings changed are rendered again.
5. Click **📦 Export selected**, then **⬇️ Download all (zip)**.

Each video gets its own folder in `jobs\` with:

| File | Contents |
|---|---|
| `job.json` | source, settings, every candidate (times, titles, captions, scores) and status |
| `transcript.json` | the transcript (reused, so the same video is never transcribed twice) |
| `sections\` | for links: just the parts of the video each clip needs (a few MB each) |
| `video\` | the whole video, only if downloading a section ever fails |
| `renders\` | rendered clips |
| `export\` | the selected clips, a `.caption.txt` per clip, and `metadata.json` |
| `job.log` | processing log |

### Command line

```powershell
python clipper.py "https://www.youtube.com/watch?v=..."      # analyse, render and export
python clipper.py video.mp4 --clips 5 --min 20 --max 45 --framing fit
python clipper.py --check                                    # dependency check
python clipper.py "https://www.twitch.tv/videos/..." --campaign example --clips 10
python clipper.py --campaigns                                # list campaign profiles
```

## Clipping campaigns (Content Rewards and similar)

A **campaign profile** holds one campaign's rules. Make it once, then pick it for every video you clip for that
campaign: in the sidebar (**🎯 Campaign**) or with `--campaign <id>` on the command line. Profiles are JSON files in
`campaigns\`; create and edit them in the app's **🎯 Campaigns** tab.

| In the profile | What happens |
|---|---|
| Requirements text (pasted as-is), what to look for, what to avoid | Given to the AI before it picks clips and writes captions |
| Required hashtags and names (e.g. `#NOVA`, "Nova") | Added to every caption automatically |
| Caption language, clip length, aspect, layout | Applied to every clip |
| Earliest source date | Older videos get a warning (they wouldn't be paid) |
| Things to check yourself (logos, thumbnails, your account's audience) | Listed on each clip and in `export\CHECKLIST.txt` |

Each clip shows a **Campaign check**: ✅ checked automatically, ❌ breaks a rule (fix it before posting), 👀 check it
yourself. The export folder gets `CHECKLIST.txt` and the results in `metadata.json`. The AI follows content rules
like "no negative portrayal" as well as it can, but you still review each clip before submitting it.

**Stream layout.** For streams with a facecam box over gameplay, the layout **Stream layout (camera on top, gameplay
below)** puts the camera in the top 40% and the gameplay underneath. Set the camera position once per campaign with
the **📷 Camera position helper** (paste a VOD link, grab a frame, line up the red box). Optionally set a gameplay
area to leave out the chat. Each clip is checked: if the camera box isn't on screen (IRL or full-camera parts of the
stream), that clip follows the speaker's face instead.

### Long streams (Twitch VODs, up to 30 hours)

Twitch VODs have no captions, and transcribing 10 hours on a laptop CPU takes hours. For sources longer than 90
minutes without captions, the app first measures the audio's loudness (a few minutes), picks the loudest moments
(about 4 per hour: laughing, yelling, hype) and transcribes only around those. The AI then reads the stream in
one-hour parts and keeps the best clips overall. Ask for 10 or more clips on a long stream.

A VOD of a stream that's still live (24/7 marathons) can be clipped too: the part recorded so far is used. Run it
again later for the newer part.

## Autopilot: clip and post automatically

`autopilot.py` runs the whole thing hands-free. It takes videos from two places: YouTube channels you watch
(checked through their free RSS feed) and links you add yourself. It clips the best moments and posts them to
YouTube Shorts, TikTok, Instagram Reels and Facebook Reels on a daily schedule, using each platform's own free API.

```powershell
python autopilot.py connect youtube        # once per account; "meta" covers Instagram + Facebook
python autopilot.py watch "https://www.youtube.com/@SomeChannel"   # new uploads get clipped
python autopilot.py add "https://www.youtube.com/watch?v=..."      # or a one-off video (or paste into inbox.txt)
python autopilot.py install                # runs every 15 minutes in the background (Task Scheduler)
python autopilot.py status                 # queue, schedule, posted links, failures
```

Each pass downloads a video once and renders each clip once, then uploads the same file to every platform in
parallel. It deletes the source video after rendering and never runs two passes at once. A post is marked
"posting" before upload, so a crash can never cause a double post. Clips scoring below `min_score` aren't posted.
Every clip is also a normal job, so you can open it in the app's **Recent jobs** to review it.

### Catching moments early

- **Breakouts first:** each watched-channel upload gets a *breakout ratio*: its views per hour divided by the
  channel's usual pace (both read from the free channel feed). The fastest-growing video is clipped first. Set
  `min_breakout` (e.g. `2`) to clip only uploads growing at least that much faster than usual. A young video that's
  below the bar is checked again on every pass during its first 24 hours.
- **Viewer-flagged moments:** the top 200 comments are scanned for timestamps ("17:30 😂"). Chapter lists are
  ignored. The AI is told which moments viewers point at and builds clips around them first. Those clips get up to
  +10 score (shown as 💬 in the app). With 3 or more such moments, the AI reads only the transcript around them,
  which used 57% fewer tokens on a 98-minute episode.
- **Discovery:** `python autopilot.py discover podcast "بودكاست"` searches this week's long videos for each keyword
  and ranks them by momentum (views per hour, boosted when a video reaches beyond its own subscribers). It also
  lists the channels behind them. It takes about 25 seconds and uses no API quota. Put keywords in
  `discover_keywords` to run it daily (the result is saved to `jobs\_autopilot\discover.json`). Set
  `discover_auto_add` to queue the top N automatically, but only for creators who allow clipping.

### Settings (`autopilot.json`, created on first run)

| Key | Default | Meaning |
|---|---|---|
| `channels` | `[]` | channel URLs to watch (`watch` / `unwatch` edit this). The first check skips the back catalogue |
| `platforms` | all four | where to post; platforms you haven't connected are ignored |
| `post_times` | `10:00, 15:00, 20:00` | daily posting slots; one clip per slot, on every platform at once |
| `min_score` | `70` | only clips the AI scored at least this high are posted |
| `require_approval` | `false` | `true` holds clips until `python autopilot.py approve all` (or `approve <job>/<clip>`) |
| `videos_per_run` | `1` | videos clipped per pass (keeps each pass short) |
| `min_source_minutes` | `4` | watched-channel uploads shorter than this (Shorts, trailers) are skipped |
| `keep_source_video` | `false` | keep the downloaded source after rendering |
| `youtube_privacy` | `public` | `public`, `unlisted` or `private` |
| `tiktok_privacy` | `SELF_ONLY` | change to `PUBLIC_TO_EVERYONE` once TikTok has audited your app |
| `min_breakout` | `0` | `0` clips every long upload; e.g. `2` only clips uploads growing 2× faster than the channel's usual pace |
| `discover_keywords` | `[]` | search terms for the daily discovery (empty = off) |
| `discover_every_hours` | `24` | how often discovery runs |
| `discover_auto_add` | `0` | queue this many top discovered videos automatically (`0` = suggestions only) |
| `clip_settings` | 5 clips, 25–60 s | the same settings as the app sidebar (`framing`, `quality`, `sub_*`, …) |

### Autopilot setup (all free)

The OAuth redirect for YouTube and TikTok is `http://127.0.0.1:8765/callback/`. Keys go in `.env` (see `.env.example`).

**YouTube Shorts**
1. At https://console.cloud.google.com create a project and enable **YouTube Data API v3**.
2. Set up the **OAuth consent screen** (External), then **publish it ("In production")**. In Testing mode, Google
   expires the sign-in every 7 days. The "unverified app" warning is fine for your own channel: click *Advanced → continue*.
3. **Credentials → Create OAuth client ID → Desktop app**. Put the ID and secret in `.env`, then run `connect youtube`.
4. Uploads from a new API project stay **private** until Google's free compliance audit is approved:
   https://support.google.com/youtube/contact/yt_api_form . The default quota allows about 6 uploads a day.

**TikTok**
1. At https://developers.tiktok.com create an app, add **Login Kit (Desktop)** and **Content Posting API** (turn on
   Direct Post), request the scopes `user.info.basic` and `video.publish`, and add the redirect URI above.
2. Put the client key and secret in `.env`, then run `connect tiktok`.
3. Until TikTok audits the app, posts are **private (only you can see them)**. Submit the app for review. Once it's
   approved, set `tiktok_privacy` to `PUBLIC_TO_EVERYONE`.

**Instagram Reels + Facebook Reels** (one setup for both)
1. Switch Instagram to a **professional** (Creator or Business) account and link it to a **Facebook Page**.
2. At https://developers.facebook.com create a **Business** app with the Facebook Login for Business,
   Instagram and Pages use cases. Put the App ID and App secret (*Settings → Basic*) in `.env`.
3. Run `connect meta` and follow the prompt (it walks you through the Graph API Explorer). The Page token it
   stores doesn't expire. As the app's admin you can post to your own accounts without App Review. If posts aren't
   visible to others, switch the app to **Live** (it needs a privacy-policy URL).
4. Limits: Facebook allows 30 API reels per Page per day, and Instagram has a similar daily cap. Three slots a day
   stays well under both.

Only post clips you have the rights to (your own videos, or creators who permit clipping). Reposting other people's
content gets copyright strikes and account bans quickly.

## How it works

- **Transcript:** YouTube's own captions when they exist (seconds). Otherwise the audio, not the whole video, is
  downloaded and transcribed locally with Faster-Whisper (slow on a CPU: pick `tiny` or `base` under Advanced).
- **Highlights:** Gemini reads the transcript and returns structured candidates. The app then checks every
  timestamp against the video, snaps it to sentence boundaries, enforces your duration range, drops overlapping
  clips and ranks the rest. The **score** is the average of the AI's 1–10 ratings for hook, clarity, story,
  usefulness and payoff. It's a ranking aid, **not** a prediction of views.
- **Rendering:** for links, yt-dlp downloads only each clip's section plus 1 s either side (about 17 MB for a
  70-second clip, instead of 1.5 GB for a 98-minute video), with exact timing so subtitles stay in sync. If a
  section download fails, it downloads the whole video once and reuses it. FFmpeg then cuts and crops each clip in one pass. Encoding uses Intel Quick Sync
  (the graphics chip) when available, which keeps laptops cooler, and falls back to the CPU (libx264) otherwise. **Smart** framing finds the main face with OpenCV and centres on it; if no face is
  found it uses a centre crop. **Fit** shows the whole frame over a blurred background.
- **Subtitles:** generated from the transcript's word timings in short phrases, with the spoken word
  highlighted. "Lower" keeps them above the TikTok/Reels/Shorts button and caption area.

## Costs

Everything runs locally and is free except Gemini. One analysis sends the transcript (a 1-hour video is roughly
15–25k tokens), which fits comfortably in Gemini's free tier. On a paid key it costs a fraction of a cent per
video. See https://ai.google.dev/pricing.

## Troubleshooting

| Problem | Fix |
|---|---|
| `ffmpeg was not found` | `winget install Gyan.FFmpeg`, then restart VS Code |
| `GEMINI_API_KEY is not set` | see setup step 5 |
| `The Gemini model ... isn't available` | type another model under **Advanced** (e.g. `gemini-3.8-flash`) or set `GEMINI_MODEL` |
| `rate limit` | wait a minute; free keys allow a limited number of requests per minute |
| `needs a signed-in account` / `private` | the video is restricted; only public videos work |
| Very slow "Transcribing" | the video has no captions; choose Whisper `tiny` under Advanced |
| Captions out of sync by a second | YouTube's auto captions are approximate; nudge start/end and re-render |

## Development

```powershell
python -m pytest
```

Code layout: `app.py` (UI), `clipper.py` (CLI), and `clipper_core\`: `config` (settings, keys, FFmpeg
discovery), `sources` (links, files, captions), `transcribe` (Whisper), `highlights` (Gemini and validation),
`subtitles`, `render` (FFmpeg and framing), `jobs` (folders and metadata), `pipeline` (the steps the UI
and CLI share), `social` (platform uploads and sign-in) and `autopilot` (watching, queue, schedule). To support another site, add it to `SUPPORTED_HOSTS` in `sources.py`.

`backup\clipper_v1.py` is the original single-file script.
