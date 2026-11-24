"""Streamlit interface: paste a link or upload a video, review the AI's picks, render and export clips.

Start it with:  .\\run.ps1   (or: python -m streamlit run app.py)
"""
import shutil
from pathlib import Path

import streamlit as st

st.set_page_config(page_title="AI Clip Generator", page_icon="🎬", layout="wide")

from clipper_core import campaigns, pipeline, render, sources  # noqa: E402
from clipper_core.config import (  # noqa: E402
    ASPECTS, FRAMING, JOBS_DIR, QUALITY, ClipperError, Settings, check_dependencies, load_env, parse_cam_box,
)
import numpy as np  # noqa: E402
from clipper_core.highlights import CLIP_TYPES, HOOK_TEXT_MAX, SCORE_KEYS  # noqa: E402
from clipper_core.jobs import Job  # noqa: E402
from clipper_core.render import render_signature  # noqa: E402

load_env()

ANALYZE_STAGES = [("download", "1. Source"), ("transcribe", "2. Transcript"), ("analyze", "3. AI analysis")]
RENDER_STAGES = [("render", "Rendering")]
UPLOAD_TYPES = ["mp4", "mov", "mkv", "webm", "avi", "m4v", "mpg", "mpeg", "wmv"]


class ProgressUI:
    """Progress bars inside a status box; pipeline functions call it as progress(stage, fraction, message)."""

    def __init__(self, stages, label):
        self.stages = stages
        self.box = st.status(label, expanded=True)
        with self.box:
            self.bars = {key: st.progress(0.0, text=name) for key, name in stages}
            self.message = st.empty()
            # clicking any button reruns the script, which interrupts this run at its next progress update
            self.stop_slot = st.empty()
            self.stop_slot.button("⏹ Stop processing", key="stop_processing",
                                  on_click=flash, args=("warning", "Processing stopped."))

    def __call__(self, stage, fraction, message=""):
        keys = [k for k, _ in self.stages]
        if stage in keys:
            for earlier in keys[: keys.index(stage)]:
                self.bars[earlier].progress(1.0, text=dict(self.stages)[earlier] + " ✓")
            self.bars[stage].progress(min(max(fraction, 0.0), 1.0), text=f"{dict(self.stages)[stage]}: {message}")
        if message:
            self.message.caption(message)

    def done(self, label):
        for key, name in self.stages:
            self.bars[key].progress(1.0, text=name + " ✓")
        self.stop_slot.empty()
        self.box.update(label=label, state="complete", expanded=False)

    def fail(self, label):
        self.stop_slot.empty()  # an error already stopped processing
        self.box.update(label=label, state="error", expanded=True)


@st.cache_data(ttl=60)
def dependency_status():
    return check_dependencies()


def sidebar_settings():
    sb = st.sidebar
    sb.header("🎯 Campaign")
    found = {c["id"]: c for c in campaigns.list_all()}
    campaign_id = sb.selectbox("Campaign", [""] + list(found), key="s_campaign",
                               format_func=lambda k: found[k]["name"] if k else "No campaign",
                               help="A campaign's rules are given to the AI and checked on every clip. "
                                    "Create or edit campaigns in the 🎯 Campaigns tab.")
    if campaign_id:
        c = found[campaign_id]
        fixed = [f"{c['min_seconds']}–{c['max_seconds']}s" if c["min_seconds"] and c["max_seconds"] else "",
                 FRAMING.get(c["framing"], "") if c["framing"] else "", " ".join(c["required_hashtags"])]
        sb.caption("Sets: " + " · ".join(f for f in fixed if f) + ". These override the settings below.")
    sb.header("⚙️ Settings")
    num = sb.slider("Number of clips", 1, 20, 3, key="s_num",
                    help="For multi-hour streams, ask for 10 or more: the AI reads the stream in parts.")
    lo, hi = sb.slider("Clip length (seconds)", 10, 180, (30, 60), step=5, key="s_len")
    aspect = sb.selectbox("Aspect ratio", list(ASPECTS), key="s_aspect")
    framing = sb.selectbox("Framing", list(FRAMING), format_func=FRAMING.get, key="s_framing")
    quality = sb.selectbox("Output quality", list(QUALITY), index=1, format_func=lambda q: QUALITY[q]["label"], key="s_quality")
    with sb.expander("Subtitles", expanded=False):
        subs = st.toggle("Burn in subtitles", value=True, key="s_subs")
        font = st.slider("Font size", 36, 120, 72, key="s_font")
        c1, c2 = st.columns(2)
        color = c1.color_picker("Text", "#FFFFFF", key="s_color")
        highlight = c2.color_picker("Highlight", "#FFD400", key="s_hl")
        position = st.selectbox("Position", ["lower", "middle", "upper"], key="s_pos",
                                help="'lower' stays above the TikTok/Reels/Shorts caption and button area.")
        background = st.selectbox("Background", ["outline", "box", "none"], key="s_bg")
        emphasis = st.selectbox("Emphasis", ["word", "caps", "none"], key="s_emph",
                                format_func={"word": "Highlight spoken word", "caps": "Bold capitals", "none": "Plain"}.get)
        hook = st.toggle("Hook text over the first 3 seconds", value=True, key="s_hook",
                         help="Burns the AI's on-screen hook (editable on each clip) in a bold box at the top.")
    with sb.expander("Advanced", expanded=False):
        whisper = st.selectbox("Whisper model (videos without captions)", ["tiny", "base", "small", "medium"], index=1,
                               key="s_whisper", help="Bigger is more accurate but much slower on a CPU.")
        model = st.text_input("Gemini model", value="", placeholder=Settings().gemini_model(), key="s_model")
    with sb.expander("System check", expanded=False):
        for name, (ok, detail) in dependency_status().items():
            st.markdown(f"{'✅' if ok else '⚠️'} **{name}** — {detail}")
        if st.button("Re-check"):
            dependency_status.clear()
            st.rerun()
    settings = Settings(num_clips=num, min_seconds=lo, max_seconds=hi, aspect=aspect, framing=framing,
                        quality=quality, subtitles=subs, sub_font_size=font, sub_color=color, sub_highlight=highlight,
                        sub_position=position, sub_background=background, sub_emphasis=emphasis, hook_overlay=hook,
                        whisper_model=whisper, model=model, campaign=campaign_id)
    return campaigns.apply_settings(settings, found[campaign_id]) if campaign_id else settings


def flash(kind, text):
    st.session_state.setdefault("flash", []).append((kind, text))


def show_flash():
    for kind, text in st.session_state.pop("flash", []):
        getattr(st, kind)(text)


def open_job(job):
    """Make this job the one on screen, dropping any edit widgets left over from a previous run."""
    for k in [k for k in st.session_state if isinstance(k, str) and k.startswith(f"{job.id}:")]:
        del st.session_state[k]
    st.session_state.job_id = job.id


def run_analysis(opener, settings, force):
    ui = ProgressUI(ANALYZE_STAGES, "Processing...")
    try:
        job, info = opener(ui)
        prev = job.data.get("settings") or {}
        reusable = job.data.get("candidates") and not force and all(
            prev.get(k, "") == getattr(settings, k) for k in ("num_clips", "min_seconds", "max_seconds", "campaign"))
        if reusable:
            ui("analyze", 1.0, "Loaded the previous results for this video")
        else:
            pipeline.analyze(job, settings, info, ui)
        ui.done(f"Found {len(job.data['candidates'])} clips — review them below")
        open_job(job)
    except ClipperError as e:
        ui.fail("Processing failed")
        st.error(str(e))
    except Exception as e:  # unexpected bug: show it rather than pretend it worked
        ui.fail("Processing failed")
        st.error(f"Unexpected error: {e}")


def save_upload(upload):
    tmp_dir = JOBS_DIR / "_uploads"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    path = tmp_dir / Path(upload.name).name
    with open(path, "wb") as f:
        shutil.copyfileobj(upload, f, length=8 * 1024 * 1024)
    return path


def _lines(text):
    return [x.strip() for x in (text or "").splitlines() if x.strip()]


def _parse_references(text):
    out = []
    for line in _lines(text):
        url, _, note = line.partition(" — ") if " — " in line else line.partition(" - ")
        out.append({"url": url.strip(), "note": note.strip()})
    return out


def campaign_editor():
    found = {c["id"]: c for c in campaigns.list_all()}
    if "ce_pending" in st.session_state:  # a campaign was just saved: show it (set before the widget is drawn)
        st.session_state.ce_choice = st.session_state.pop("ce_pending")
    choice = st.selectbox("Edit campaign", ["__new__"] + list(found), key="ce_choice",
                          format_func=lambda k: "➕ New campaign" if k == "__new__" else found[k]["name"])
    c = found.get(choice) or campaigns._clean({})
    k = f"ce_{choice}_"  # widget keys per campaign, so switching campaigns shows its own values
    with st.form(k + "form"):
        name = st.text_input("Name", c["name"], key=k + "name", placeholder="e.g. Podcast Highlights")
        brief = st.text_area("Campaign requirements (paste them exactly as the campaign page shows them)",
                             c["brief"], height=180, key=k + "brief",
                             help="The AI reads this before picking clips and writing captions.")
        a, b = st.columns(2)
        look_for = a.text_area("What to look for (one per line)", "\n".join(c["look_for"]), key=k + "look")
        avoid = b.text_area("Never pick or write clips that… (one per line)", "\n".join(c["avoid"]), key=k + "avoid")
        a, b, d = st.columns(3)
        hashtags = a.text_input("Required hashtags", " ".join(c["required_hashtags"]), key=k + "tags",
                                placeholder="#ExamplePodcast")
        mentions = b.text_input("Names the caption must mention", ", ".join(c["required_mentions"]), key=k + "ment",
                                placeholder="Example Podcast")
        language = d.text_input("Caption language", c["caption_language"], key=k + "lang", placeholder="English")
        a, b, d, e = st.columns(4)
        earliest = a.text_input("Sources from (date)", c["earliest_source_date"], key=k + "date",
                                placeholder="2026-11-18")
        lo = b.number_input("Min clip length (s)", 0, 600, c["min_seconds"] or 0, key=k + "min",
                            help="0 = use the sidebar setting")
        hi = d.number_input("Max clip length (s)", 0, 600, c["max_seconds"] or 0, key=k + "max")
        aspect = e.selectbox("Aspect", [""] + list(ASPECTS), index=([""] + list(ASPECTS)).index(c["aspect"]),
                             key=k + "aspect", format_func=lambda x: x or "Sidebar setting")
        a, b = st.columns(2)
        framing = a.selectbox("Layout", [""] + list(FRAMING), index=([""] + list(FRAMING)).index(c["framing"]),
                              key=k + "framing", format_func=lambda x: FRAMING.get(x, "Sidebar setting"))
        cam_box = b.text_input("Camera position (x, y, width, height as 0–1 fractions)", c["cam_box"],
                               key=k + "cam", help="Needed for the stream layout. Use the camera helper below to "
                                                   "find it from a frame of the stream.")
        a, b = st.columns(2)
        game_box = a.text_input("Gameplay area (x, y, width, height; optional)", c["game_box"], key=k + "game",
                                help="The part of the frame used as gameplay in the stream layout, e.g. to leave "
                                     "out the chat. Empty = the centre of the frame, next to the camera box.")
        platforms = b.text_input("Allowed platforms", ", ".join(c["platforms"]), key=k + "plat")
        refs = st.text_area("Reference clips (one per line: link — what's good about it)",
                            "\n".join(f"{r.get('url', '')} — {r.get('note', '')}".strip(" —") for r in c["references"]),
                            key=k + "refs")
        checks = st.text_area("Things to check yourself before posting (one per line)",
                              "\n".join(c["manual_checks"]), key=k + "checks")
        save = st.form_submit_button("💾 Save campaign", type="primary")
    if save:
        data = {"id": c["id"], "name": name, "brief": brief, "look_for": _lines(look_for), "avoid": _lines(avoid),
                "required_hashtags": hashtags.split(), "required_mentions": [m.strip() for m in mentions.split(",")],
                "caption_language": language.strip(), "earliest_source_date": earliest.strip(),
                "min_seconds": lo or None, "max_seconds": hi or None, "aspect": aspect, "framing": framing,
                "cam_box": cam_box.strip(), "game_box": game_box.strip(), "platforms": [p.strip() for p in platforms.split(",")],
                "references": _parse_references(refs), "manual_checks": _lines(checks)}
        try:
            saved = campaigns.save(data)
        except ClipperError as e:
            st.error(str(e))
        else:
            flash("success", f"Saved campaign “{saved['name']}”. Pick it in the sidebar to use it.")
            st.session_state.ce_pending = saved["id"]
            st.rerun()
    if choice != "__new__":
        camera_helper(c)


FRAMES_DIR = JOBS_DIR / "_frames"


def camera_helper(c):
    """Grab a frame from a stream, drag sliders over the facecam, preview the stream layout, save the position."""
    from PIL import Image, ImageDraw

    with st.expander("📷 Camera position helper", expanded=not c["cam_box"]):
        st.caption("Paste a link to one of the streams and a time when the gameplay and the camera box are both on "
                   "screen, then line the red box up with the camera.")
        a, b, d = st.columns([4, 1, 1])
        url = a.text_input("Stream link", key=f"cam_url_{c['id']}", placeholder="https://www.twitch.tv/videos/...")
        minute = b.number_input("Minute", 0, 30 * 60, 60, key=f"cam_min_{c['id']}")
        frame_file = FRAMES_DIR / f"{c['id']}.png"
        if d.button("Grab frame", key=f"cam_grab_{c['id']}", disabled=not url.strip()):
            try:
                with st.spinner("Downloading 2 seconds of the stream..."):
                    FRAMES_DIR.mkdir(parents=True, exist_ok=True)
                    piece = FRAMES_DIR / f"{c['id']}-{int(minute)}.mp4"
                    piece.unlink(missing_ok=True)
                    sources.download_section(url.strip(), minute * 60, minute * 60 + 2, piece)
                    render.extract_frame(piece, 0.5, frame_file)
                    piece.unlink(missing_ok=True)
            except ClipperError as e:
                st.error(str(e))
        if not frame_file.exists():
            return
        img = Image.open(frame_file).convert("RGB")
        W, H = img.size
        try:
            x, y, w, h = parse_cam_box(c["cam_box"])
        except ClipperError:
            x, y, w, h = 0.0, 0.0, 0.25, 0.25
        s1, s2, s3, s4 = st.columns(4)
        x = s1.slider("Left %", 0.0, 99.0, round(x * 100, 1), 0.1, key=f"cam_x_{c['id']}") / 100
        y = s2.slider("Top %", 0.0, 99.0, round(y * 100, 1), 0.1, key=f"cam_y_{c['id']}") / 100
        w = s3.slider("Width %", 2.0, 100.0, round(w * 100, 1), 0.1, key=f"cam_w_{c['id']}") / 100
        h = s4.slider("Height %", 2.0, 100.0, round(h * 100, 1), 0.1, key=f"cam_h_{c['id']}") / 100
        w, h = min(w, 1 - x), min(h, 1 - y)
        box = f"{x:.4f},{y:.4f},{w:.4f},{h:.4f}"
        marked = img.copy()
        ImageDraw.Draw(marked).rectangle([x * W, y * H, (x + w) * W, (y + h) * H], outline=(255, 40, 40),
                                         width=max(3, W // 300))
        score = render.overlay_edge_score(np.asarray(img.convert("L")), box)
        left, right = st.columns([3, 1])
        out_w, out_h = 540, 960
        try:
            parse_cam_box(c["game_box"]) if c["game_box"] else None
            game_box = c["game_box"]
        except ClipperError:
            game_box = ""
        top_h, (cw, ch, cx, cy), (gw, gh, gx, gy) = render.stack_layout(W, H, box, out_w, out_h, game_box)
        ImageDraw.Draw(marked).rectangle([gx, gy, gx + gw, gy + gh], outline=(40, 200, 255), width=max(3, W // 300))
        left.image(marked, caption=f"Frame at minute {int(minute)} · red: camera box {box} · blue: gameplay used")
        preview = Image.new("RGB", (out_w, out_h))
        preview.paste(img.crop((cx, cy, cx + cw, cy + ch)).resize((out_w, top_h)), (0, 0))
        preview.paste(img.crop((gx, gy, gx + gw, gy + gh)).resize((out_w, out_h - top_h)), (0, top_h))
        right.image(preview, caption="Stream layout preview")
        if score >= render.OVERLAY_EDGE_RATIO:
            st.success(f"Camera box edges found at this position (edge score {score:.0f}). Clips where the camera "
                       "box is on screen will use the stream layout; IRL parts follow the face instead.")
        else:
            st.warning(f"No sharp camera-box edges at this position (edge score {score:.1f}, needs "
                       f"{render.OVERLAY_EDGE_RATIO:.0f}). Line the box up with the camera's border, or grab a frame "
                       "where the camera box is showing.")
        if st.button("✅ Use this camera position", key=f"cam_save_{c['id']}", disabled=box == c["cam_box"]):
            campaigns.save({**c, "cam_box": box})
            st.session_state.pop(f"ce_{c['id']}_cam", None)  # the form shows the saved value next time
            flash("success", f"Camera position saved: {box}")
            st.rerun()


def input_section(settings):
    tab_link, tab_file, tab_recent, tab_campaigns = st.tabs(
        ["🔗 Paste link", "📁 Upload file", "🕘 Recent jobs", "🎯 Campaigns"])
    with tab_campaigns:
        campaign_editor()
    with tab_link:
        url = st.text_input("Video link", placeholder="YouTube, TikTok, Instagram, Facebook, X, Twitch, Vimeo... or a direct .mp4 link", key="url")
        go_link = st.button("Start processing", type="primary", key="go_link", disabled=not url.strip())
    with tab_file:
        upload = st.file_uploader("Video file", type=UPLOAD_TYPES)
        go_file = st.button("Start processing", type="primary", key="go_file", disabled=upload is None)
    with tab_recent:
        jobs = Job.list_all()
        if jobs:
            choice = st.selectbox(
                "Previous jobs", jobs, format_func=lambda j: f"{j.data['source'].get('title') or j.id} — "
                f"{len(j.data.get('candidates', []))} clips — {j.data.get('updated', '')}")
            if st.button("Open"):
                open_job(choice)
        else:
            st.caption("No jobs yet.")
    force = st.checkbox("Re-run the AI even if this video was analysed before", key="force")

    if go_link:
        run_analysis(lambda ui: pipeline.open_url(url.strip(), ui), settings, force)
    elif go_file:
        def opener(ui):
            ui("download", 0.02, "Saving upload...")
            path = save_upload(upload)
            try:
                return pipeline.open_file(path, ui), None
            finally:
                path.unlink(missing_ok=True)
        run_analysis(opener, settings, force)


def _campaign_for(settings):
    try:
        return campaigns.for_settings(settings)
    except ClipperError:
        return None


def candidate_card(job, c, settings):
    key = f"{job.id}:{c['id']}:"
    defaults = {"selected": c.get("selected", True), "title": c["title"], "start": float(c["start"]),
                "end": float(c["end"]), "description": c["description"], "caption": c["caption"],
                "hashtags": " ".join(c["hashtags"]), "hook_text": c.get("hook_text", "")}
    for field, value in defaults.items():
        st.session_state.setdefault(key + field, value)
    # widget values from the last interaction are already in session_state: store edits before drawing
    edited = {
        "selected": st.session_state[key + "selected"],
        "title": st.session_state[key + "title"].strip(),
        "start": round(st.session_state[key + "start"], 2),
        "end": round(st.session_state[key + "end"], 2),
        "description": st.session_state[key + "description"].strip(),
        "caption": st.session_state[key + "caption"].strip(),
        "hashtags": [t if t.startswith("#") else f"#{t}" for t in st.session_state[key + "hashtags"].split()],
        "hook_text": " ".join(st.session_state[key + "hook_text"].split()),
    }
    changed = any(c.get(k) != v for k, v in edited.items())
    c.update(edited)
    duration = float(job.data["source"].get("duration") or c["end"] + 60)
    rendered = job.dir / c["render"]["file"] if c.get("render", {}).get("file") else None
    rendered = rendered if rendered and rendered.exists() else None

    with st.container(border=True):
        left, right = st.columns([2, 3], gap="large")
        with right:
            top = st.columns([1, 6, 2])
            top[0].checkbox("Use", key=key + "selected", label_visibility="collapsed", help="Include this clip")
            top[1].text_input("Title", key=key + "title", label_visibility="collapsed")
            top[2].markdown(f"**Score {c['score']}/100**" + (f" · 💬 {c['viewer_mentions']}" if c.get("viewer_mentions") else ""),
                            help="Heuristic: the average of the AI's 1–10 ratings for hook, clarity, story, usefulness "
                            "and payoff, plus up to +10 when viewers timestamped this moment in the comments (💬 = how "
                            "many). Not a prediction of views.")
            t1, t2, t3 = st.columns([2, 2, 1])
            t1.number_input("Start (s)", 0.0, duration, step=0.5, format="%.1f", key=key + "start")
            t2.number_input("End (s)", 0.0, duration, step=0.5, format="%.1f", key=key + "end")
            length = st.session_state[key + "end"] - st.session_state[key + "start"]
            t3.metric("Length", f"{length:.0f}s")
            if length <= 0:
                st.warning("End must be after start.")
            clip_type = CLIP_TYPES.get(c.get("clip_type") or "other", CLIP_TYPES["other"])[0]
            st.text_input(f"On-screen hook · {clip_type}", key=key + "hook_text", max_chars=HOOK_TEXT_MAX,
                          help="Shown in a bold box at the top for the first 3 seconds (turn it off under "
                               "Subtitles in the sidebar). Leave empty for no hook text on this clip.")
            st.text_area("Description", key=key + "description", height=70)
            st.text_area("Post caption", key=key + "caption", height=90)
            st.text_input("Hashtags", key=key + "hashtags")
            campaign = _campaign_for(settings)
            if campaign:
                checks = campaigns.check(c, campaign, job.data["source"], settings)
                icons = {"ok": "✅", "fail": "❌", "manual": "👀"}
                with st.expander(f"Campaign check: {campaigns.summary(checks)}",
                                 expanded=any(s == "fail" for s, _ in checks)):
                    st.markdown("\n".join(f"- {icons[s]} {t}" for s, t in checks))
                    st.caption("✅ checked automatically · ❌ breaks a rule: fix it before posting · "
                               "👀 check it yourself")
            with st.expander("Why the AI picked this"):
                st.markdown(f"**Topic:** {c.get('topic', '')}\n\n**Opening hook:** “{c.get('hook', '')}”\n\n"
                            f"**Why it could engage viewers:** {c.get('reason', '')}")
                st.caption(" · ".join(f"{k} {c['scores'][k]}/10" for k in SCORE_KEYS))
            b1, b2 = st.columns(2)
            b1.button("🤖 AI re-cut", key=key + "recut", on_click=do_recut, args=(job.id, c["id"], settings),
                      help="Ask the AI for better start/end points for this moment.")
            render_one = b2.button("🎬 Render this clip", key=key + "render")
        with left:
            if rendered:
                stale = c["render"].get("signature") != render_signature(c, settings)
                st.video(str(rendered))
                if stale:
                    st.caption("⚠️ Times or settings changed since this render. Render again to update it.")
                if settings.framing == "stack" and c["render"].get("layout") == "smart":
                    st.caption("No camera box in this part of the stream (IRL or full camera), so it follows the "
                               "face instead of the stream layout.")
                st.download_button("⬇️ Download clip", rendered.read_bytes(), file_name=f"{c['id']}.mp4",
                                   mime="video/mp4", key=key + "dl")
            else:
                src = job.data["source"]
                start, end = int(c["start"]), int(c["end"]) + 1
                st.video(src["url"] if src["type"] == "url" else src["path"], start_time=start,
                         end_time=end if end > start else None)
                st.caption("Source preview (not rendered yet)")
            if c.get("render", {}).get("status") == "failed":
                st.error(c["render"]["error"])
    return changed, render_one


def do_recut(job_id, cid, settings):
    job = Job(job_id)
    try:
        c = pipeline.recut(job, cid, settings)
    except ClipperError as e:
        flash("error", f"Re-cut failed: {e}")
        return
    key = f"{job_id}:{cid}:"
    for field in ("title", "start", "end", "description", "caption", "hook_text"):
        st.session_state[key + field] = float(c[field]) if field in ("start", "end") else c[field]
    st.session_state[key + "hashtags"] = " ".join(c["hashtags"])
    flash("success", f"New cut for {cid}: {c['start']:.1f}–{c['end']:.1f}s")


def run_render(job, settings, cids=None, export=False):
    ui = ProgressUI(RENDER_STAGES, "Rendering...")
    try:
        pipeline.render(job, settings, cids, ui)
        ui.done("Rendering finished")
        if export:  # clips + caption files + zip, ready to post
            job.export()
            flash("success", f"Your clips are ready to post: {job.export_dir} (or use Download all below).")
        else:
            flash("success", "Rendering finished.")
    except ClipperError as e:
        ui.fail("Rendering failed")
        flash("error", str(e))
    except Exception as e:
        ui.fail("Rendering failed")
        flash("error", f"Unexpected error: {e}")
    st.rerun()


def job_section(job, settings):
    src = job.data["source"]
    st.divider()
    st.subheader(src.get("title") or job.id)
    hours = src.get("duration", 0) / 3600
    meta = [f"{hours:.1f} hours" if hours >= 1.5 else f"{src.get('duration', 0) / 60:.1f} min"]
    if src.get("published"):
        meta.append(f"from {src['published']}")
    if job.data.get("transcript_source"):
        meta.append(f"transcript: {job.data['transcript_source']}")
    meta.append(f"folder: `{job.dir}`")
    st.caption(" · ".join(meta))
    if job.data.get("status") == "failed" and job.data.get("error"):
        st.error(job.data["error"])
    if job.data.get("notes"):
        st.info(job.data["notes"])

    to_render = None
    export_after = False
    any_changed = False
    for c in job.data["candidates"]:
        changed, render_one = candidate_card(job, c, settings)
        any_changed |= changed
        if render_one:
            to_render = [c["id"]]
    if any_changed:
        job.save()

    selected = [c for c in job.data["candidates"] if c.get("selected")]
    st.markdown(f"**{len(selected)} of {len(job.data['candidates'])} clips selected**")
    a, b, d = st.columns(3)
    if a.button(f"🎬 Render & export selected ({len(selected)})", type="primary", disabled=not selected,
                help="Renders the clips, then saves each one with its caption and hashtags, plus a zip of all of them."):
        to_render = [c["id"] for c in selected]
        export_after = True
    if b.button("📦 Export selected", disabled=not selected):
        try:
            job.export()
            flash("success", f"Exported to {job.export_dir}")
        except ClipperError as e:
            flash("error", str(e))
        st.rerun()
    zip_path = job.dir / f"{job.id}-clips.zip"
    if zip_path.exists():
        d.download_button("⬇️ Download all (zip)", zip_path.read_bytes(), file_name=zip_path.name, mime="application/zip")
    if to_render:
        run_render(job, settings, to_render, export_after)

    with st.expander("Processing log"):
        st.code(job.read_log() or "(empty)", language=None)


def main():
    st.title("🎬 AI Clip Generator")
    st.caption("Paste a video link or upload a file. The AI suggests the strongest moments, you review and adjust "
               "them, then export vertical clips with subtitles and post captions.")
    settings = sidebar_settings()
    try:
        settings.validate()
    except ClipperError as e:
        st.error(str(e))
        return
    missing = [n for n, (ok, _) in dependency_status().items() if not ok and "optional" not in n]
    if missing:
        st.warning("Setup needed: " + ", ".join(missing) + ". Open **System check** in the sidebar for details.")
    input_section(settings)
    show_flash()
    if st.session_state.get("job_id"):
        job = Job(st.session_state.job_id)
        if job.data["candidates"]:
            job_section(job, settings)


main()
