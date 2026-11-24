"""Posting rendered clips through each platform's official, free API.

Connect each account once with `python autopilot.py connect youtube|tiktok|meta`; tokens are kept in .tokens.json
and refreshed automatically. Uploads stream from disk, so even large clips never sit in memory.
"""
import base64
import hashlib
import json
import os
import secrets
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

import requests

from .config import ROOT, ClipperError

PLATFORMS = ("youtube", "tiktok", "instagram", "facebook")
TOKENS_FILE = ROOT / ".tokens.json"
REDIRECT_PORT = 8765
REDIRECT_URI = f"http://127.0.0.1:{REDIRECT_PORT}/callback/"  # register exactly this in the Google and TikTok apps
UPLOAD_TIMEOUT = (30, 1800)

GOOGLE_AUTH = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN = "https://oauth2.googleapis.com/token"
TIKTOK_AUTH = "https://www.tiktok.com/v2/auth/authorize/"
TIKTOK_API = "https://open.tiktokapis.com/v2"
TIKTOK_MAX_SINGLE_CHUNK = 64 * 1024 * 1024
TIKTOK_CHUNK = 10_000_000
META_PERMISSIONS = ("pages_show_list, pages_read_engagement, pages_manage_posts, business_management, "
                    "instagram_basic, instagram_content_publish")

_tokens_lock = threading.Lock()  # platforms upload in parallel and may refresh tokens at the same time


class NotConnected(ClipperError):
    """The account isn't connected or its sign-in expired; retrying won't help until it's reconnected."""


# ---------- token store ----------

def _tokens():
    return json.loads(TOKENS_FILE.read_text(encoding="utf-8")) if TOKENS_FILE.exists() else {}


def _store(platform, data):
    with _tokens_lock:
        tokens = _tokens()
        tokens[platform] = data
        tmp = TOKENS_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(tokens, indent=2), encoding="utf-8")
        tmp.replace(TOKENS_FILE)


def _need(platform):
    t = _tokens().get(platform)
    if not t:
        raise NotConnected(f"{platform} isn't connected. Run: python autopilot.py connect "
                           f"{'meta' if platform in ('instagram', 'facebook') else platform}")
    return t


def connected():
    return [p for p in PLATFORMS if p in _tokens()]


def account_name(platform):
    """The connected channel / page / account name, or '' if unknown."""
    return (_tokens().get(platform) or {}).get("name", "")


def _env(name):
    value = os.environ.get(name, "").strip()
    if not value:
        raise ClipperError(f"{name} is not set. Add it to .env (see README, 'Autopilot setup').")
    return value


# ---------- HTTP ----------

def _call(method, url, **kw):
    """requests.request that turns every API error style (Google, TikTok, Meta) into a readable ClipperError."""
    kw.setdefault("timeout", (30, 120))
    try:
        r = requests.request(method, url, **kw)
    except requests.RequestException as e:
        raise ClipperError(f"Network error talking to {urlparse(url).netloc}: {e}") from e
    try:
        r.data = r.json() if r.content else {}
    except ValueError:
        r.data = {}
    err = r.data.get("error") if isinstance(r.data, dict) else None
    code = err.get("code") if isinstance(err, dict) else err
    if r.status_code < 400 and code in (None, "", "ok"):
        return r
    msg = (err.get("message") if isinstance(err, dict) else None) or r.data.get("error_description") or err or r.text[:300]
    if r.status_code == 401 or code in (190, "invalid_grant", "access_token_invalid", "scope_not_authorized"):
        raise NotConnected(f"Sign-in rejected by {urlparse(url).netloc} ({msg}). Connect the account again.")
    raise ClipperError(f"{urlparse(url).netloc} error {r.status_code}{f' [{code}]' if code else ''}: {msg}")


def _browser_code(auth_url, state):
    """Open the sign-in page and catch the redirect on 127.0.0.1; returns the authorization code."""
    result = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            query = {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}
            if "code" not in query and "error" not in query:
                self.send_response(404)
                self.end_headers()
                return
            result.update(query)
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write("<h2>Done. You can close this tab and go back to the terminal.</h2>".encode())

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", REDIRECT_PORT), Handler)
    server.timeout = 10
    print(f"Opening your browser to sign in. If it doesn't open, visit:\n{auth_url}\n")
    webbrowser.open(auth_url)
    deadline = time.time() + 300
    try:
        while not result and time.time() < deadline:
            server.handle_request()
    finally:
        server.server_close()
    if not result:
        raise ClipperError("Sign-in timed out after 5 minutes.")
    if "error" in result:
        raise ClipperError(f"Sign-in was refused: {result.get('error_description') or result['error']}")
    if result.get("state") != state:
        raise ClipperError("Sign-in failed a security check (state mismatch). Try again.")
    return result["code"]


def _refresh(platform, url, params):
    """Cached access token, refreshed with the stored refresh token when it's about to expire."""
    t = _need(platform)
    if t.get("expires_at", 0) > time.time() + 60:
        return t["access_token"]
    data = _call("POST", url, data={**params, "grant_type": "refresh_token", "refresh_token": t["refresh_token"]}).data
    t.update(access_token=data["access_token"], expires_at=time.time() + int(data.get("expires_in", 3600)))
    if data.get("refresh_token"):  # TikTok rotates refresh tokens
        t["refresh_token"] = data["refresh_token"]
    _store(platform, t)
    return t["access_token"]


# ---------- YouTube (Shorts) ----------

def connect_youtube():
    client_id, secret = _env("YOUTUBE_CLIENT_ID"), _env("YOUTUBE_CLIENT_SECRET")
    verifier, state = secrets.token_urlsafe(64), secrets.token_urlsafe(16)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    code = _browser_code(GOOGLE_AUTH + "?" + urlencode({
        "client_id": client_id, "redirect_uri": REDIRECT_URI, "response_type": "code", "state": state,
        # readonly only to show which channel the account posts to; uploads need youtube.upload
        "scope": "https://www.googleapis.com/auth/youtube.upload https://www.googleapis.com/auth/youtube.readonly",
        "access_type": "offline", "prompt": "consent",
        "code_challenge": challenge, "code_challenge_method": "S256"}), state)
    data = _call("POST", GOOGLE_TOKEN, data={
        "code": code, "client_id": client_id, "client_secret": secret, "redirect_uri": REDIRECT_URI,
        "grant_type": "authorization_code", "code_verifier": verifier}).data
    channels = _call("GET", "https://www.googleapis.com/youtube/v3/channels", params={"part": "snippet", "mine": "true"},
                     headers={"Authorization": f"Bearer {data['access_token']}"}).data.get("items", [])
    if not channels:
        raise ClipperError("That Google account has no YouTube channel. Create one, or sign in with the account "
                           "(or Brand Account) that owns your channel.")
    name, cid = channels[0]["snippet"]["title"], channels[0]["id"]
    _store("youtube", {"refresh_token": data["refresh_token"], "access_token": data["access_token"],
                       "expires_at": time.time() + int(data.get("expires_in", 3600)),
                       "name": name, "channel_url": f"https://www.youtube.com/channel/{cid}"})
    return f"YouTube connected: clips will be posted to '{name}' (https://www.youtube.com/channel/{cid})."


def _youtube_token():
    try:
        return _refresh("youtube", GOOGLE_TOKEN, {"client_id": _env("YOUTUBE_CLIENT_ID"),
                                                  "client_secret": _env("YOUTUBE_CLIENT_SECRET")})
    except NotConnected as e:
        raise NotConnected(f"{e} If this happens every 7 days, set your Google OAuth consent screen to "
                           "'In production' (Testing mode expires sign-ins weekly).") from e


def post_youtube(path, text, privacy):
    token = _youtube_token()
    size = path.stat().st_size
    meta = {"snippet": {"title": text["title"], "description": text["description"], "tags": text["tags"],
                        "categoryId": "22"},
            "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": False}}
    init = _call("POST", "https://www.googleapis.com/upload/youtube/v3/videos",
                 params={"uploadType": "resumable", "part": "snippet,status"}, json=meta,
                 headers={"Authorization": f"Bearer {token}", "X-Upload-Content-Type": "video/mp4",
                          "X-Upload-Content-Length": str(size)})
    with open(path, "rb") as f:
        video = _call("PUT", init.headers["Location"], data=f, headers={"Content-Type": "video/mp4"},
                      timeout=UPLOAD_TIMEOUT).data
    return f"https://youtube.com/shorts/{video['id']}"


# ---------- TikTok ----------

def connect_tiktok():
    key, secret = _env("TIKTOK_CLIENT_KEY"), _env("TIKTOK_CLIENT_SECRET")
    verifier, state = secrets.token_urlsafe(64), secrets.token_urlsafe(16)
    code = _browser_code(TIKTOK_AUTH + "?" + urlencode({
        "client_key": key, "redirect_uri": REDIRECT_URI, "response_type": "code", "state": state,
        "scope": "user.info.basic,video.publish", "code_challenge_method": "S256",
        "code_challenge": hashlib.sha256(verifier.encode()).hexdigest()}), state)  # TikTok desktop wants hex, not base64
    data = _call("POST", f"{TIKTOK_API}/oauth/token/", data={
        "client_key": key, "client_secret": secret, "code": code, "grant_type": "authorization_code",
        "redirect_uri": REDIRECT_URI, "code_verifier": verifier}).data
    _store("tiktok", {"refresh_token": data["refresh_token"], "access_token": data["access_token"],
                      "expires_at": time.time() + int(data.get("expires_in", 86400))})
    return "TikTok connected."


def tiktok_chunks(size):
    """(chunk_size, count): one chunk up to 64 MB, else 10 MB chunks with the remainder folded into the last."""
    if size <= TIKTOK_MAX_SINGLE_CHUNK:
        return size, 1
    return TIKTOK_CHUNK, size // TIKTOK_CHUNK


def post_tiktok(path, caption, privacy):
    token = _refresh("tiktok", f"{TIKTOK_API}/oauth/token/", {"client_key": _env("TIKTOK_CLIENT_KEY"),
                                                              "client_secret": _env("TIKTOK_CLIENT_SECRET")})
    h = {"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=UTF-8"}
    creator = _call("POST", f"{TIKTOK_API}/post/publish/creator_info/query/", headers=h).data["data"]
    allowed = creator.get("privacy_level_options", [])
    if privacy not in allowed:
        raise NotConnected(f"TikTok won't accept privacy {privacy} for this account (allowed: {', '.join(allowed)}). "
                           "Until TikTok audits your app, set tiktok_privacy to SELF_ONLY in autopilot.json.")
    size = path.stat().st_size
    chunk, count = tiktok_chunks(size)
    init = _call("POST", f"{TIKTOK_API}/post/publish/video/init/", headers=h, json={
        "post_info": {"title": caption, "privacy_level": privacy, "disable_duet": False, "disable_comment": False,
                      "disable_stitch": False, "video_cover_timestamp_ms": 1000},
        "source_info": {"source": "FILE_UPLOAD", "video_size": size, "chunk_size": chunk, "total_chunk_count": count},
    }).data["data"]
    with open(path, "rb") as f:
        for i in range(count):
            start = i * chunk
            end = size - 1 if i == count - 1 else start + chunk - 1
            _call("PUT", init["upload_url"], data=f.read(end - start + 1), timeout=UPLOAD_TIMEOUT,
                  headers={"Content-Type": "video/mp4", "Content-Range": f"bytes {start}-{end}/{size}"})
    profile = f"https://www.tiktok.com/@{creator.get('creator_username', '')}"
    for _ in range(60):  # the upload is done; never raise from here or a retry would post twice
        time.sleep(5)
        try:
            s = _call("POST", f"{TIKTOK_API}/post/publish/status/fetch/", headers=h,
                      json={"publish_id": init["publish_id"]}).data["data"]
        except ClipperError:
            continue
        if s.get("status") == "FAILED":
            raise ClipperError(f"TikTok rejected the video: {s.get('fail_reason')}")
        if s.get("status") == "PUBLISH_COMPLETE":
            ids = s.get("publicaly_available_post_id") or []  # (sic) TikTok's spelling
            return f"{profile}/video/{ids[0]}" if ids else profile
    return profile


# ---------- Instagram + Facebook (one Meta login) ----------

def _graph():
    return "https://graph.facebook.com/" + os.environ.get("META_API_VERSION", "v23.0")


def connect_meta(user_token=None):
    app_id, secret = _env("META_APP_ID"), _env("META_APP_SECRET")
    if not user_token:
        print("1. Open https://developers.facebook.com/tools/explorer/ and choose your app.\n"
              f"2. Add these permissions: {META_PERMISSIONS}\n"
              "3. Click 'Generate Access Token', allow access to your Page and Instagram account, then copy the token.\n")
        user_token = input("Paste the token here: ").strip()
    long_lived = _call("GET", f"{_graph()}/oauth/access_token", params={
        "grant_type": "fb_exchange_token", "client_id": app_id, "client_secret": secret,
        "fb_exchange_token": user_token}).data["access_token"]
    pages = _call("GET", f"{_graph()}/me/accounts", params={
        "fields": "id,name,access_token,instagram_business_account{id,username}",
        "access_token": long_lived}).data.get("data", [])
    if not pages:
        raise ClipperError("That login has no Facebook Pages. Instagram and Facebook posting both need a Page "
                           "(with your Instagram professional account linked to it).")
    wanted = os.environ.get("META_PAGE_ID", "").strip()
    page = next((p for p in pages if p["id"] == wanted), None) if wanted else pages[0] if len(pages) == 1 else None
    if not page:
        for i, p in enumerate(pages, 1):
            print(f"{i}. {p['name']}")
        page = pages[int(input("Which Page should the clips go to? Number: ")) - 1]
    # a Page token made from a long-lived user token doesn't expire
    _store("facebook", {"page_id": page["id"], "name": page["name"], "token": page["access_token"]})
    ig = page.get("instagram_business_account")
    if ig:
        _store("instagram", {"ig_id": ig["id"], "name": ig.get("username", ""), "token": page["access_token"]})
        return f"Facebook Page '{page['name']}' and Instagram @{ig.get('username', '')} connected."
    return (f"Facebook Page '{page['name']}' connected. No Instagram account is linked to it: switch Instagram to a "
            "professional account, link it to this Page, then run connect meta again.")


def _rupload(url, path, token):
    with open(path, "rb") as f:
        return _call("POST", url, data=f, timeout=UPLOAD_TIMEOUT, headers={
            "Authorization": f"OAuth {token}", "offset": "0", "file_size": str(path.stat().st_size)})


def post_facebook(path, caption):
    t = _need("facebook")
    reels = f"{_graph()}/{t['page_id']}/video_reels"
    start = _call("POST", reels, data={"upload_phase": "start", "access_token": t["token"]}).data
    _rupload(start["upload_url"], path, t["token"])
    _call("POST", reels, data={"upload_phase": "finish", "video_id": start["video_id"], "video_state": "PUBLISHED",
                               "description": caption, "access_token": t["token"]})
    return f"https://www.facebook.com/reel/{start['video_id']}"


def post_instagram(path, caption):
    t = _need("instagram")
    api, token = _graph(), t["token"]
    container = _call("POST", f"{api}/{t['ig_id']}/media", data={
        "media_type": "REELS", "upload_type": "resumable", "caption": caption, "share_to_feed": "true",
        "access_token": token}).data
    _rupload(container.get("uri") or f"https://rupload.facebook.com/ig-api-upload/{api.rsplit('/', 1)[1]}/{container['id']}",
             path, token)
    for _ in range(120):  # Instagram transcodes before it allows publishing
        status = _call("GET", f"{api}/{container['id']}", params={"fields": "status_code,status",
                                                                  "access_token": token}).data
        if status.get("status_code") == "FINISHED":
            break
        if status.get("status_code") in ("ERROR", "EXPIRED"):
            raise ClipperError(f"Instagram couldn't process the video: {status.get('status')}")
        time.sleep(5)
    else:
        raise ClipperError("Instagram was still processing the video after 10 minutes.")
    media_id = _call("POST", f"{api}/{t['ig_id']}/media_publish",
                     data={"creation_id": container["id"], "access_token": token}).data["id"]
    try:
        return _call("GET", f"{api}/{media_id}", params={"fields": "permalink", "access_token": token}).data["permalink"]
    except (ClipperError, KeyError):  # already published: a missing link must not trigger a repost
        return f"instagram media {media_id}"


# ---------- shared ----------

def compose(clip, platform):
    """Platform-specific text from the AI's title, caption and hashtags."""
    tags = [t for t in clip.get("hashtags", []) if t.strip("#")]
    caption = clip.get("caption") or clip.get("title", "")
    if platform == "youtube":
        def clean(s):
            return s.replace("<", "").replace(">", "")  # YouTube rejects angle brackets
        return {"title": clean(clip.get("title") or caption)[:100],
                "description": clean(f"{caption}\n\n{' '.join(tags)}")[:5000],
                "tags": [t.lstrip("#") for t in tags][:15]}
    return {"caption": f"{caption}\n\n{' '.join(tags[:30])}".strip()[:2200]}  # Instagram allows 30 hashtags


def post(platform, path, clip, options):
    """Upload one clip; returns the post's URL. NotConnected means: reconnect, don't retry."""
    text = compose(clip, platform)
    if platform == "youtube":
        return post_youtube(path, text, options.get("youtube_privacy", "public"))
    if platform == "tiktok":
        return post_tiktok(path, text["caption"], options.get("tiktok_privacy", "SELF_ONLY"))
    if platform == "instagram":
        return post_instagram(path, text["caption"])
    if platform == "facebook":
        return post_facebook(path, text["caption"])
    raise ClipperError(f"Unknown platform {platform!r}.")


CONNECT = {"youtube": connect_youtube, "tiktok": connect_tiktok, "meta": connect_meta}
