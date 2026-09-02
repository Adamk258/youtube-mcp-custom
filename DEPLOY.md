# Deploy to mcphosting.io

mcphosting.io connects a **GitHub repo**, detects Python FastMCP, and gives you a
remote HTTPS MCP URL that Claude web / ChatGPT can use as a custom connector.
This folder is already a self-contained repo (`server.py` exposes a module-level
`mcp = FastMCP(...)` object, which is the entrypoint host runners look for).

## 1. Put this folder on GitHub (one time)

It was `git init`-ed locally as its own repo (separate from the big vault repo).
Create an empty repo on github.com named e.g. `youtube-mcp`, then:

```bash
cd "mcp/youtube-mcp"
git remote add origin https://github.com/<your-user>/youtube-mcp.git
git branch -M main
git push -u origin main
```

`.env` is git-ignored — only `.env.example` ships. Keys go in the host, step 3.

## 2. Connect on mcphosting.io

- Sign in with GitHub, pick the `youtube-mcp` repo.
- Language/framework: **Python / FastMCP** (auto-detected).
- Entrypoint: `server.py` (object `mcp`). If it asks for a start command:
  `python server.py` or `fastmcp run server.py:mcp`.
- It builds from `requirements.txt`.

## 3. Set environment variables (secrets) in the mcphosting dashboard

| var | value |
|---|---|
| `YOUTUBE_API_KEY_2` | your key (from the vault `.env`) |
| `YOUTUBE_API_KEY_3` | your key |
| `YOUTUBE_API_KEY_4`… | any more keys you add (server rotates through all) |
| `MCP_HOST` | `0.0.0.0` |
| `MCP_BEARER_TOKEN` | *(optional)* a long random string, if you want a shared-secret gate on top of the host's OAuth |

`PORT` is injected by the platform — `server.py` reads `MCP_PORT` \|\| `PORT` \|\| 8765, so leave it unset.

## 4. Connect the URL

mcphosting gives you `https://<something>.mcphosting.io/mcp` (or similar).

- **Claude web:** Settings → Connectors → Add custom connector → that URL. If mcphosting put OAuth in front, Claude walks you through it; if you set `MCP_BEARER_TOKEN` instead, put `Bearer <token>` in the auth field.
- **ChatGPT** (Plus/Pro/Team/Enterprise): Settings → Connectors → add MCP server → same URL.

## 5. Verify

In a chat: *"use the YouTube tools — call `list_api_keys`, then `get_clean_transcript` for video `Di2DCj2dO4o`."*
`list_api_keys` should report `keys_loaded: 2+` and a masked list — that confirms rotation is wired.

## Fallback

If mcphosting's free tier or detection doesn't work out, the same repo deploys to
Render (`render.yaml` included), Fly.io / Hugging Face Spaces (`Dockerfile`
included), or run locally + `cloudflared tunnel` (see `README.md`).
