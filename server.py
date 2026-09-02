"""
YouTube MCP — remote (streamable-HTTP) MCP server for Claude web / ChatGPT web.

Wraps the YouTube Data API v3 + transcripts + the "most replayed" heatmap, with
AUTOMATIC API-KEY ROTATION across every YOUTUBE_API_KEY* found in the environment.
When one key hits its daily quota (HTTP 403 quotaExceeded / 429), the server
advances to the next key and retries the same call. When every key is exhausted
it returns a clear error instead of failing silently.

Tool names mirror what the Studio prompts ask for:
  search_videos · get_video_metadata · get_channel_info · get_channel_videos
  get_video_comments · get_transcript · get_clean_transcript · search_transcript
  extract_chapters · get_most_replayed · calculate_engagement · get_trending_videos

RUN LOCALLY
  python -m venv .venv && .venv\\Scripts\\activate        (Windows)
  pip install -r requirements.txt
  # keys: copy .env.example -> .env and fill, OR export them, OR rely on the
  # repo-root .env (this file loads ../../.env automatically if present)
  python server.py                       ->  http://127.0.0.1:8765/mcp

EXPOSE FOR THE WEB CONNECTORS  (Claude web / ChatGPT need a public HTTPS URL)
  cloudflared tunnel --url http://127.0.0.1:8765         (free, no account)
  #   -> prints https://<random>.trycloudflare.com  ; the MCP URL is that + /mcp

ADD TO CLAUDE WEB
  Settings -> Connectors -> Add custom connector
  Name: YouTube    URL: https://<your-tunnel>/mcp
ADD TO CHATGPT  (Plus/Pro/Team/Enterprise)
  Settings -> Connectors (or Developer mode) -> add MCP server -> same URL

SECURITY
  This server is unauthenticated by default. Set MCP_BEARER_TOKEN in the env to
  require  Authorization: Bearer <token>  on every request (paste the same token
  into the connector's auth field). Do not post the tunnel URL publicly.
"""
from __future__ import annotations

import os
import re
import json
import html
import threading
from pathlib import Path
from typing import Any

import httpx

try:
    from fastmcp import FastMCP
except ImportError:  # pragma: no cover
    raise SystemExit("pip install -r requirements.txt  (fastmcp not installed)")

API = "https://www.googleapis.com/youtube/v3"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


# --------------------------------------------------------------------------- env
def _load_dotenv() -> None:
    """Load KEY=VALUE lines from the first .env found walking up from here.
    Never overrides a variable already set in the real environment."""
    here = Path(__file__).resolve()
    for parent in [here.parent, *here.parents]:
        f = parent / ".env"
        if f.is_file():
            for line in f.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
            return


_load_dotenv()


def _load_keys() -> list[str]:
    """Every distinct YouTube Data API key in the env, in a stable order:
    YOUTUBE_API_KEY, _1, _2, ... then YOUTUBE_API_KEYS='a,b,c'."""
    out: list[str] = []
    seen: set[str] = set()

    def add(v: str) -> None:
        v = (v or "").strip()
        if v and v not in seen:
            seen.add(v)
            out.append(v)

    add(os.environ.get("YOUTUBE_API_KEY", ""))
    for i in range(1, 16):
        add(os.environ.get(f"YOUTUBE_API_KEY_{i}", ""))
    for v in (os.environ.get("YOUTUBE_API_KEYS", "") or "").split(","):
        add(v)
    return out


KEYS = _load_keys()
_key_lock = threading.Lock()
_key_idx = 0
_client = httpx.Client(timeout=30, headers={"User-Agent": UA})


class QuotaExhausted(RuntimeError):
    pass


def yt_get(path: str, params: dict[str, Any]) -> dict:
    """GET {API}/{path} with the current key; on quota errors rotate + retry
    through every key exactly once."""
    global _key_idx
    if not KEYS:
        raise RuntimeError(
            "No YouTube Data API key found. Set YOUTUBE_API_KEY (and optionally "
            "YOUTUBE_API_KEY_2, _3, ...) in the environment or a .env file.")
    tried: list[str] = []
    for _ in range(len(KEYS)):
        with _key_lock:
            cur = _key_idx % len(KEYS)
            key = KEYS[cur]
        p = {k: v for k, v in params.items() if v is not None}
        p["key"] = key
        r = _client.get(f"{API}/{path}", params=p)
        if r.status_code == 200:
            return r.json()
        body = r.text[:800]
        low = body.lower()
        is_quota = r.status_code in (403, 429) and (
            "quota" in low or "ratelimitexceeded" in low or "userratelimit" in low)
        if is_quota:
            tried.append(f"key#{cur} -> {r.status_code} quota")
            with _key_lock:
                if _key_idx % len(KEYS) == cur:
                    _key_idx += 1
            continue
        raise RuntimeError(f"YouTube API {r.status_code} on {path}: {body}")
    raise QuotaExhausted(
        "All %d API key(s) are quota-exhausted for today. Details: %s"
        % (len(KEYS), "; ".join(tried)))


# ------------------------------------------------------------------- small utils
def _as_id_list(video_ids: Any, cap: int = 50) -> list[str]:
    if isinstance(video_ids, str):
        parts = re.split(r"[^A-Za-z0-9_-]+", video_ids)
    else:
        parts = list(video_ids or [])
    ids = [p for p in (s.strip() for s in parts) if len(p) >= 6]
    return ids[:cap]


def _int(x: Any) -> int:
    try:
        return int(x)
    except (TypeError, ValueError):
        return 0


def _resolve_channel_id(channel_id: str | None, handle: str | None) -> str:
    if channel_id and channel_id.startswith("UC"):
        return channel_id
    q = {"part": "id"}
    if handle:
        q["forHandle"] = handle.lstrip("@")
    elif channel_id:
        q["forUsername"] = channel_id
    else:
        raise ValueError("pass channel_id (UC...) or handle (@name)")
    data = yt_get("channels", q)
    items = data.get("items") or []
    if not items:
        # last resort: search
        s = yt_get("search", {"part": "snippet", "type": "channel",
                              "q": handle or channel_id, "maxResults": 1})
        it = s.get("items") or []
        if it:
            return it[0]["snippet"]["channelId"]
        raise ValueError(f"channel not found: {handle or channel_id}")
    return items[0]["id"]


def _uploads_playlist(channel_id: str) -> str:
    data = yt_get("channels", {"part": "contentDetails", "id": channel_id})
    items = data.get("items") or []
    if not items:
        raise ValueError(f"channel not found: {channel_id}")
    return items[0]["contentDetails"]["relatedPlaylists"]["uploads"]


TS = re.compile(r"(?:(\d{1,2}):)?(\d{1,2}):(\d{2})")


# ------------------------------------------------------------------------- MCP
mcp = FastMCP(
    name="youtube",
    instructions=(
        "YouTube research tools backed by the YouTube Data API v3 with API-key "
        "rotation. Use search_videos + get_channel_videos + get_video_metadata "
        "for census/stats, get_transcript/get_clean_transcript for why a video "
        "holds attention (free, no quota), get_most_replayed for retention "
        "spikes, get_video_comments for audience demand. Every number is live."),
)


@mcp.tool
def list_api_keys() -> dict:
    """How many YouTube API keys are loaded and which one is currently active
    (values are masked). Useful to confirm key rotation is wired up."""
    with _key_lock:
        active = _key_idx % len(KEYS) if KEYS else None
    return {
        "keys_loaded": len(KEYS),
        "active_index": active,
        "masked": [k[:6] + "…" + k[-4:] for k in KEYS],
        "transcript_proxy_configured": PROXY_ON,
        "note": ("Data API tools work from any host. Transcript tools "
                 "(get_transcript / get_clean_transcript / search_transcript) "
                 "need a residential proxy when the server runs on a cloud IP — "
                 "set YT_PROXY_URL or WEBSHARE_PROXY_USERNAME/PASSWORD."
                 if not PROXY_ON else "transcript proxy is set."),
    }


@mcp.tool
def search_videos(query: str, order: str = "relevance", max_results: int = 25,
                  published_after: str | None = None,
                  channel_id: str | None = None,
                  region_code: str | None = None) -> dict:
    """Search YouTube. order = relevance | date | viewCount | rating | title.
    published_after is an RFC3339 date (e.g. 2025-01-01T00:00:00Z). Returns
    video id + title + channel + publishedAt; call get_video_metadata on the
    ids for view/like/comment counts."""
    data = yt_get("search", {
        "part": "snippet", "type": "video", "q": query,
        "order": order, "maxResults": max(1, min(int(max_results), 50)),
        "publishedAfter": published_after, "channelId": channel_id,
        "regionCode": region_code,
    })
    out = []
    for it in data.get("items", []):
        s = it.get("snippet", {})
        out.append({
            "video_id": it["id"]["videoId"],
            "title": s.get("title"),
            "channel": s.get("channelTitle"),
            "channel_id": s.get("channelId"),
            "published_at": s.get("publishedAt"),
            "description": (s.get("description") or "")[:200],
        })
    return {"query": query, "order": order, "count": len(out), "results": out}


@mcp.tool
def get_video_metadata(video_ids: Any) -> dict:
    """Real stats for up to 50 videos at once. Pass a list of ids or a
    whitespace/comma string. Returns views, likes, comments, duration,
    publishedAt, tags, and the full description per video."""
    ids = _as_id_list(video_ids, cap=50)
    if not ids:
        return {"count": 0, "videos": []}
    data = yt_get("videos", {
        "part": "snippet,statistics,contentDetails", "id": ",".join(ids)})
    out = []
    for it in data.get("items", []):
        s, st, cd = it.get("snippet", {}), it.get("statistics", {}), it.get("contentDetails", {})
        out.append({
            "video_id": it["id"],
            "title": s.get("title"),
            "channel": s.get("channelTitle"),
            "channel_id": s.get("channelId"),
            "published_at": s.get("publishedAt"),
            "duration": cd.get("duration"),
            "views": _int(st.get("viewCount")),
            "likes": _int(st.get("likeCount")),
            "comments": _int(st.get("commentCount")),
            "tags": s.get("tags", [])[:25],
            "description": s.get("description", ""),
            "thumbnail": (s.get("thumbnails", {}).get("maxres")
                          or s.get("thumbnails", {}).get("high")
                          or {}).get("url"),
        })
    return {"count": len(out), "videos": out}


@mcp.tool
def get_channel_info(channel_id: str | None = None, handle: str | None = None) -> dict:
    """Channel subs, total views, video count, creation date. Pass channel_id
    (UC...) or handle (@name)."""
    cid = _resolve_channel_id(channel_id, handle)
    data = yt_get("channels", {"part": "snippet,statistics,contentDetails", "id": cid})
    items = data.get("items") or []
    if not items:
        raise ValueError(f"channel not found: {cid}")
    it = items[0]
    s, st = it.get("snippet", {}), it.get("statistics", {})
    return {
        "channel_id": cid,
        "title": s.get("title"),
        "handle": s.get("customUrl"),
        "published_at": s.get("publishedAt"),
        "country": s.get("country"),
        "subscribers": _int(st.get("subscriberCount")),
        "total_views": _int(st.get("viewCount")),
        "video_count": _int(st.get("videoCount")),
        "uploads_playlist": it["contentDetails"]["relatedPlaylists"]["uploads"],
        "description": s.get("description", "")[:500],
    }


@mcp.tool
def get_channel_videos(channel_id: str | None = None, handle: str | None = None,
                       order: str = "date", max_results: int = 50) -> dict:
    """Up to ~200 of a channel's uploads with full stats. order = date |
    viewCount (viewCount fetches the recent window then sorts by views —
    say so when you report). Use this + get_channel_info to normalize a
    winner: multiple = views / median of the ~10 uploads before it."""
    cid = _resolve_channel_id(channel_id, handle)
    playlist = _uploads_playlist(cid)
    want = max(1, min(int(max_results), 200))
    ids: list[str] = []
    page: str | None = None
    while len(ids) < want:
        data = yt_get("playlistItems", {
            "part": "contentDetails", "playlistId": playlist,
            "maxResults": 50, "pageToken": page})
        for it in data.get("items", []):
            vid = it["contentDetails"].get("videoId")
            if vid:
                ids.append(vid)
        page = data.get("nextPageToken")
        if not page:
            break
    ids = ids[:want]
    vids: list[dict] = []
    for i in range(0, len(ids), 50):
        chunk = get_video_metadata(ids[i:i + 50])["videos"]
        vids.extend(chunk)
    if order == "viewCount":
        vids.sort(key=lambda v: v.get("views", 0), reverse=True)
    med = 0
    if vids:
        vs = sorted(v.get("views", 0) for v in vids)
        med = vs[len(vs) // 2]
    for v in vids:
        v["view_multiple_vs_channel_median"] = round(v["views"] / med, 2) if med else None
    return {"channel_id": cid, "order": order, "count": len(vids),
            "channel_median_views": med, "videos": vids}


@mcp.tool
def get_video_comments(video_id: str, max_results: int = 50,
                       order: str = "relevance") -> dict:
    """Top-level comments. order = relevance | time. High-like 'what about X?'
    comments are pre-validated sub-topics and objections."""
    data = yt_get("commentThreads", {
        "part": "snippet", "videoId": video_id, "order": order,
        "maxResults": max(1, min(int(max_results), 100)), "textFormat": "plainText"})
    out = []
    for it in data.get("items", []):
        c = it["snippet"]["topLevelComment"]["snippet"]
        out.append({
            "author": c.get("authorDisplayName"),
            "text": c.get("textDisplay"),
            "likes": _int(c.get("likeCount")),
            "published_at": c.get("publishedAt"),
            "replies": _int(it["snippet"].get("totalReplyCount")),
        })
    out.sort(key=lambda x: x["likes"], reverse=True)
    return {"video_id": video_id, "count": len(out), "comments": out}


@mcp.tool
def get_trending_videos(region_code: str = "US", category_id: str | None = None,
                        max_results: int = 25) -> dict:
    """Most-popular chart for a region (optionally a videoCategoryId)."""
    data = yt_get("videos", {
        "part": "snippet,statistics", "chart": "mostPopular",
        "regionCode": region_code, "videoCategoryId": category_id,
        "maxResults": max(1, min(int(max_results), 50))})
    out = []
    for it in data.get("items", []):
        s, st = it.get("snippet", {}), it.get("statistics", {})
        out.append({
            "video_id": it["id"], "title": s.get("title"),
            "channel": s.get("channelTitle"),
            "published_at": s.get("publishedAt"),
            "views": _int(st.get("viewCount")),
            "likes": _int(st.get("likeCount")),
            "comments": _int(st.get("commentCount")),
        })
    return {"region": region_code, "count": len(out), "results": out}


# ---- transcripts -----------------------------------------------------------
# YouTube blocks the timedtext endpoint from datacenter IPs (Render / AWS / GCP
# / Azure), so a cloud-hosted server needs a residential/rotating proxy for the
# transcript tools. Data API tools are unaffected (they use the API key).
# Set ONE of:
#   WEBSHARE_PROXY_USERNAME + WEBSHARE_PROXY_PASSWORD   (Webshare "Residential")
#   YT_PROXY_URL = http://user:pass@host:port           (any http/https proxy)
def _proxy_config():
    wu = os.environ.get("WEBSHARE_PROXY_USERNAME", "").strip()
    wp = os.environ.get("WEBSHARE_PROXY_PASSWORD", "").strip()
    generic = os.environ.get("YT_PROXY_URL", "").strip()
    try:
        if wu and wp:
            from youtube_transcript_api.proxies import WebshareProxyConfig
            return WebshareProxyConfig(proxy_username=wu, proxy_password=wp)
        if generic:
            from youtube_transcript_api.proxies import GenericProxyConfig
            return GenericProxyConfig(http_url=generic, https_url=generic)
    except Exception:  # noqa: BLE001  (old lib without .proxies)
        return None
    return None


PROXY_ON = bool(
    os.environ.get("YT_PROXY_URL", "").strip()
    or (os.environ.get("WEBSHARE_PROXY_USERNAME", "").strip()
        and os.environ.get("WEBSHARE_PROXY_PASSWORD", "").strip()))


def _fetch_transcript(video_id: str, languages: list[str]) -> list[dict]:
    from youtube_transcript_api import YouTubeTranscriptApi
    pc = _proxy_config()
    try:  # newer API (>=1.0): instance .fetch()
        api = YouTubeTranscriptApi(proxy_config=pc) if pc else YouTubeTranscriptApi()
        fetched = api.fetch(video_id, languages=languages)
        return [{"text": s.text, "start": round(s.start, 2),
                 "duration": round(s.duration, 2)} for s in fetched]
    except (AttributeError, TypeError):
        pass
    # older API (<=0.6): classmethod .get_transcript(..., proxies=?)
    kw = {"languages": languages}
    gp = os.environ.get("YT_PROXY_URL", "").strip()
    if gp:
        kw["proxies"] = {"http": gp, "https": gp}
    raw = YouTubeTranscriptApi.get_transcript(video_id, **kw)
    return [{"text": r["text"], "start": round(r["start"], 2),
             "duration": round(r.get("duration", 0.0), 2)} for r in raw]


@mcp.tool
def get_transcript(video_id: str, languages: list[str] | None = None) -> dict:
    """Timed transcript segments [{text,start,duration}]. Free — no API key,
    no quota. Pull this on every winner you plan to bend from."""
    langs = languages or ["en", "en-US", "en-GB"]
    try:
        segs = _fetch_transcript(video_id, langs)
    except Exception as e:  # noqa: BLE001
        msg = str(e)
        hint = ""
        if not PROXY_ON and ("block" in msg.lower() or "ip" in msg.lower()
                             or "cloud" in msg.lower() or "RequestBlocked" in msg):
            hint = (" — YouTube blocks transcript scraping from datacenter IPs; "
                    "set YT_PROXY_URL (or WEBSHARE_PROXY_USERNAME/PASSWORD) on "
                    "the host to route through a residential proxy. Data API "
                    "tools are unaffected.")
        return {"video_id": video_id, "error": f"no transcript: {msg}{hint}",
                "proxy_configured": PROXY_ON, "segments": []}
    return {"video_id": video_id, "segment_count": len(segs), "segments": segs}


@mcp.tool
def get_clean_transcript(video_id: str, languages: list[str] | None = None) -> dict:
    """The transcript as one plain-text block (timestamps stripped, whitespace
    collapsed). Best input for 'why does this hold attention'."""
    r = get_transcript(video_id, languages)
    if r.get("error"):
        return r
    text = " ".join(s["text"].replace("\n", " ") for s in r["segments"])
    text = html.unescape(re.sub(r"\s+", " ", text)).strip()
    return {"video_id": video_id, "word_count": len(text.split()), "text": text}


@mcp.tool
def search_transcript(video_id: str, query: str,
                      languages: list[str] | None = None) -> dict:
    """Find every place a phrase is said, with timestamps."""
    r = get_transcript(video_id, languages)
    if r.get("error"):
        return r
    q = query.lower()
    hits = [s for s in r["segments"] if q in s["text"].lower()]
    return {"video_id": video_id, "query": query, "hit_count": len(hits), "hits": hits}


@mcp.tool
def extract_chapters(video_id: str) -> dict:
    """Chapter list parsed from the video description's timestamp lines
    (0:00 Intro / 1:23 ...). Empty if the creator didn't chapter it."""
    meta = get_video_metadata(video_id)["videos"]
    if not meta:
        return {"video_id": video_id, "chapters": []}
    desc = meta[0].get("description", "")
    chapters = []
    for line in desc.splitlines():
        m = TS.search(line)
        if not m:
            continue
        h, mm, ss = m.groups()
        secs = _int(h) * 3600 + _int(mm) * 60 + _int(ss)
        label = line[m.end():].strip(" -–—:\t") or line[:m.start()].strip(" -–—:\t")
        if label:
            chapters.append({"start_s": secs, "label": label[:120]})
    chapters.sort(key=lambda c: c["start_s"])
    return {"video_id": video_id, "chapter_count": len(chapters), "chapters": chapters}


@mcp.tool
def get_most_replayed(video_id: str) -> dict:
    """The 'most replayed' retention heatmap (needs ~50k+ views). Returns the
    top spikes as {start_s, intensity 0-1} — the moments the audience rewound
    to. Scraped from the watch page; not part of the Data API."""
    r = _client.get(f"https://www.youtube.com/watch?v={video_id}",
                    headers={"User-Agent": UA, "Accept-Language": "en-US,en"})
    m = re.search(r'"heatMarkers":\s*(\[.*?\])\s*,\s*"heatMarkersDecorations"', r.text, re.S) \
        or re.search(r'"heatMarkers":\s*(\[.*?\])', r.text, re.S)
    if not m:
        return {"video_id": video_id, "error": "no heatmap (video too small or not exposed)",
                "spikes": []}
    try:
        markers = json.loads(m.group(1))
    except json.JSONDecodeError:
        return {"video_id": video_id, "error": "heatmap parse failed", "spikes": []}
    pts = []
    for mk in markers:
        h = mk.get("heatMarkerRenderer", {})
        pts.append({
            "start_s": round(_int(h.get("timeRangeStartMillis")) / 1000, 1),
            "intensity": round(float(h.get("heatMarkerIntensityScoreNormalized", 0)), 3),
        })
    top = sorted(pts, key=lambda p: p["intensity"], reverse=True)[:8]
    return {"video_id": video_id, "point_count": len(pts),
            "top_spikes": sorted(top, key=lambda p: p["start_s"]), "all_points": pts}


@mcp.tool
def calculate_engagement(video_id: str) -> dict:
    """Engagement ratios for one video: like-rate, comment-rate, and a combined
    engagement score per 1000 views."""
    meta = get_video_metadata(video_id)["videos"]
    if not meta:
        return {"video_id": video_id, "error": "video not found"}
    v = meta[0]
    views = max(v["views"], 1)
    return {
        "video_id": video_id,
        "title": v["title"],
        "views": v["views"],
        "likes": v["likes"],
        "comments": v["comments"],
        "like_rate_pct": round(v["likes"] / views * 100, 3),
        "comment_rate_pct": round(v["comments"] / views * 100, 3),
        "engagement_per_1000_views": round((v["likes"] + v["comments"]) / views * 1000, 2),
    }


# ------------------------------------------------------------------- transport
def _bearer_middleware(token: str):
    """Shared-secret gate, enabled only when MCP_BEARER_TOKEN is set.

    Accepts the secret EITHER as an `Authorization: Bearer <token>` header
    (Claude web custom connector auth field) OR as a `?token=<token>` query
    param on the connector URL. The query-param form lets ChatGPT connect with
    auth set to "No authentication" — its UI has no static-header option."""
    from starlette.middleware import Middleware
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.responses import JSONResponse

    class Guard(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            if "/mcp" in request.url.path:
                q = request.query_params.get("token", "")
                header_ok = request.headers.get("authorization", "") == f"Bearer {token}"
                # tolerate an un-encoded '+' in the URL (a raw '+' in a query
                # string decodes to a space): compare both forms.
                query_ok = token in (q, q.replace(" ", "+"))
                if not (header_ok or query_ok):
                    return JSONResponse({"error": "unauthorized"}, status_code=401)
            return await call_next(request)

    return [Middleware(Guard)]


def main() -> None:
    host = os.environ.get("MCP_HOST", "127.0.0.1")
    # PORT is injected by Render / Railway / Fly / Heroku; MCP_PORT overrides.
    port = int(os.environ.get("MCP_PORT") or os.environ.get("PORT") or "8765")
    token = os.environ.get("MCP_BEARER_TOKEN", "").strip()
    print(f"[youtube-mcp] {len(KEYS)} API key(s) loaded; rotation on quotaExceeded")
    print(f"[youtube-mcp] MCP endpoint: http://{host}:{port}/mcp")
    if token:
        print("[youtube-mcp] bearer-token auth ENABLED")
        import uvicorn
        uvicorn.run(mcp.http_app(middleware=_bearer_middleware(token)),
                    host=host, port=port)
    else:
        mcp.run(transport="http", host=host, port=port)


if __name__ == "__main__":
    main()
