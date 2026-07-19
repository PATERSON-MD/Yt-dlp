"""
VENS-DOWNLOADER — yt-dlp extraction server.
Deploy to Render / Fly / Railway. Set env var API_KEY to any random 32+ char string.
Endpoints:
  GET  /            -> health check
  POST /extract     -> { url, quality } -> video info + direct download_url
Header required on /extract:  X-API-Key: <your API_KEY>
"""
import os
from flask import Flask, request, jsonify
from flask_cors import CORS
import yt_dlp

app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}})

API_KEY = os.environ.get("API_KEY", "").strip()


def pick_format(quality: str) -> str:
    q = (quality or "best").lower()
    if q == "audio":
        return "bestaudio/best"
    if q in ("2160", "4k"):
        return "bestvideo[height<=2160]+bestaudio/best[height<=2160]/best"
    if q == "1080":
        return "bestvideo[height<=1080]+bestaudio/best[height<=1080]/best"
    if q == "720":
        return "bestvideo[height<=720]+bestaudio/best[height<=720]/best"
    if q == "480":
        return "bestvideo[height<=480]+bestaudio/best[height<=480]/best"
    return "best"


@app.get("/")
def health():
    return jsonify({"status": "ok", "service": "vens-ytdlp", "auth_required": bool(API_KEY)})


@app.post("/extract")
def extract():
    if API_KEY:
        supplied = request.headers.get("X-API-Key", "").strip()
        if supplied != API_KEY:
            return jsonify({"detail": "Invalid or missing API key"}), 401

    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    quality = (data.get("quality") or "best").strip()
    if not url:
        return jsonify({"detail": "Missing 'url'"}), 400

    ydl_opts = {
        "format": pick_format(quality),
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        "extract_flat": False,
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as e:
        return jsonify({"detail": f"Extraction failed: {e}"}), 502

    if not info:
        return jsonify({"detail": "No info returned"}), 502

    # Resolve a direct URL usable in a browser <a download>
    download_url = info.get("url")
    if not download_url:
        formats = info.get("formats") or []
        # prefer progressive (video+audio) matching height cap
        for f in reversed(formats):
            if f.get("url") and f.get("acodec") != "none" and f.get("vcodec") != "none":
                download_url = f["url"]
                break
        if not download_url and formats:
            download_url = formats[-1].get("url")

    return jsonify({
        "title": info.get("title"),
        "thumbnail": info.get("thumbnail"),
        "duration": info.get("duration"),
        "download_url": download_url,
        "ext": info.get("ext"),
        "height": info.get("height"),
        "width": info.get("width"),
        "extractor": info.get("extractor"),
    })


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
