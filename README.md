# VENS yt-dlp micro-service

Self-hosted extractor used by the VENS-DOWNLOADER site. It wraps
[`yt-dlp`](https://github.com/yt-dlp/yt-dlp) in a tiny FastAPI HTTP service so
your Lovable Cloud backend (which runs on a Cloudflare Worker and can't run
Python) can call it to resolve TikTok / YouTube / Facebook / Instagram / X /
Pinterest / Snapchat / etc. URLs into a direct media link.

## Endpoints

- `GET  /health` → `{"ok": true}`
- `POST /extract` — body `{"url":"…","quality":"720p|1080p|4k|hd|sd"}`
  → `{"ok":true,"url":"…direct.mp4","title":"…","thumbnail":"…"}`
- `GET  /proxy?u=<encoded-url>` — streams the remote file through this server
  (used as a fallback when the source blocks hotlinking).

All endpoints accept an `X-API-Key` header. Set the `API_KEY` env var to
require it.

## Local test

```bash
docker compose up --build
curl -X POST http://localhost:8080/extract \
  -H "Content-Type: application/json" \
  -H "X-API-Key: change-me-to-a-long-random-string" \
  -d '{"url":"https://www.tiktok.com/@scout2015/video/6718335390845095173","quality":"720p"}'
```

## Deploy — Fly.io (recommended, free tier fits)

```bash
# one-time
brew install flyctl        # or: curl -L https://fly.io/install.sh | sh
fly auth signup            # or: fly auth login

cd ytdlp-service
fly launch --no-deploy     # keep the included fly.toml (edit app name if taken)
fly secrets set API_KEY="$(openssl rand -hex 32)"
fly deploy
fly status                 # note the https://<app>.fly.dev URL
```

Then, in Lovable, save two secrets (chat: “add secret”):

- `YTDLP_URL`  = `https://<your-app>.fly.dev`
- `YTDLP_API_KEY` = the same value you passed to `fly secrets set API_KEY`

That's it — the site will start using yt-dlp automatically. If the service is
down or the secrets are missing, the site falls back to the public APIs it
already uses.

## Deploy — Railway / Render / any VPS

Any Docker host works. Expose port `8080`, set `API_KEY`, and put the public
URL + key into the two Lovable secrets above.

## Notes

- `yt-dlp` supports 1000+ sites, so this covers every platform in the app.
- Update yt-dlp regularly (sites change): re-run `fly deploy` — the Dockerfile
  reinstalls the latest release each build. You can add a weekly redeploy
  cron in Fly or a GitHub Action.
- If YouTube starts requiring cookies for a video, mount a `cookies.txt` at
  `/app/cookies.txt` and add `"cookiefile": "/app/cookies.txt"` to
  `ydl_opts` in `server.py`.
