# Deploy — keeping this MCP running

The server needs a **public HTTPS URL ending in `/mcp`** so Claude web / ChatGPT
web can add it as a custom connector. `server.py` exposes a module-level
`mcp` object and reads `MCP_HOST` + (`MCP_PORT` \|\| `PORT`) from the env.

Pick one:

---

## A. Render — free, stable URL, no PC needed  ← recommended

Free web service. Sleeps after ~15 min idle (≈50 s cold start on the next
call) — fine for bursty research use.

1. Push this repo to GitHub (`git remote add origin … && git push -u origin main`).
2. Render → **New → Blueprint** → pick the repo. It reads `render.yaml`
   (build `pip install -r requirements.txt`, start `python server.py`).
   *(No blueprint? New → Web Service → same two commands, runtime Python.)*
3. In the service's **Environment** tab set:
   - `YOUTUBE_API_KEY_2` = your key
   - `YOUTUBE_API_KEY_3` = your key
   - `MCP_HOST` = `0.0.0.0`
   - `MCP_BEARER_TOKEN` = (Render auto-generates one via the blueprint — copy its value)
   `PORT` is injected automatically; leave it unset.
4. Deploy → your URL is `https://youtube-mcp-xxxx.onrender.com/mcp`.
5. Claude web → Settings → Connectors → **Add custom connector** → that URL.
   Auth field: `Bearer <the MCP_BEARER_TOKEN value>`.
6. Test in a chat: *"use the YouTube tools — call `list_api_keys`, then
   `get_clean_transcript` for `Di2DCj2dO4o`."* `list_api_keys` should report
   `keys_loaded: 2`.

To kill the cold start later: Render Starter plan ($7/mo, always-on), or move
to Railway/Fly.

---

## B. Cloudflare Tunnel — free, but your PC must stay on

Cloudflare **Workers** can't run this (Python won't run on V8). A **Tunnel**
just exposes your local `python server.py`.

**Quick (testing, URL changes each run):**
```bash
python server.py                                   # terminal 1
cloudflared tunnel --url http://127.0.0.1:8765     # terminal 2  -> prints https://<random>.trycloudflare.com
```
MCP URL = that host + `/mcp`.

**Persistent (stable URL, survives reboot) — needs a free Cloudflare account + a domain in it:**
```bash
cloudflared tunnel login
cloudflared tunnel create youtube-mcp
cloudflared tunnel route dns youtube-mcp youtube-mcp.<yourdomain>
# config.yml:  tunnel: youtube-mcp
#              ingress: [{hostname: youtube-mcp.<yourdomain>, service: http://127.0.0.1:8765}, {service: http_status:404}]
cloudflared service install         # runs as a Windows service on boot
```
Then run `python server.py` on boot too (Task Scheduler → "At log on" →
`pythonw.exe server.py`). MCP URL = `https://youtube-mcp.<yourdomain>/mcp`.

---

## C. Docker host (Fly.io / Railway / Hugging Face Spaces)

`Dockerfile` is included. Fly: `fly launch` → set the `YOUTUBE_API_KEY_*` +
`MCP_HOST=0.0.0.0` secrets → `fly deploy`. Railway: New Project → deploy from
repo → same env vars (always-on, ~$5/mo after trial credit).

---

## Which to use

- **Free + set-and-forget:** Render (A). Live with the cold start.
- **Must be free + always-on + fine with your PC running:** Cloudflare persistent tunnel (B).
- **A few $/mo for zero hassle, always-on:** Railway or Fly (C).
