"""Burned-in subtitles: transcript words -> short readable phrases -> an ASS subtitle file for ffmpeg."""

MAX_WORDS = 4
MAX_CHARS = 26
MAX_GAP = 0.6       # a pause longer than this starts a new phrase
MIN_SHOW = 0.4      # keep very fast phrases on screen long enough to read
HOOK_SECONDS = 3.0  # how long the on-screen hook text stays up at the start of a clip


def phrases(words, start, end):
    """Words inside [start, end], shifted to clip time and grouped into short phrases."""
    inside = [
        {"start": max(w["start"], start) - start, "end": min(w["end"], end) - start, "text": w["text"]}
        for w in words if w["end"] > start and w["start"] < end
    ]
    groups, current = [], []
    for w in inside:
        text_len = len(" ".join(x["text"] for x in current + [w]))
        if current and (len(current) >= MAX_WORDS or text_len > MAX_CHARS or w["start"] - current[-1]["end"] > MAX_GAP):
            groups.append(current)
            current = []
        current.append(w)
        if w["text"][-1:] in ".?!,":
            groups.append(current)
            current = []
    if current:
        groups.append(current)
    out = []
    for g in groups:
        p = {"start": g[0]["start"], "end": max(g[-1]["end"], g[0]["start"] + MIN_SHOW), "words": g}
        if out and p["start"] < out[-1]["end"]:
            out[-1]["end"] = p["start"]
        out.append(p)
    return [p for p in out if p["end"] > p["start"]]


def ass_color(hex_color, alpha=0):
    """#RRGGBB -> ASS &HAABBGGRR."""
    h = hex_color.lstrip("#")
    r, g, b = h[0:2], h[2:4], h[4:6]
    return f"&H{alpha:02X}{b}{g}{r}".upper()


def _ts(t):
    cs = int(round(max(t, 0) * 100))
    return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"


def _escape(text):
    return text.replace("\\", "\\\\").replace("{", "(").replace("}", ")")


def build_ass(phrase_list, width, height, settings, hook_text="", hook_seconds=HOOK_SECONDS, hook_y=None):
    """Subtitle events for phrase_list, plus hook_text in a bold box near the top for the first hook_seconds.

    hook_y (pixels) centres the hook on that line instead, e.g. the seam of the stream layout.
    """
    scale = width / 1080
    size = int(settings.sub_font_size * scale)
    # lower subtitles sit above the area TikTok/Reels/Shorts cover with captions and buttons
    align, margin_v = {"lower": (2, 0.24), "middle": (5, 0.0), "upper": (8, 0.14)}[settings.sub_position]
    border_style, outline, shadow, back = {
        "outline": (1, max(2, int(5 * scale)), 0, ass_color("#000000", 0x80)),
        "box": (3, max(4, int(12 * scale)), 0, ass_color("#000000", 0x60)),
        "none": (1, 0, max(1, int(2 * scale)), ass_color("#000000", 0x80)),
    }[settings.sub_background]
    outline_color = back if settings.sub_background == "box" else ass_color("#000000")
    bold = -1 if settings.sub_emphasis in ("caps", "word") else 0
    # the hook sits at the top, below the platforms' top bar; if subtitles use the top, it moves to the middle
    hook_align, hook_margin = (5, 0.0) if settings.sub_position == "upper" else (8, 0.10)
    hook_size = int(settings.sub_font_size * 0.85 * scale)
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Arial,{size},{ass_color(settings.sub_color)},{ass_color(settings.sub_highlight)},{outline_color},{back},{bold},0,0,0,100,100,0,0,{border_style},{outline},{shadow},{align},{int(width * 0.08)},{int(width * 0.08)},{int(height * margin_v)},1
Style: Hook,Arial,{hook_size},{ass_color("#FFFFFF")},{ass_color("#FFFFFF")},{ass_color("#000000", 0x30)},{ass_color("#000000", 0x30)},-1,0,0,0,100,100,0,0,3,{max(6, int(18 * scale))},0,{hook_align},{int(width * 0.07)},{int(width * 0.07)},{int(height * hook_margin)},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = []
    hook_text = " ".join((hook_text or "").split())
    if hook_text and hook_seconds > 0:
        place = f"\\an5\\pos({width // 2},{hook_y})" if hook_y else ""
        lines.append(f"Dialogue: 1,{_ts(0)},{_ts(hook_seconds)},Hook,,0,0,0,,{{{place}\\fad(150,250)}}{_escape(hook_text)}")
    highlight = ass_color(settings.sub_highlight)
    for p in phrase_list:
        words = [_escape(w["text"].upper() if settings.sub_emphasis in ("caps", "word") else w["text"]) for w in p["words"]]
        if settings.sub_emphasis != "word":
            lines.append(f"Dialogue: 0,{_ts(p['start'])},{_ts(p['end'])},Default,,0,0,0,,{' '.join(words)}")
            continue
        # one event per spoken word, with that word coloured, so the highlight follows the speech
        for i, w in enumerate(p["words"]):
            w_start = p["start"] if i == 0 else w["start"]
            w_end = p["words"][i + 1]["start"] if i + 1 < len(p["words"]) else p["end"]
            if w_end <= w_start:
                continue
            text = " ".join(f"{{\\c{highlight}}}{t}{{\\r}}" if j == i else t for j, t in enumerate(words))
            lines.append(f"Dialogue: 0,{_ts(w_start)},{_ts(w_end)},Default,,0,0,0,,{text}")
    return header + "\n".join(lines) + "\n"
