"""
VENS-DOWNLOADER — Production Server v10.1
Optimized for Render + Flask + yt-dlp
"""

import os
import re
import time
import uuid
import threading
from pathlib import Path
from urllib.parse import urlparse

from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
import yt_dlp


app = Flask(__name__)

CORS(
    app,
    resources={
        r"/*": {
            "origins": "*",
            "methods": ["GET", "POST", "OPTIONS"],
            "allow_headers": ["Content-Type", "X-API-Key"],
            "expose_headers": ["Content-Disposition"]
        }
    }
)

API_KEY = os.environ.get("API_KEY", "").strip()
DOWNLOAD_DIR = Path(os.environ.get("DOWNLOAD_DIR", "/tmp/vens_downloads"))
MAX_FILE_SIZE = int(os.environ.get("MAX_FILE_SIZE", "500")) * 1024 * 1024
FILE_EXPIRE_TIME = int(os.environ.get("FILE_EXPIRE_TIME", "300"))

DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

# =========================
# SECURITY
# =========================

def check_api_key():
    if not API_KEY:
        return True
    supplied = request.headers.get("X-API-Key", "").strip()
    return supplied == API_KEY

def valid_url(url):
    try:
        parsed = urlparse(url)
        return parsed.scheme in ("http", "https") and parsed.netloc
    except Exception:
        return False

def safe_filename(name):
    return re.sub(r"[^a-zA-Z0-9._-]", "_", name)

# =========================
# FORMAT
# =========================

def get_format(quality):
    q = (quality or "720").lower()
    
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
    
    return "bestvideo[height<=720]+bestaudio/best[height<=720]/best"

# =========================
# PLATFORM DETECTION
# =========================

def detect_platform(url):
    platforms = {
        "youtube.com": "YouTube",
        "youtu.be": "YouTube",
        "tiktok.com": "TikTok",
        "vt.tiktok.com": "TikTok",
        "instagram.com": "Instagram",
        "facebook.com": "Facebook",
        "fb.watch": "Facebook",
        "twitter.com": "X",
        "x.com": "X",
        "pinterest.com": "Pinterest",
        "snapchat.com": "Snapchat"
    }
    
    for domain, name in platforms.items():
        if domain in url:
            return name
    return "Unknown"

# =========================
# CLEANUP SYSTEM
# =========================

def cleanup_worker():
    while True:
        try:
            now = time.time()
            for file in DOWNLOAD_DIR.iterdir():
                if not file.is_file():
                    continue
                age = now - file.stat().st_mtime
                if age > FILE_EXPIRE_TIME:
                    file.unlink(missing_ok=True)
        except Exception:
            pass
        time.sleep(60)

cleanup_thread = threading.Thread(target=cleanup_worker, daemon=True)
cleanup_thread.start()

# =========================
# HEALTH CHECK
# =========================

@app.get("/")
def health():
    return jsonify({
        "status": "ok",
        "service": "vens-ytdlp",
        "version": "10.1",
        "mode": "download-server",
        "auth_required": bool(API_KEY),
        "download_dir": str(DOWNLOAD_DIR)
    })

# =========================
# DOWNLOAD ENGINE
# =========================

def download_media(url, quality):
    file_id = str(uuid.uuid4())
    output_template = str(DOWNLOAD_DIR / f"{file_id}.%(ext)s")
    
    ydl_opts = {
        "format": get_format(quality),
        "outtmpl": output_template,
        "quiet": True,
        "no_warnings": False,
        "noplaylist": True,
        "nocheckcertificate": True,
        "ignoreerrors": True,
        "retries": 10,
        "fragment_retries": 10,
        "merge_output_format": "mp4",
        "extract_flat": False,
        "geo_bypass": True,
        "geo_bypass_country": "FR",
        "sleep_interval": 1,
        "max_sleep_interval": 3,
        "concurrent_fragment_downloads": 5,
        "http_headers": {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            "Accept-Language": "fr-FR,fr;q=0.9,en-US;q=0.8,en;q=0.7",
            "Accept-Encoding": "gzip, deflate, br",
            "DNT": "1",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1"
        },
        "extractor_args": {
            "tiktok": {
                "use_api": ["1"],
                "app_version": ["24.6.0"],
                "device_id": [str(uuid.uuid4())],
                "user_agent": ["Mozilla/5.0"]
            },
            "youtube": {
                "skip": ["dash", "hls"],
                "player_client": ["android"]
            }
        }
    }
    
    # Audio seulement
    if quality.lower() == "audio":
        ydl_opts["format"] = "bestaudio/best"
        ydl_opts["postprocessors"] = [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192"
            }
        ]
    
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
    
    # Trouver le fichier téléchargé
    files = list(DOWNLOAD_DIR.glob(f"{file_id}.*"))
    
    if not files:
        raise Exception("Fichier téléchargé introuvable")
    
    file_path = files[0]
    
    if file_path.stat().st_size > MAX_FILE_SIZE:
        file_path.unlink(missing_ok=True)
        raise Exception("Fichier trop volumineux")
    
    return file_path, info

# =========================
# EXTRACT ENDPOINT
# =========================

@app.post("/extract")
def extract():
    if not check_api_key():
        return jsonify({"error": "Invalid API key"}), 401
    
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    quality = (data.get("quality") or "720").strip()
    
    if not valid_url(url):
        return jsonify({"error": "URL invalide"}), 400
    
    try:
        file_path, info = download_media(url, quality)
        
        return jsonify({
            "success": True,
            "title": info.get("title"),
            "thumbnail": info.get("thumbnail"),
            "duration": info.get("duration"),
            "platform": detect_platform(url),
            "filename": file_path.name,
            "filesize": file_path.stat().st_size,
            "download_url": f"/download/{file_path.name}"
        })
    
    except Exception as e:
        return jsonify({"error": str(e)}), 502

# =========================
# DOWNLOAD ROUTE
# =========================

@app.get("/download/<filename>")
def download(filename):
    if not check_api_key():
        return jsonify({"error": "Invalid API key"}), 401
    
    filename = safe_filename(filename)
    file_path = DOWNLOAD_DIR / filename
    
    if not file_path.exists():
        return jsonify({"error": "Fichier expiré ou introuvable"}), 404
    
    return send_file(
        file_path,
        as_attachment=True,
        download_name=filename,
        mimetype="video/mp4"
    )

# =========================
# INFO ENDPOINT
# =========================

@app.post("/info")
def get_info():
    if not check_api_key():
        return jsonify({"error": "Invalid API key"}), 401
    
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    
    if not valid_url(url):
        return jsonify({"error": "URL invalide"}), 400
    
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        "extract_flat": False,
        "nocheckcertificate": True,
        "http_headers": {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        }
    }
    
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
        
        if not info:
            return jsonify({"error": "Aucune information"}), 404
        
        formats = info.get("formats", [])
        available_qualities = set()
        for f in formats:
            height = f.get("height")
            if height:
                available_qualities.add(f"{height}p")
        
        return jsonify({
            "title": info.get("title"),
            "thumbnail": info.get("thumbnail"),
            "duration": info.get("duration"),
            "platform": detect_platform(url),
            "available_qualities": sorted(list(available_qualities)),
            "ext": info.get("ext", "mp4")
        })
    
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# =========================
# RUN SERVER
# =========================

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    debug = os.environ.get("DEBUG", "False").lower() == "true"
    app.run(host="0.0.0.0", port=port, debug=debug)
