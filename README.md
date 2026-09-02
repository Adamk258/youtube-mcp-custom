# YouTube MCP — for Claude web + ChatGPT web

A remote (streamable-HTTP) MCP server that wraps the **YouTube Data API v3** +
transcripts + the "most replayed" heatmap, with **automatic API-key rotation**.
It gives the Studio's Finder / Script / Packaging prompts the `search_videos`,
`get_channel_videos`, `get_video_metadata`, `get_clean_transcript`,
`get_most_replayed`, `get_video_comments` … tools they ask for.

## Why HTTP and not stdio

Claude **web** and ChatGPT **web** only accept *remote* MCP servers at a public
HTTPS URL. A stdio server works only for desktop apps (Claude Desktop, Cursor,
Claude Code). So this runs as an HTTP server and you expose it with a tunnel.

## 1. Install

Python **3.11 or 3.12** recommended (some deps lag on 3.14).

```bash
cd mcp/youtube-mcp
python -m venv .venv
# Windows:
.venv\Scripts\activate
# macOS/Linux:
# source .venv/bin/activate
pip install -r requirements.txt
```

## 2. Keys

The repo-root `.env` already has `YOUTUBE_API_KEY_2` and `YOUTUBE_API_KEY_3` —
the server auto-loads the first `.env` it finds walking up from this folder, so
you don't have to copy anything. To add more keys, drop `YOUTUBE_API_KEY_4=…`
into that `.env` (each key = 10,000 quota units/day; the server rotates on
`quotaExceeded` and only fails when *all* keys are spent).

Get more keys: Google Cloud Console → new project → enable **YouTube Data API
v3** → Credentials → API key. One key per project.

## 3. Run

```bash
python server.py
# [youtube-mcp] 2 API key(s) loaded; rotation on quotaExceeded
# [youtube-mcp] MCP endpoint: http://127.0.0.1:8765/mcp
```

Smoke-test without any client:

```bash
python -c "import server; print(server.get_clean_transcript('Di2DCj2dO4o')['word_count'])"
python -c "import server; print(server.list_api_keys())"
```

## 4. Expose a public HTTPS URL

**cloudflared** (free, no account):

```bash
# one-time: winget install --id Cloudflare.cloudflared
cloudflared tunnel --url http://127.0.0.1:8765
#  -> https://<random-words>.trycloudflare.com
```

Your MCP URL is that host **+ `/mcp`** →
`https://<random-words>.trycloudflare.com/mcp`

(ngrok works too: `ngrok http 8765`.)

The free tunnel URL changes every restart. For a stable URL, deploy `server.py`
to Render / Railway / Fly (set `MCP_HOST=0.0.0.0`, `MCP_PORT=$PORT`, and the
`YOUTUBE_API_KEY*` vars in the host's dashboard) — one `web` service, start
command `python server.py`.

## 5. Connect

**Claude web** (Pro/Max/Team/Enterprise):
Settings → **Connectors** → *Add custom connector* →
Name `YouTube`, URL `https://<tunnel>/mcp`. If you set `MCP_BEARER_TOKEN`, put
`Bearer <token>` in the auth field.

**ChatGPT** (Plus/Pro/Team/Enterprise):
Settings → **Connectors** (or *Developer mode* → MCP) → add server → same URL.

Then in a chat: *"use the YouTube tools — get the most replayed moments and the
clean transcript for video `<id>`"*.

## 6. Security

Unauthenticated by default. For anything beyond a quick test, set
`MCP_BEARER_TOKEN=<long-random>` in `.env` and pass the same value in the
connector's auth field. Don't post the tunnel URL anywhere public — an open
endpoint spends your YouTube quota.

## Tools

| tool | quota cost | notes |
|---|---|---|
| `search_videos(query, order, max_results, published_after, channel_id)` | 100 units | order = relevance/date/viewCount/rating |
| `get_video_metadata(video_ids)` | 1 unit / 50 vids | views, likes, comments, duration, tags, full description, thumbnail |
| `get_channel_info(channel_id \| handle)` | 1–2 units | subs, total views, video count, uploads playlist |
| `get_channel_videos(channel_id \| handle, order, max_results)` | ~1 unit / 50 | up to 200 uploads + stats + `view_multiple_vs_channel_median` |
| `get_video_comments(video_id, max_results, order)` | 1 unit | sorted by likes |
| `get_trending_videos(region_code, category_id, max_results)` | 1 unit | mostPopular chart |
| `get_transcript(video_id, languages)` | **0** | timed segments |
| `get_clean_transcript(video_id, languages)` | **0** | one plain-text block |
| `search_transcript(video_id, query)` | **0** | timestamped phrase hits |
| `extract_chapters(video_id)` | 1 unit | parsed from description timestamps |
| `get_most_replayed(video_id)` | **0** (scrape) | top retention spikes; needs ~50k+ views |
| `calculate_engagement(video_id)` | 1 unit | like-rate, comment-rate, per-1000 score |
| `list_api_keys()` | 0 | confirms rotation is wired (values masked) |
