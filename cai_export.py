#!/usr/bin/env python3
"""
Character.AI full account backup.

Status: works the same way the site does in a browser; verified 2026-11-14.
Published as-is; no support, no updates, no maintenance plan. If the site
changes and this breaks, it is dead.

Exports, for one or more character.ai accounts:
  - by default: every liked character (Character.AI exposes at most 10)
  - with --all: also every character you created (your own characters)
    and every character you have chats with
  - every chat (ACTIVE **and ARCHIVED**) of the selected characters
  - lorebooks, avatars

Output per account (SillyTavern-friendly):
  cards/<Name>.card.json        Character Card V2
  cards/<Name>.card.png         V2 card embedded in PNG (chara tEXt)
  cards/<Name>.avatar.<ext>     original avatar (animation preserved)
  chats/<Name>/<date>.jsonl     SillyTavern chat
  chats/<Name>/<date>.html      readable transcript
  chats/<Name>/<date>.raw.json  raw API turns
  chats/<Name>/_index.json
  lorebooks/<Name>.json
  user.json, personas.json
  _manifest.json, INCOMPLETE.md
  <account>-cai-backup-<ts>.zip

Why this works: neo.character.ai is not Cloudflare-protected and accepts
`Authorization: Token <hex>`. Archived chats 403 until they are temporarily
unarchived (PATCH /chat/{id}/unarchive), then they are re-archived.

Tokens: pass --token, --token-file, set CAI_TOKEN, or let the script recover
them from static HTML pages saved from the browser
(__NEXT_DATA__.pageProps.token).

Polite defaults: 0.4 s between requests with ±30% jitter, exponential backoff
on 429/5xx (honouring Retry-After), and a 15 s pause after every 10 characters.
Use --rate / --pause-every / --pause-seconds to tune, not to hammer.
"""
import argparse
import base64
import io
import json
import os
import random
import re
import sys
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import requests
from PIL import Image, PngImagePlugin

API = "https://neo.character.ai"
LEGACY = "https://plus.character.ai"
CDN = "https://characterai.io/i/400/static/avatars/"
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
NEXT_RE = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)
AVATAR_PX = 128

OUTPUT_README = """Character.AI backup - what to import

SillyTavern / JikoChat:
  cards/<Name>.card.png    Character Card V2 PNG - import THIS into
                           SillyTavern (Characters -> Import). The card JSON
                           is embedded in the PNG's "chara" metadata.

Other tools / archiving:
  cards/<Name>.card.json   The same card as JSON. Keep it: some tools and
                           scripts read JSON directly.

Not character cards:
  cards/<Name>.avatar.*    Original avatar image (animation preserved).
  chats/<Name>/*.jsonl     Chat logs; import per character, not as a card.

Extras: lorebooks/, user.json, personas.json, _manifest.json.
Archived chats are included when the server returned them; unrecoverable
legacy chats are listed in INCOMPLETE.md.
"""


# ---------------------------------------------------------------- helpers
LOG_FN = None


def log(msg=""):
    # The tkinter front-end sets LOG_FN; also tee to stdout so console builds
    # show progress. Windowed builds have no usable stdout, hence the guard.
    if LOG_FN is not None:
        try:
            LOG_FN(msg)
        except Exception:
            pass
    try:
        print(msg, flush=True)
    except Exception:
        pass


def sanitize(name, fallback="character"):
    s = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', "_", str(name or "")).strip().strip(".")
    s = re.sub(r"\s+", " ", s)
    return (s[:80] or fallback)


def fmt_iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def parse_ts(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def fmt_human(s):
    d = parse_ts(s)
    if not d:
        return ""
    return d.strftime("%Y-%m-%d %H:%M UTC")


def now_stamp():
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def dedupe_names(items, key):
    """Case-insensitive unique stems. items: list of dicts with 'name'."""
    used = set()
    for it in items:
        base = sanitize(it.get("name"), "character")
        stem = base
        n = 2
        while stem.lower() in used:
            stem = f"{base} ({n})"
            n += 1
        used.add(stem.lower())
        it[key] = stem


def esc(s):
    return (str(s or "").replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def format_body(text):
    """*actions* -> teal italic, "quotes" -> span. Single pass so the two
    transforms cannot rewrite each other's HTML attributes."""
    out, i = [], 0
    text = str(text or "")
    pattern = re.compile(r"(\*[^*\n]+\*)|(\"[^\"\n]+\")")
    for m in pattern.finditer(text):
        out.append(esc(text[i:m.start()]))
        if m.group(1):
            out.append(f'<em class="action">{esc(m.group(1)[1:-1])}</em>')
        else:
            out.append(f'<span class="quote">{esc(m.group(2))}</span>')
        i = m.end()
    out.append(esc(text[i:]))
    return "".join(out)


def downscale_png(image_bytes, px=AVATAR_PX):
    try:
        im = Image.open(io.BytesIO(image_bytes))
        im = im.convert("RGBA")
        im.thumbnail((px, px), Image.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, "PNG")
        return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()
    except Exception:
        return ""


def is_data_url(s):
    return bool(s and s.startswith("data:"))


def img_meta(data):
    """(width*height, animated, format) or None."""
    try:
        im = Image.open(io.BytesIO(data))
        return im.size[0] * im.size[1], bool(getattr(im, "is_animated", False)), (im.format or "").lower()
    except Exception:
        return None


def img_ext(fmt, ctype=""):
    fmt = fmt or ""
    ctype = ctype or ""
    if "gif" in fmt or "gif" in ctype:
        return "gif"
    if "png" in fmt:
        return "png"
    if "jpeg" in fmt or "jpg" in fmt:
        return "jpg"
    return "webp"


# ---------------------------------------------------------------- API client
class ApiError(Exception):
    def __init__(self, status, body, path):
        self.status, self.body, self.path = status, body, path
        super().__init__(f"HTTP {status} {path}: {str(body)[:200]}")


class Neo:
    def __init__(self, token, rate=0.4, pause_every=10, pause_seconds=15,
                 timeout=(10.0, 90.0), verbose=False, on_character=None):
        self.token = token
        self.rate = max(0.0, rate)
        self.pause_every = max(0, pause_every)
        self.pause_seconds = max(0.0, pause_seconds)
        self.timeout = timeout
        self.verbose = verbose
        self.on_character = on_character
        self.s = requests.Session()
        self.s.headers.update({
            "authorization": "Token " + token,
            "accept": "application/json",
            "user-agent": UA,
        })
        self._last = 0.0
        self._requests = 0
        self._characters = 0

    def _throttle(self):
        # Base delay plus ±30% jitter, so requests do not arrive in lockstep.
        target = self.rate * random.uniform(0.7, 1.3)
        wait = target - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()
        self._requests += 1

    def character_done(self):
        """Called after each character; long pause every N characters."""
        self._characters += 1
        if self.on_character is not None:
            self.on_character(self._characters)
        if self.pause_every and self._characters % self.pause_every == 0:
            log(f"  ... {self._characters} characters done; "
                f"pausing {self.pause_seconds:.0f}s to be gentle")
            time.sleep(self.pause_seconds)

    def req(self, method, url, *, retries=4, **kw):
        if not url.startswith("http"):
            url = API + url
        for attempt in range(retries):
            self._throttle()
            try:
                r = self.s.request(method, url, timeout=self.timeout, **kw)
            except requests.RequestException as e:
                if attempt == retries - 1:
                    raise ApiError(0, str(e), url)
                time.sleep(min(30.0, 2.0 ** attempt) + random.uniform(0, 1))
                continue
            if r.status_code == 200:
                return r
            if r.status_code == 401:
                raise ApiError(401, "token expired or invalid; get a fresh token "
                                    "from your browser (see README)", url)
            if r.status_code in (429, 500, 502, 503, 504) and attempt < retries - 1:
                ra = r.headers.get("retry-after")
                sleep = (float(ra) if ra and ra.replace(".", "", 1).isdigit()
                         else min(60.0, 2.0 ** attempt) + random.uniform(0, 1))
                if self.verbose:
                    log(f"    {r.status_code} on {url}, retry in {sleep:.0f}s")
                time.sleep(sleep)
                continue
            raise ApiError(r.status_code, r.text[:400], url)

    def json(self, method, url, **kw):
        return self.req(method, url, **kw).json()

    # --- endpoints -------------------------------------------------
    def me(self):
        return self.json("GET", "/user/")["user"]["user"]

    def liked(self):
        return self.json("GET", "/character/v1/upvoted_characters").get("characters", [])

    def created(self):
        return self.json("GET", "/character/v1/get_characters_created_by_user").get("characters", [])

    def all_chats(self):
        """Every chat on the account, including archived and removed.
        `GET /chats/` with no `character_ids` returns the full list (the UI's
        `/chats/recent/` only exposes the ~10 most recent)."""
        return self.json("GET", "/chats/", params={"limit": 1000}).get("chats", [])

    def recent_chats(self):
        chats, token, seen = [], "", set()
        while True:
            params = {"include_restricted": True}
            if token:
                params["next_token"] = token
            d = self.json("GET", "/chats/recent/", params=params)
            batch = d.get("chats", [])
            chats.extend(batch)
            token = (d.get("meta") or {}).get("next_token") or ""
            if not batch or not token or token in seen:
                break
            seen.add(token)
        return chats

    def char_info(self, cid, creator_view=False):
        body = {"external_id": cid, "lang": "en"}
        if creator_view:
            body["is_creator_view"] = True
        d = self.json("POST", "/character/v1/get_character_info", json=body)
        c = d.get("character")
        # deleted/private records return HTTP 200 with
        # {"character": [], "status": "NOT_OK", "error": "unauthorized", ...}
        if not isinstance(c, dict) or not c.get("name"):
            raise ApiError(200, d.get("error") or "character unavailable", cid)
        return c

    def chats_for(self, cid):
        out, token, seen = [], "", set()
        while True:
            params = {"character_ids": cid, "num_preview_turns": 0,
                      "include_turn_count": 1, "num_summaries": 0, "limit": 100}
            if token:
                params["next_token"] = token
            d = self.json("GET", "/chats/", params=params)
            batch = d.get("chats", [])
            out.extend(batch)
            token = (d.get("meta") or {}).get("next_token") or ""
            if not batch or not token or token in seen:
                break
            seen.add(token)
        return out

    def legacy_histories(self, cid):
        try:
            d = self.json("POST", LEGACY + "/chat/character/histories_v2/",
                          json={"external_id": cid, "number": 999})
            return d.get("histories") or []
        except ApiError:
            return []

    def chat_meta(self, chat_id):
        return self.json("GET", f"/chat/{chat_id}/").get("chat") or {}

    def unarchive(self, chat_id):
        return self.req("PATCH", f"/chat/{chat_id}/unarchive")

    def archive(self, chat_id):
        return self.req("PATCH", f"/chat/{chat_id}/archive")

    def resurrect(self, chat_id):
        return self.req("GET", f"/chat/{chat_id}/resurrect")

    def turns(self, chat_id):
        out, token, seen_ids, seen_tokens = [], "", set(), set()
        for _ in range(2000):
            params = {"next_token": token} if token else {}
            d = self.json("GET", f"/turns/{chat_id}/", params=params)
            batch = d.get("turns", []) or []
            new = 0
            for t in batch:
                tid = (t.get("turn_key") or {}).get("turn_id")
                if tid in seen_ids:
                    continue
                seen_ids.add(tid)
                out.append(t)
                new += 1
            token = (d.get("meta") or {}).get("next_token") or ""
            if not batch or not new or not token or token in seen_tokens:
                break
            seen_tokens.add(token)
        return out

    def legacy_messages(self, chat_id):
        msgs, page = [], None
        for _ in range(500):
            params = {"history_external_id": chat_id}
            if page:
                params["page_num"] = page
            d = self.json("GET", LEGACY + "/chat/history/msgs/user/", params=params)
            msgs = (d.get("messages") or []) + msgs
            if not d.get("has_more"):
                break
            page = d.get("next_page")
            if page is None:
                break
        return msgs

    def lorebook(self, lb_id):
        return self.json("GET", f"/lorebooks/{lb_id}/").get("lorebook") or {}

    def personas(self):
        try:
            return self.json("GET", "/character/v1/get_user_personas").get("personas", [])
        except ApiError:
            return []

    def image(self, filename, transform=False):
        """Avatar bytes.

        `transform=True` asks the CDN for its displayed 400px version
        (`?webp=true&anim=0`). Without it the CDN returns the raw upload, which
        for many bots is only 80x80. Neither request may advertise `image/avif`
        (Pillow cannot decode AVIF).
        """
        params = {"webp": "true", "anim": "0"} if transform else None
        self._throttle()
        r = requests.get(CDN + filename, params=params, timeout=self.timeout,
                         headers={"user-agent": UA, "accept": "image/webp,image/png,image/jpeg,image/*"})
        r.raise_for_status()
        return r.content, r.headers.get("content-type", "")


# ---------------------------------------------------------------- token discovery
def tokens_from_pages(pages_dir):
    found = {}
    if not pages_dir or not Path(pages_dir).is_dir():
        return found
    for p in sorted(Path(pages_dir).glob("*.html")):
        try:
            html = p.read_text(encoding="utf-8", errors="replace")
            m = NEXT_RE.search(html)
            if not m:
                continue
            tok = (json.loads(m.group(1)).get("props", {}).get("pageProps", {}) or {}).get("token")
            if tok:
                found.setdefault(tok, []).append(p.name)
        except Exception:
            continue
    return found


# ---------------------------------------------------------------- export
def convert_turns(turns, char):
    cid = char.get("external_id")
    msgs = []
    for t in turns:
        if t.get("state") and t.get("state") != "STATE_OK":
            continue
        cands = t.get("candidates") or []
        if not cands:
            continue
        pick = next((c for c in cands if c.get("candidate_id") == t.get("primary_candidate_id")), None)
        pick = pick or cands[-1]
        text = str(pick.get("raw_content") or "").strip()
        if not text:
            continue
        author = t.get("author") or {}
        is_user = bool(author.get("is_human")) or (author.get("author_id") not in (None, cid))
        msgs.append({
            "name": author.get("name") or ("You" if is_user else char.get("name") or "Character"),
            "is_user": is_user,
            "mes": text,
            "send_date": t.get("create_time") or "",
        })
    msgs.sort(key=lambda m: parse_ts(m["send_date"]) or datetime.min.replace(tzinfo=timezone.utc))
    return msgs


def convert_legacy(msgs):
    out = []
    for m in msgs:
        if m.get("is_alternative"):
            continue
        if m.get("src__name") is None:
            continue
        out.append({
            "name": m.get("src__name") or "",
            "is_user": bool(m.get("src__is_human")),
            "mes": str(m.get("text") or "").strip(),
            "send_date": m.get("created") or "",
        })
    out.sort(key=lambda m: parse_ts(m["send_date"]) or datetime.min.replace(tzinfo=timezone.utc))
    return [m for m in out if m["mes"]]


def st_jsonl(char_name, user_name, chat_meta, messages):
    head = {
        "user_name": user_name,
        "character_name": char_name,
        "create_date": fmt_iso(datetime.now(timezone.utc)),
        "chat_metadata": {
            "source": "character.ai",
            "chat_id": chat_meta.get("chat_id"),
            "character_id": chat_meta.get("character_id"),
            "original_state": chat_meta.get("state"),
            "original_create_time": chat_meta.get("create_time"),
        },
    }
    lines = [json.dumps(head, ensure_ascii=False)]
    for m in messages:
        lines.append(json.dumps({
            "name": m["name"],
            "is_user": m["is_user"],
            "is_system": False,
            "send_date": m["send_date"],
            "mes": m["mes"],
            "extra": {},
        }, ensure_ascii=False))
    return "\n".join(lines) + "\n"


HTML_HEAD = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Chat with {char}</title>
<style>
:root{{--bg-color:#0f0f0f;--text-color:#e5e5e5;--header-bg:#1a1a1a;--message-bg:#1a1a1a;
--user-message-bg:rgba(0,100,255,.1);--bot-message-bg:rgba(100,100,100,.1);--border-color:#333;
--timestamp-color:#6b7280;--accent-color:#ffdd6d}}
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,'Helvetica Neue',Arial,sans-serif;
background:var(--bg-color);color:var(--text-color);line-height:1.6}}
.container{{max-width:900px;margin:0 auto;padding:20px}}
.header{{background:var(--header-bg);border:1px solid var(--border-color);border-radius:12px;
padding:20px;margin-bottom:20px;text-align:center}}
.header h1{{font-size:1.5rem;margin-bottom:8px}}
.header .subtitle{{color:var(--timestamp-color);font-size:.9rem}}
.export-info{{font-size:.75rem;color:var(--timestamp-color);margin-top:12px;padding-top:12px;
border-top:1px solid var(--border-color)}}
.messages-container{{display:flex;flex-direction:column;gap:16px}}
.message{{display:flex;gap:12px;padding:16px;border-radius:12px;background:var(--message-bg);
border:1px solid var(--border-color)}}
.message.user{{background:var(--user-message-bg)}}
.message.bot{{background:var(--bot-message-bg)}}
.avatar-container{{flex-shrink:0}}
.avatar{{width:40px;height:40px;border-radius:50%;background-size:cover;background-position:center}}
.avatar-placeholder{{width:40px;height:40px;border-radius:50%;background:var(--accent-color);color:#fff;
display:flex;align-items:center;justify-content:center;font-weight:bold;font-size:1.2rem}}
.message-content{{flex:1;min-width:0}}
.message-header{{display:flex;align-items:center;gap:8px;margin-bottom:6px;flex-wrap:wrap}}
.sender-name{{font-weight:600}}
.timestamp{{font-size:.75rem;color:var(--timestamp-color)}}
.message-text{{word-wrap:break-word;font-size:16px}}
.message-text .action{{font-style:italic;color:#06B7DB}}
.message-text .quote{{font-style:normal}}
@media (max-width:600px){{.container{{padding:10px}}.message{{padding:12px}}
.avatar,.avatar-placeholder{{width:32px;height:32px;font-size:1rem}}}}{avatar_css}
</style></head><body>
<div class="container">
<div class="header">
<h1>Chat with {char}</h1>
{subtitle}
<div class="export-info">Exported on {exported} by cai_export.py<br>{count} messages</div>
</div>
<div class="messages-container">
{rows}
</div>
</div>
</body></html>
"""


def to_html(char_name, char_title, messages, user_name, char_avatar, user_avatar):
    rows = []
    for m in messages:
        is_user = m["is_user"]
        who = user_name if is_user else char_name
        av = user_avatar if is_user else char_avatar
        idx = 1 if is_user else 0
        if is_data_url(av):
            avatar = f'<div class="avatar avatar-img-{idx}" role="img" aria-label="{esc(who)}"></div>'
        else:
            avatar = f'<div class="avatar-placeholder">{esc((who or "?")[0].upper())}</div>'
        rows.append(
            f'<div class="message {"user" if is_user else "bot"}">\n'
            f'  <div class="avatar-container">{avatar}</div>\n'
            f'  <div class="message-content">\n'
            f'    <div class="message-header"><span class="sender-name">{esc(who)}</span>'
            f'<span class="timestamp">{esc(fmt_human(m["send_date"]))}</span></div>\n'
            f'    <div class="message-text">{format_body(m["mes"])}</div>\n'
            f'  </div>\n</div>')
    css = ""
    if is_data_url(char_avatar):
        css += f'\n.avatar-img-0{{background-image:url("{char_avatar}")}}'
    if is_data_url(user_avatar):
        css += f'\n.avatar-img-1{{background-image:url("{user_avatar}")}}'
    subtitle = f'<p class="subtitle">{esc(char_title)}</p>' if char_title else ""
    return HTML_HEAD.format(
        char=esc(char_name), subtitle=subtitle, rows="\n".join(rows),
        exported=esc(fmt_iso(datetime.now(timezone.utc))), count=len(messages),
        avatar_css=css)


def build_card(c, lorebook, account):
    desc = (c.get("definition") or "").strip() or (c.get("description") or "").strip()
    entries = []
    if lorebook and lorebook.get("lores"):
        lores = sorted(lorebook["lores"], key=lambda x: x.get("position") or 0)
        for i, l in enumerate(lores):
            content = str(l.get("description") or "").strip()
            if not content:
                continue
            keys = [k for k in (l.get("keywords") or []) if k]
            entries.append({
                "id": i, "keys": keys, "secondary_keys": [], "comment": l.get("name") or "",
                "content": content, "enabled": True, "constant": len(keys) == 0,
                "insertion_order": i, "extensions": {},
            })
    data = {
        "name": c.get("name") or "",
        "description": desc,
        "personality": "",
        "scenario": "",
        "first_mes": c.get("greeting") or "",
        "mes_example": "",
        "creator_notes": " — ".join(x for x in [c.get("title"), c.get("description")] if x),
        "system_prompt": "",
        "post_history_instructions": "",
        "alternate_greetings": c.get("additional_greetings") or [],
        "tags": [],
        "creator": c.get("user__username") or "",
        "character_version": str(c.get("updated") or "1"),
        "character_book": {
            "name": (lorebook or {}).get("name") or "",
            "description": (lorebook or {}).get("description") or "",
            "extensions": {},
            "entries": entries,
        } if lorebook else None,
        "extensions": {
            "source": "character.ai",
            "account": account.get("username"),
            "avatar_data_url": "",
            "character_ai": {
                "external_id": c.get("external_id"),
                "short_hash": c.get("short_hash"),
                "identifier": c.get("identifier"),
                "title": c.get("title") or "",
                "tagline": c.get("description") or "",
                "definition": c.get("definition") or "",
                "has_definition": c.get("has_definition"),
                "greeting": c.get("greeting") or "",
                "additional_greetings": c.get("additional_greetings") or [],
                "visibility": c.get("visibility"),
                "copyable": c.get("copyable"),
                "creator": c.get("user__username"),
                "created": c.get("created") or "",
                "updated": c.get("updated") or "",
                "upvotes": c.get("upvotes"),
                "interactions": c.get("participant__num_interactions"),
                "base_img_prompt": c.get("base_img_prompt") or "",
                "img_gen_enabled": c.get("img_gen_enabled"),
                "starter_prompts": c.get("starter_prompts"),
                "voice_id": c.get("voice_id") or c.get("default_voice_id"),
                "lorebook_id": c.get("lorebook_id"),
                "avatar_file_name": c.get("avatar_file_name"),
                "source_url": f"https://character.ai/character/{c.get('external_id')}",
            },
        },
    }
    return {"spec": "chara_card_v2", "spec_version": "2.0", "data": data}


def card_png(image_bytes, card):
    im = Image.open(io.BytesIO(image_bytes)).convert("RGBA")
    meta = PngImagePlugin.PngInfo()
    payload = json.dumps(card, ensure_ascii=False, separators=(",", ":"))
    meta.add_text("chara", base64.b64encode(payload.encode("utf-8")).decode("ascii"))
    buf = io.BytesIO()
    im.save(buf, "PNG", pnginfo=meta)
    return buf.getvalue()


def write_lorebook_st(lb):
    out = {"entries": {}}
    if not lb:
        return out
    lores = sorted(lb.get("lores") or [], key=lambda x: x.get("position") or 0)
    uid = 0
    for l in lores:
        content = str(l.get("description") or "").strip()
        if not content:
            continue
        keys = [k for k in (l.get("keywords") or []) if k]
        out["entries"][str(uid)] = {
            "uid": uid, "key": keys, "keysecondary": [],
            "comment": l.get("name") or "", "content": content,
            "constant": len(keys) == 0, "selective": True,
            "order": l.get("position") or 0, "disable": False,
            "probability": 100, "useProbability": True,
            "position": 0, "depth": 4, "caseSensitive": None,
            "matchWholeWords": None, "excludeRecursion": False,
            "preventRecursion": False, "delayUntilRecursion": False,
        }
        uid += 1
    return out


def export_account(api, account, out_root, *, cards=True, do_chats=True,
                   force=False, max_chats=None, skip_removed=True, full=False):
    username = account["username"]
    user_name = account.get("name") or username
    acc_dir = Path(out_root) / sanitize(username)
    (acc_dir / "cards").mkdir(parents=True, exist_ok=True)
    (acc_dir / "chats").mkdir(parents=True, exist_ok=True)
    (acc_dir / "lorebooks").mkdir(parents=True, exist_ok=True)
    log(f"\n=== Account: {username} ({user_name}) -> {acc_dir}")

    # 1. universe ----------------------------------------------------
    liked = api.liked()
    if full:
        created = api.created()
        allc = api.all_chats()
        recent = api.recent_chats()   # only used for display-name hints
        name_hint = {c.get("character_id"): c.get("character_name") for c in recent}
        log(f"mode=all liked={len(liked)} created={len(created)} "
            f"all_chats={len(allc)} "
            f"chat_characters={len({c.get('character_id') for c in allc})}")
    else:
        created, allc, name_hint = [], [], {}
        log(f"mode=liked liked={len(liked)} (use --all for the whole account)")

    universe, sources = {}, {}

    def add(cid, name, src):
        if not cid:
            return
        universe.setdefault(cid, {"external_id": cid, "name": name})
        sources.setdefault(cid, set()).add(src)

    for c in liked:
        add(c.get("external_id"), c.get("name"), "liked")
    for c in created:
        add(c.get("external_id"), c.get("name") or c.get("title"), "created")
    for c in allc:
        add(c.get("character_id"), name_hint.get(c.get("character_id")), "chatted")

    chars = []
    for cid, base in universe.items():
        owned = cid in {c.get("external_id") for c in created}
        info = None
        err = None
        try:
            info = api.char_info(cid, creator_view=owned)
        except ApiError as e:
            err = str(e)
        if info and info.get("name"):
            info["_sources"] = sorted(sources.get(cid, []))
            chars.append(info)
        else:
            base["_sources"] = sorted(sources.get(cid, []))
            base["_info_error"] = err
            chars.append(base)

    chars.sort(key=lambda c: (c.get("name") or "").lower())
    dedupe_names(chars, "_stem")
    log(f"characters={len(chars)}")

    # 2. chats list per character ------------------------------------
    chat_map = {}   # cid -> list of chat dicts
    if full:
        for c in allc:
            chat_map.setdefault(c.get("character_id"), []).append(c)
    else:
        # scoped to the selected (liked) characters only
        for ch in chars:
            cid = ch.get("external_id")
            try:
                chat_map[cid] = api.chats_for(cid)
            except ApiError as e:
                log(f"  ! chats {ch.get('name')}: {e}")
    for ch in chars:
        cid = ch.get("external_id")
        n = len(chat_map.get(cid, []))
        if n:
            log(f"  {ch.get('name')}: {n} chats")

    # 3. cards --------------------------------------------------------
    manifest_chars = []
    incomplete = []
    total_msgs = 0
    inaccessible = []
    exported_chats = 0

    for ch in chars:
        cid = ch.get("external_id")
        stem = ch["_stem"]
        name = ch.get("name") or stem
        cri = {"name": name, "external_id": cid, "stem": stem,
               "visibility": ch.get("visibility"), "creator": ch.get("user__username"),
               "sources": ch.get("_sources") or [], "chats": []}
        if ch.get("_info_error"):
            cri["info_error"] = ch["_info_error"]

        if cards and not ch.get("_info_error") and ch.get("name"):
            lb = None
            if ch.get("lorebook_id"):
                try:
                    lb = api.lorebook(ch["lorebook_id"])
                except ApiError as e:
                    log(f"  ! lorebook {name}: {e}")
            card = build_card(ch, lb, account)
            card_json_path = acc_dir / "cards" / f"{stem}.card.json"
            with open(card_json_path, "w", encoding="utf-8") as f:
                json.dump(card, f, ensure_ascii=False, indent=2)
            if lb and (lb.get("lores") or []):
                with open(acc_dir / "lorebooks" / f"{stem}.json", "w", encoding="utf-8") as f:
                    json.dump(write_lorebook_st(lb), f, ensure_ascii=False, indent=2)
                with open(acc_dir / "lorebooks" / f"{stem}.raw.json", "w", encoding="utf-8") as f:
                    json.dump(lb, f, ensure_ascii=False, indent=2)
            cri["card_json"] = str(card_json_path.relative_to(acc_dir))

            # avatar
            # The untransformed URL is a tiny mobile/chat thumbnail (often
            # 80x80); `?anim=0` makes the CDN render the real avatar (capped at
            # 400px, preserving the source's own odd dimensions).
            avatar_bytes = None
            card_bytes = None
            if ch.get("avatar_file_name"):
                fn = ch["avatar_file_name"]
                cdir = acc_dir / "cards"
                raw = big = None
                raw_ctype = ""
                try:
                    raw, raw_ctype = api.image(fn)
                except Exception as e:
                    log(f"  ! avatar raw {name}: {e}")
                try:
                    big, _ = api.image(fn, transform=True)
                except Exception:
                    big = None
                if big is None:
                    big = raw
                if raw is None:
                    raw = big
                if raw and big:
                    rmeta, bmeta = img_meta(raw), img_meta(big)
                    raw_anim = bool(rmeta and rmeta[1])
                    if raw_anim:
                        # animated original: keep animation + a static copy for the card
                        ext = img_ext(rmeta[2], raw_ctype)
                        ap = cdir / f"{stem}.avatar.{ext}"
                        ap.write_bytes(raw)
                        cri["avatar"] = str(ap.relative_to(acc_dir))
                        sp = cdir / f"{stem}.avatar.static.webp"
                        sp.write_bytes(big)
                        cri["avatar_static"] = str(sp.relative_to(acc_dir))
                        card_bytes = big
                    else:
                        # big is the real artwork; raw is a small thumbnail
                        ap = cdir / f"{stem}.avatar.webp"
                        ap.write_bytes(big)
                        cri["avatar"] = str(ap.relative_to(acc_dir))
                        if rmeta and bmeta and rmeta[0] < bmeta[0]:
                            tp = cdir / f"{stem}.avatar.thumb.{img_ext(rmeta[2], raw_ctype)}"
                            tp.write_bytes(raw)
                            cri["avatar_thumb"] = str(tp.relative_to(acc_dir))
                        card_bytes = big
                    avatar_bytes = card_bytes
                    # drop stale file names from earlier exporter versions
                    old = cdir / f"{stem}.avatar.400.webp"
                    if old.exists():
                        old.unlink()
                    card["data"]["extensions"]["avatar_data_url"] = downscale_png(card_bytes)
                    with open(card_json_path, "w", encoding="utf-8") as f:
                        json.dump(card, f, ensure_ascii=False, indent=2)
                    try:
                        png = card_png(card_bytes, card)
                        (cdir / f"{stem}.card.png").write_bytes(png)
                        cri["card_png"] = f"cards/{stem}.card.png"
                    except Exception as e:
                        log(f"  ! card png {name}: {e}")
                elif raw:
                    card_bytes = raw
                    avatar_bytes = raw
                    card["data"]["extensions"]["avatar_data_url"] = downscale_png(raw)
                    with open(card_json_path, "w", encoding="utf-8") as f:
                        json.dump(card, f, ensure_ascii=False, indent=2)
                    try:
                        (cdir / f"{stem}.card.png").write_bytes(card_png(raw, card))
                        cri["card_png"] = f"cards/{stem}.card.png"
                    except Exception as e:
                        log(f"  ! card png {name}: {e}")

            desc = (ch.get("definition") or "").strip() or (ch.get("description") or "").strip()
            greet = (ch.get("greeting") or "").strip()
            missing = []
            if not desc:
                missing.append("description/definition")
            if not greet:
                missing.append("greeting")
            if missing:
                cri["incomplete"] = missing
                incomplete.append((name, cid, ", ".join(missing)))
        else:
            avatar_bytes = card_bytes = None
            if ch.get("avatar_file_name"):
                try:
                    card_bytes, _ = api.image(ch["avatar_file_name"], transform=True)
                    avatar_bytes = card_bytes
                except Exception:
                    pass

        # chats -------------------------------------------------------
        chat_list = chat_map.get(cid, [])
        if chat_list and do_chats:
            char_avatar = downscale_png(avatar_bytes or card_bytes or b"") if (avatar_bytes or card_bytes) else ""
            chdir = acc_dir / "chats" / stem
            chdir.mkdir(parents=True, exist_ok=True)
            index = []
            used_names = set()
            chat_list.sort(key=lambda x: x.get("create_time") or "")
            for ci, chat in enumerate(chat_list):
                chat_id = chat.get("chat_id")
                state = chat.get("state")
                if state == "STATE_REMOVED":
                    rec = {"chat_id": chat_id, "state": state,
                           "create_time": chat.get("create_time"),
                           "storage": chat.get("storage"), "inaccessible": "STATE_REMOVED"}
                    cri["chats"].append(rec)
                    inaccessible.append({"character": name, "character_id": cid,
                                         "chat_id": chat_id, "state": state,
                                         "create_time": chat.get("create_time")})
                    continue
                if max_chats is not None and exported_chats >= max_chats:
                    continue

                # filename from create_time
                dt = parse_ts(chat.get("create_time"))
                base = dt.strftime("%Y%m%d-%H%M") if dt else f"chat-{ci+1:03d}"
                fname = base
                n = 2
                while fname.lower() in used_names:
                    fname = f"{base}-{n}"
                    n += 1
                used_names.add(fname.lower())
                raw_path = chdir / f"{fname}.raw.json"

                messages = None
                if raw_path.exists() and not force:
                    try:
                        raw = json.loads(raw_path.read_text(encoding="utf-8"))
                        messages = raw.get("messages") or []
                        chat = dict(chat, **{k: v for k, v in raw.get("chat", {}).items() if k in ("create_time",)})
                    except Exception:
                        messages = None

                if messages is None:
                    meta = {"chat_id": chat_id, "character_id": cid,
                            "state": state, "create_time": chat.get("create_time")}
                    turns = []
                    legacy_msgs = []
                    restored = False
                    try:
                        if state == "STATE_ARCHIVED":
                            try:
                                api.unarchive(chat_id)
                                restored = True
                            except ApiError as e:
                                log(f"  ! unarchive {name}/{chat_id}: {e}")
                        try:
                            turns = api.turns(chat_id)
                        except ApiError as e:
                            if e.status == 403:
                                try:
                                    api.resurrect(chat_id)
                                    turns = api.turns(chat_id)
                                except ApiError:
                                    turns = []
                            if not turns:
                                try:
                                    legacy_msgs = api.legacy_messages(chat_id)
                                except ApiError:
                                    legacy_msgs = []
                    finally:
                        if restored:
                            try:
                                api.archive(chat_id)
                            except ApiError as e:
                                log(f"  ! re-archive {name}/{chat_id}: {e}")

                    if turns:
                        messages = convert_turns(turns, ch)
                        raw = {"chat": meta, "messages": messages, "turns": turns, "source": "neo"}
                    elif legacy_msgs:
                        messages = convert_legacy(legacy_msgs)
                        raw = {"chat": meta, "messages": messages, "legacy": legacy_msgs, "source": "legacy"}
                    else:
                        messages = []
                        raw = {"chat": meta, "messages": [], "source": "none"}
                    if not messages:
                        log(f"  ! no messages {name}/{chat_id} ({state})")
                    with open(raw_path, "w", encoding="utf-8") as f:
                        json.dump(raw, f, ensure_ascii=False, indent=1)

                if not messages:
                    rec = {"chat_id": chat_id, "state": state,
                           "create_time": chat.get("create_time"), "inaccessible": "no_messages"}
                    cri["chats"].append(rec)
                    inaccessible.append({"character": name, "character_id": cid,
                                         "chat_id": chat_id, "state": state,
                                         "create_time": chat.get("create_time")})
                    continue

                (chdir / f"{fname}.jsonl").write_text(
                    st_jsonl(name, user_name, {"chat_id": chat_id, "character_id": cid,
                                                "state": state, "create_time": chat.get("create_time")},
                             messages), encoding="utf-8")
                (chdir / f"{fname}.html").write_text(
                    to_html(name, ch.get("title") or ch.get("description") or "",
                            messages, user_name, char_avatar, USER_AVATAR),
                    encoding="utf-8")

                total_msgs += len(messages)
                exported_chats += 1
                rec = {"chat_id": chat_id, "state": state,
                       "create_time": chat.get("create_time"),
                       "messages": len(messages), "file": f"{fname}.jsonl"}
                cri["chats"].append(rec)
                index.append({"file": fname, "chat_id": chat_id, "state": state,
                              "create_time": chat.get("create_time"),
                              "messages": len(messages),
                              "first_message": messages[0]["mes"][:160],
                              "last_message": messages[-1]["mes"][:160]})
                log(f"  {name}: {fname} [{state}] {len(messages)} msgs")

            if index:
                with open(chdir / "_index.json", "w", encoding="utf-8") as f:
                    json.dump(index, f, ensure_ascii=False, indent=2)

        api.character_done()
        manifest_chars.append(cri)

    # 4. account files ------------------------------------------------
    try:
        (acc_dir / "README.txt").write_text(OUTPUT_README, encoding="utf-8")
    except OSError:
        pass
    try:
        # Never write the session token to disk or into the backup zip.
        safe_account = {k: v for k, v in account.items() if k != "token"}
        with open(acc_dir / "user.json", "w", encoding="utf-8") as f:
            json.dump(safe_account, f, ensure_ascii=False, indent=2)
    except Exception:
        pass
    try:
        personas = api.personas()
        if personas:
            with open(acc_dir / "personas.json", "w", encoding="utf-8") as f:
                json.dump(personas, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

    manifest = {
        "generated": fmt_iso(datetime.now(timezone.utc)),
        "tool": "cai_export.py",
        "account": {"username": username, "name": user_name, "id": account.get("id")},
        "summary": {
            "characters": len(manifest_chars),
            "chats_exported": exported_chats,
            "messages": total_msgs,
            "chats_inaccessible": len(inaccessible),
            "incomplete_characters": len(incomplete),
        },
        "incomplete": [{"name": n, "external_id": i, "missing": m} for n, i, m in incomplete],
        "inaccessible_chats": inaccessible,
        "characters": manifest_chars,
    }
    with open(acc_dir / "_manifest.json", "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)

    lines = ["# Incomplete characters", "",
             "These characters have no public description/definition and/or no greeting.",
             "Nothing more can be recovered from the API; the greeting and avatar are still saved.", ""]
    for n, i, m in incomplete:
        lines.append(f"- **{n}** (`{i}`) — missing: {m}")
    (acc_dir / "INCOMPLETE.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    log(f"  summary: {manifest['summary']}")

    # 5. zip ----------------------------------------------------------
    zip_path = Path(out_root) / f"{sanitize(username)}-cai-backup-{now_stamp()}.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for p in sorted(acc_dir.rglob("*")):
            if p.is_file():
                z.write(p, p.relative_to(Path(out_root)))
    log(f"  zip: {zip_path} ({zip_path.stat().st_size/1024/1024:.1f} MB)")
    return manifest


USER_AVATAR = ""  # set per account


def main():
    ap = argparse.ArgumentParser(
        description="Character.AI account backup (liked by default; --all for the whole account)")
    ap.add_argument("--pages", default=None, help="optional dir with saved *.html pages (token source)")
    ap.add_argument("--token", action="append", default=[], help="API token (repeatable; or set CAI_TOKEN)")
    ap.add_argument("--token-file", help="file with one token per line")
    ap.add_argument("--account", action="append", default=[], help="only these usernames (repeatable)")
    ap.add_argument("--all", action="store_true",
                    help="back up the whole account, not just the liked characters")
    ap.add_argument("--out", default="export", help="output directory")
    ap.add_argument("--rate", type=float, default=0.4,
                    help="base seconds between requests, +/-30%% jitter (default 0.4)")
    ap.add_argument("--pause-every", type=int, default=10,
                    help="pause after this many characters; 0 = never (default 10)")
    ap.add_argument("--pause-seconds", type=float, default=15.0,
                    help="length of the character pause (default 15)")
    ap.add_argument("--timeout", type=float, default=90.0,
                    help="read timeout in seconds; connect timeout is 10 (default 90)")
    ap.add_argument("--cards-only", action="store_true")
    ap.add_argument("--chats-only", action="store_true")
    ap.add_argument("--force", action="store_true", help="re-fetch chats even if raw json exists")
    ap.add_argument("--max-chats", type=int, default=None, help="limit chats (testing)")
    ap.add_argument("--dry-run", action="store_true", help="inventory only, write nothing")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    toks = [t.strip() for t in os.environ.get("CAI_TOKEN", "").split(",") if t.strip()]
    toks += list(args.token)
    if args.token_file:
        toks += [l.strip() for l in Path(args.token_file).read_text().splitlines()
                 if l.strip() and not l.strip().startswith("#")]
    if args.pages:
        toks += list(tokens_from_pages(args.pages).keys())
    seen, tokens = set(), []
    for t in toks:
        if t and t not in seen:
            seen.add(t)
            tokens.append(t)
    if not tokens:
        sys.exit("No tokens found. Pass --token, --token-file, set CAI_TOKEN, "
                 "or save pages into --pages.")

    Path(args.out).mkdir(parents=True, exist_ok=True)
    accounts = []
    for i, t in enumerate(tokens, 1):
        try:
            api = Neo(t, rate=args.rate,
                      pause_every=args.pause_every,
                      pause_seconds=args.pause_seconds,
                      timeout=(10.0, args.timeout),
                      verbose=args.verbose)
            me = api.me()
        except Exception as e:
            log(f"! token #{i} failed: {e}")
            continue
        accounts.append((api, {"username": me.get("username"),
                               "name": me.get("name") or me.get("first_name") or (me.get("account") or {}).get("name"),
                               "id": me.get("id"), "token": t,
                               "avatar_file_name": (me.get("account") or {}).get("avatar_file_name")}))
        log(f"token #{i} -> {me.get('username')} ({me.get('name')})")

    if args.account:
        wanted = {a.lower() for a in args.account}
        accounts = [(a, acc) for a, acc in accounts if (acc["username"] or "").lower() in wanted]

    if args.dry_run:
        for api, acc in accounts:
            log(f"\n=== {acc['username']}:")
            liked = api.liked()
            if args.all:
                created = api.created()
                allc = api.all_chats()
                ids = {c.get("external_id") for c in liked} | {c.get("external_id") for c in created} \
                    | {c.get("character_id") for c in allc}
                states = {}
                for c in allc:
                    states[c.get("state")] = states.get(c.get("state"), 0) + 1
                log(f"  mode=all liked={len(liked)} created={len(created)}"
                    f" characters={len(ids)} chats={len(allc)} states={states}")
            else:
                n = 0
                for c in liked:
                    try:
                        n += len(api.chats_for(c.get("external_id")))
                    except ApiError as e:
                        log(f"  ! chats {c.get('name')}: {e}")
                log(f"  mode=liked liked={len(liked)} chats={n} "
                    f"(use --all for the whole account)")
        return

    for api, acc in accounts:
        global USER_AVATAR
        USER_AVATAR = ""
        if acc.get("avatar_file_name"):
            try:
                raw, _ = api.image(acc["avatar_file_name"])
                USER_AVATAR = downscale_png(raw)
            except Exception:
                pass
        export_account(api, acc, args.out,
                       cards=not args.chats_only,
                       do_chats=not args.cards_only,
                       force=args.force,
                       max_chats=args.max_chats,
                       full=args.all)


if __name__ == "__main__":
    main()
