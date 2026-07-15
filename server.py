"""
VENS-DOWNLOADER — yt-dlp micro-service.

Endpoints:
  GET  /health                 -> {"ok": true}
  POST /extract                -> resolve best direct media URL (JSON)
       body: {"url": "...", "quality": "720p|1080p|4k|hd|..."}
  GET  /proxy?u=<encoded-url>  -> streams the remote media through this server
                                  (useful when the source blocks hotlinking)

Auth: send header  X-API-Key: <API_KEY>  (set API_KEY env var to enable).

Deploy anywhere that runs Docker: Fly.io, Railway, Render, a VPS, etc.
"""
from __future__ import annotations

import os
import re
from typing import Optional

import httpx
from fastapi import FastAPI, HTTPException, Header, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, JSONResponse
from pydantic import BaseModel, Field
from yt_dlp import YoutubeDL

API_KEY = os.environ.get("API_KEY", "").strip()
ALLOW_ORIGINS = [o.strip() for o in os.environ.get("ALLOW_ORIGINS", "*").split(",") if o.strip()]

app = FastAPI(title="VENS yt-dlp service", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOW_ORIGINS or ["*"],
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)


def check_auth(x_api_key: Optional[str]) -> None:
    if API_KEY and x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")


class ExtractIn(BaseModel):
    url: str = Field(..., max_length=2048)
    quality: Optional[str] = Field(None, max_length=40)


def quality_to_format(q: Optional[str]) -> str:
    q = (q or "").lower()
    if "4k" in q or "max" in q:
        h = 2160
    elif "1080" in q:
        h = 1080
    elif "480" in q or "sd" in q:
        h = 480
    else:
        h = 720  # default
    # progressive mp4 first (single file), then bestvideo+bestaudio merge fallback.
    return (
        f"best[ext=mp4][height<={h}]/"
        f"best[height<={h}]/"
        f"bestvideo[height<={h}]+bestaudio/best"
    )


@app.get("/health")
def health():
    return {"ok": True}


@app.post("/extract")
def extract(payload: ExtractIn, x_api_key: Optional[str] = Header(None)):
    check_auth(x_api_key)

    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        "format": quality_to_format(payload.quality),
        "extractor_args": {"youtube": {"player_client": ["android", "web"]}},
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
            )
        },
    }

    try:
        with YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(payload.url, download=False)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=422, detail=f"extract_failed: {e}") from e

    if info is None:
        raise HTTPException(status_code=422, detail="no_info")

    # yt-dlp returns "entries" for playlists; take the first item.
    if "entries" in info and info["entries"]:
        info = info["entries"][0]

    direct = info.get("url")
    # If yt-dlp returned a merged/HLS format without a single URL, pick the best
    # progressive format from the list.
    if not direct:
        fmts = info.get("formats") or []
        progressive = [
            f for f in fmts
            if f.get("url") and f.get("vcodec") not in (None, "none")
            and f.get("acodec") not in (None, "none")
            and (f.get("ext") == "mp4" or "mp4" in (f.get("protocol") or ""))
        ]
        progressive.sort(key=lambda f: (f.get("height") or 0), reverse=True)
        if progressive:
            direct = progressive[0]["url"]

    if not direct:
        raise HTTPException(status_code=422, detail="no_direct_url")

    return JSONResponse({
        "ok": True,
        "url": direct,
        "title": info.get("title"),
        "thumbnail": info.get("thumbnail"),
        "duration": info.get("duration"),
        "ext": info.get("ext"),
        "height": info.get("height"),
        "extractor": info.get("extractor_key"),
    })


_HOST_RE = re.compile(r"^https?://[^/]+", re.IGNORECASE)


@app.get("/proxy")
async def proxy(
    u: str = Query(..., max_length=4096),
    k: Optional[str] = Query(None, max_length=200),
    x_api_key: Optional[str] = Header(None),
):
    """Stream a remote media file through this server (bypasses hotlink blocks)."""
    # Accept the key via header (server-to-server) or ?k= query (browser download).
    check_auth(x_api_key or k)
    if not _HOST_RE.match(u):
        raise HTTPException(status_code=400, detail="bad_url")

    upstream_headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        ),
        "Accept": "*/*",
    }

    client = httpx.AsyncClient(timeout=None, follow_redirects=True)
    try:
        req = client.build_request("GET", u, headers=upstream_headers)
        resp = await client.send(req, stream=True)
    except Exception as e:  # noqa: BLE001
        await client.aclose()
        raise HTTPException(status_code=502, detail=f"upstream_error: {e}") from e

    if resp.status_code >= 400:
        status = resp.status_code
        await resp.aclose()
        await client.aclose()
        raise HTTPException(status_code=502, detail=f"upstream_status_{status}")

    headers = {
        "Content-Type": resp.headers.get("content-type", "application/octet-stream"),
        "Content-Disposition": 'attachment; filename="vens-download.mp4"',
    }
    if "content-length" in resp.headers:
        headers["Content-Length"] = resp.headers["content-length"]

    async def iterator():
        try:
            async for chunk in resp.aiter_bytes():
                yield chunk
        finally:
            await resp.aclose()
            await client.aclose()

    return StreamingResponse(iterator(), status_code=200, headers=headers)
