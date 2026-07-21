"""
VENS-DOWNLOADER — Production Server v12.6
✅ FIX: Logging configuré AVANT utilisation
✅ FIX: BASE_URL nettoyée (rstrip)
✅ FIX: Utilisation de request.host_url (auto-détection)
✅ URL absolue dans /extract
✅ 404 strict (pas de fallback dangereux)
"""

import os
import re
import time
import uuid
import shutil
import threading
import logging
import traceback
import requests
from pathlib import Path
from urllib.parse import urlparse

from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
import yt_dlp


# =========================
# CONFIGURATION - LOGGING D'ABORD
# =========================

# Variables d'environnement (avant logging)
API_KEY = os.environ.get("API_KEY", "").strip()
DOWNLOAD_DIR = Path(os.environ.get("DOWNLOAD_DIR", "/tmp/vens_downloads"))
MAX_FILE_SIZE = int(os.environ.get("MAX_FILE_SIZE", "500")) * 1024 * 1024
FILE_EXPIRE_TIME = int(os.environ.get("FILE_EXPIRE_TIME", "600"))
MAX_CONCURRENT_DOWNLOADS = int(os.environ.get("MAX_CONCURRENT_DOWNLOADS", "2"))
DELETE_AFTER_DOWNLOAD = os.environ.get("DELETE_AFTER_DOWNLOAD", "false").lower() == "true"
CLEANUP_DELAY = int(os.environ.get("CLEANUP_DELAY", "300"))
DEBUG = os.environ.get("DEBUG", "True").lower() == "true"

# ⭐ LOGGING CONFIGURÉ EN PREMIER
logging.basicConfig(
    level=logging.DEBUG if DEBUG else logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Créer le dossier
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

# ⭐ BASE_URL - Nettoyée automatiquement
BASE_URL = os.environ.get("BASE_URL", "").strip()
if BASE_URL:
    BASE_URL = BASE_URL.rstrip('/')
else:
    # Auto-détection pour Render/Fly/Railway
    render_url = os.environ.get("RENDER_EXTERNAL_HOSTNAME", "")
    if render_url:
        BASE_URL = f"https://{render_url}"
    else:
        BASE_URL = "http://localhost:8000"

logger.info(f"📦 yt-dlp version: {yt_dlp.version.__version__}")
logger.info(f"🔧 Mode DEBUG: {DEBUG}")
logger.info(f"🌐 BASE_URL: {BASE_URL}")

# =========================
# FLASK APP
# =========================

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

# Compteur de téléchargements actifs
active_downloads = 0
download_lock = threading.Lock()

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"

# =========================
# VÉRIFICATION FFmpeg
# =========================

def check_ffmpeg():
    ffmpeg_path = shutil.which("ffmpeg")
    if ffmpeg_path:
        logger.info(f"✅ FFmpeg trouvé: {ffmpeg_path}")
        return True
    else:
        logger.error("❌ FFmpeg NON trouvé")
        return False

FFMPEG_AVAILABLE = check_ffmpeg()

# =========================
# SÉCURITÉ
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

def safe_filename(filename):
    return re.sub(r"[^a-zA-Z0-9._-]", "_", filename)

def is_within_download_dir(filepath):
    try:
        resolved_path = filepath.resolve()
        return DOWNLOAD_DIR.resolve() in resolved_path.parents or resolved_path.parent == DOWNLOAD_DIR.resolve()
    except:
        return False

# =========================
# DÉTECTION PLATEFORME
# =========================

def detect_platform(url):
    platforms = {
        "youtube.com": "YouTube",
        "youtu.be": "YouTube",
        "dailymotion.com": "Dailymotion",
        "dai.ly": "Dailymotion",
        "vimeo.com": "Vimeo",
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
# FORMAT
# =========================

def get_formats_with_fallback(quality):
    q = (quality or "720").lower()
    
    if q == "audio":
        return ["bestaudio/best"]
    
    return [
        "best[height<=720]/best",
        "best",
        "bestvideo[height<=720]+bestaudio/best"
    ]

# =========================
# NETTOYAGE AUTOMATIQUE
# =========================

def cleanup_worker():
    while True:
        try:
            now = time.time()
            count = 0
            for file in DOWNLOAD_DIR.iterdir():
                if not file.is_file():
                    continue
                age = now - file.stat().st_mtime
                if age > FILE_EXPIRE_TIME:
                    file.unlink(missing_ok=True)
                    count += 1
            if count > 0:
                logger.info(f"🧹 Nettoyage: {count} fichiers supprimés")
        except Exception as e:
            logger.error(f"Erreur nettoyage: {e}")
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
        "version": "12.6",
        "ytdlp_version": yt_dlp.version.__version__,
        "ffmpeg": FFMPEG_AVAILABLE,
        "debug": DEBUG,
        "base_url": BASE_URL,
        "delete_after_download": DELETE_AFTER_DOWNLOAD,
        "supported_platforms": [
            "YouTube", "Dailymotion", "Vimeo",
            "Facebook", "Instagram", "X (Twitter)",
            "Pinterest", "Snapchat"
        ],
        "active_downloads": active_downloads,
        "max_concurrent": MAX_CONCURRENT_DOWNLOADS,
        "storage_free_mb": shutil.disk_usage(DOWNLOAD_DIR).free // (1024*1024)
    })

# =========================
# MOTEUR DE TÉLÉCHARGEMENT
# =========================

def download_media(url, quality):
    """Télécharger un média"""
    global active_downloads
    
    with download_lock:
        if active_downloads >= MAX_CONCURRENT_DOWNLOADS:
            raise Exception("Serveur occupé, réessayez dans quelques instants")
        active_downloads += 1
    
    try:
        platform = detect_platform(url)
        logger.info(f"📥 [{platform}] Téléchargement: {url[:60]}... (qualité: {quality})")
        
        file_uuid = str(uuid.uuid4())[:8]
        formats_to_try = get_formats_with_fallback(quality)
        last_error = None
        
        for fmt in formats_to_try:
            try:
                logger.info(f"🔄 Essai format yt-dlp: {fmt}")
                
                output_template = str(DOWNLOAD_DIR / f"{file_uuid}.%(ext)s")
                
                ydl_opts = {
                    "format": fmt,
                    "outtmpl": output_template,
                    "quiet": False,
                    "no_warnings": False,
                    "noplaylist": True,
                    "nocheckcertificate": True,
                    "ignoreerrors": False,
                    "retries": 10,
                    "fragment_retries": 10,
                    "merge_output_format": "mp4" if not quality.lower() == "audio" else None,
                    "extract_flat": False,
                    "geo_bypass": True,
                    "geo_bypass_country": "FR",
                    "sleep_interval": 1,
                    "max_sleep_interval": 3,
                    "socket_timeout": 30,
                    "http_headers": {
                        "User-Agent": USER_AGENT,
                        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
                        "Accept-Language": "fr-FR,fr;q=0.9,en-US;q=0.8,en;q=0.7",
                        "Accept-Encoding": "gzip, deflate",
                        "DNT": "1",
                        "Connection": "keep-alive"
                    },
                    "extractor_args": {
                        "youtube": {"player_client": ["android"]},
                        "dailymotion": {"player_client": ["desktop"]},
                        "pinterest": {"use_api": ["1"]}
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
                    output_template = str(DOWNLOAD_DIR / f"{file_uuid}.mp3")
                    ydl_opts["outtmpl"] = output_template
                
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(url, download=True)
                
                if not info:
                    raise Exception("Aucune information récupérée")
                
                # Trouver le fichier
                final_file = None
                files = list(DOWNLOAD_DIR.glob(f"{file_uuid}.*"))
                if files:
                    final_file = files[0]
                
                if not final_file:
                    current_time = time.time()
                    for file in DOWNLOAD_DIR.iterdir():
                        if file.is_file() and (current_time - file.stat().st_mtime) < 10:
                            if file.name.startswith(file_uuid) or not final_file:
                                final_file = file
                                if file.name.startswith(file_uuid):
                                    break
                
                if not final_file or not final_file.exists():
                    raise Exception("Fichier introuvable après téléchargement")
                
                file_size = final_file.stat().st_size
                if file_size == 0:
                    final_file.unlink()
                    raise Exception("Fichier vide")
                
                if file_size > MAX_FILE_SIZE:
                    final_file.unlink()
                    raise Exception(f"Fichier trop volumineux ({file_size} > {MAX_FILE_SIZE})")
                
                ext = final_file.suffix
                clean_name = f"vens_{file_uuid}{ext}"
                clean_path = DOWNLOAD_DIR / clean_name
                
                if final_file != clean_path:
                    final_file.rename(clean_path)
                    final_file = clean_path
                
                logger.info(f"✅ [{platform}] Téléchargement réussi: {clean_name}")
                return final_file, info
                
            except Exception as e:
                last_error = str(e)
                logger.warning(f"⚠️ Format {fmt} a échoué: {e}")
                for f in DOWNLOAD_DIR.glob(f"{file_uuid}.*"):
                    f.unlink(missing_ok=True)
                continue
        
        raise Exception(f"Tous les formats ont échoué. Dernière erreur: {last_error}")
        
    finally:
        with download_lock:
            active_downloads -= 1

# =========================
# EXTRACT ENDPOINT - URL ABSOLUE AUTO
# =========================

@app.post("/extract")
def extract():
    if not check_api_key():
        return jsonify({"error": "Invalid API key"}), 401
    
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    quality = (data.get("quality") or "720").strip()
    
    logger.info(f"📥 Requête reçue - URL: {url[:60]}... Qualité: {quality}")
    
    if not valid_url(url):
        logger.error(f"❌ URL invalide: {url}")
        return jsonify({"error": "URL invalide"}), 400
    
    try:
        file_path, info = download_media(url, quality)
        
        # ⭐ URL ABSOLUE - Auto-détection du domaine
        base_url = request.host_url.rstrip('/')
        download_url = f"{base_url}/download/{file_path.name}"
        preview_url = f"{base_url}/preview/{file_path.name}"
        
        response = {
            "success": True,
            "title": info.get("title", "VENS-DOWNLOADER"),
            "thumbnail": info.get("thumbnail", ""),
            "duration": info.get("duration", 0),
            "platform": detect_platform(url),
            "filename": file_path.name,
            "filesize": file_path.stat().st_size,
            "media_type": "audio" if quality.lower() == "audio" else "video",
            "download_url": download_url,
            "preview_url": preview_url,
            "ext": file_path.suffix[1:],
            "quality": quality,
            "expires_in": FILE_EXPIRE_TIME
        }
        
        logger.info(f"✅ Succès: {file_path.name} ({file_path.stat().st_size} octets)")
        logger.info(f"🔗 Download: {download_url}")
        return jsonify(response)
    
    except Exception as e:
        logger.error(f"❌ Erreur détaillée: {e}")
        logger.error(traceback.format_exc())
        
        try:
            for f in DOWNLOAD_DIR.iterdir():
                if f.is_file() and (time.time() - f.stat().st_mtime) < 5:
                    f.unlink(missing_ok=True)
        except:
            pass
        
        return jsonify({
            "error": str(e),
            "detail": "Erreur lors du téléchargement. Vérifiez l'URL ou réessayez."
        }), 502

# =========================
# PREVIEW ROUTE
# =========================

@app.get("/preview/<filename>")
def preview(filename):
    if not check_api_key():
        return jsonify({"error": "Invalid API key"}), 401
    
    filename = safe_filename(filename)
    file_path = DOWNLOAD_DIR / filename
    
    if not file_path.exists():
        return jsonify({"error": "Fichier introuvable"}), 404
    
    return send_file(
        file_path,
        mimetype="video/mp4",
        conditional=True
    )

# =========================
# DOWNLOAD ROUTE - STRICT
# =========================

@app.get("/download/<filename>")
def download(filename):
    if not check_api_key():
        return jsonify({"error": "Invalid API key"}), 401
    
    filename = safe_filename(filename)
    file_path = DOWNLOAD_DIR / filename
    
    logger.info(f"🔍 Téléchargement demandé: {filename}")
    
    # ⭐ VÉRIFICATION STRICTE - Pas de fallback
    if not file_path.exists():
        logger.warning(f"❌ Fichier non trouvé: {filename}")
        if DEBUG:
            all_files = list(DOWNLOAD_DIR.iterdir())
            logger.info(f"📂 Fichiers disponibles: {[f.name for f in all_files]}")
            return jsonify({
                "error": "Fichier introuvable",
                "requested": filename,
                "available_files": [f.name for f in all_files]
            }), 404
        else:
            return jsonify({"error": "Fichier introuvable"}), 404
    
    # Vérifier la sécurité
    if not is_within_download_dir(file_path):
        logger.warning(f"⚠️ Tentative de sortie du dossier: {filename}")
        return jsonify({"error": "Accès interdit"}), 403
    
    # Déterminer le type MIME
    ext = file_path.suffix.lower()
    mime_types = {
        ".mp4": "video/mp4",
        ".mp3": "audio/mpeg",
        ".webm": "video/webm",
        ".mkv": "video/x-matroska",
        ".avi": "video/x-msvideo",
        ".mov": "video/quicktime"
    }
    mimetype = mime_types.get(ext, "application/octet-stream")
    
    # Suppression différée
    if DELETE_AFTER_DOWNLOAD:
        def delete_later():
            try:
                if file_path.exists():
                    file_path.unlink()
                    logger.info(f"🗑️ Fichier supprimé: {filename}")
            except Exception as e:
                logger.error(f"Erreur suppression: {e}")
        
        timer = threading.Timer(CLEANUP_DELAY, delete_later)
        timer.daemon = True
        timer.start()
    
    logger.info(f"✅ Envoi du fichier: {file_path.name} ({file_path.stat().st_size} octets)")
    
    return send_file(
        file_path,
        as_attachment=True,
        download_name=filename,
        mimetype=mimetype,
        conditional=True
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
        "socket_timeout": 30,
        "http_headers": {
            "User-Agent": USER_AGENT,
            "Accept-Encoding": "gzip, deflate"
        },
        "extractor_args": {
            "youtube": {"player_client": ["android"]},
            "pinterest": {"use_api": ["1"]}
        }
    }
    
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
        
        if not info:
            return jsonify({"error": "Aucune information"}), 404
        
        return jsonify({
            "title": info.get("title", "VENS-DOWNLOADER"),
            "thumbnail": info.get("thumbnail", ""),
            "duration": info.get("duration", 0),
            "platform": detect_platform(url),
            "ext": info.get("ext", "mp4")
        })
    
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# =========================
# DEBUG - LISTE DES FICHIERS
# =========================

@app.get("/debug/files")
def debug_files():
    if not DEBUG:
        return jsonify({"error": "Debug mode disabled"}), 403
    
    if not check_api_key():
        return jsonify({"error": "Invalid API key"}), 401
    
    files = []
    for f in DOWNLOAD_DIR.iterdir():
        if f.is_file():
            files.append({
                "name": f.name,
                "size": f.stat().st_size,
                "created": f.stat().st_mtime,
                "age_seconds": round(time.time() - f.stat().st_mtime, 2)
            })
    
    return jsonify({
        "count": len(files),
        "files": files,
        "directory": str(DOWNLOAD_DIR),
        "delete_after_download": DELETE_AFTER_DOWNLOAD,
        "cleanup_delay": CLEANUP_DELAY,
        "file_expire_time": FILE_EXPIRE_TIME,
        "debug": DEBUG,
        "base_url": BASE_URL
    })

# =========================
# STATS ENDPOINT
# =========================

@app.get("/stats")
def stats():
    if not check_api_key():
        return jsonify({"error": "Invalid API key"}), 401
    
    files = list(DOWNLOAD_DIR.iterdir())
    total_size = sum(f.stat().st_size for f in files if f.is_file())
    
    return jsonify({
        "total_files": len(files),
        "total_size_mb": round(total_size / (1024 * 1024), 2),
        "active_downloads": active_downloads,
        "max_concurrent": MAX_CONCURRENT_DOWNLOADS,
        "ffmpeg": FFMPEG_AVAILABLE,
        "storage_free_mb": shutil.disk_usage(DOWNLOAD_DIR).free // (1024*1024),
        "base_url": BASE_URL
    })

# =========================
# RUN SERVER
# =========================

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    debug = os.environ.get("DEBUG", "True").lower() == "true"
    
    print(f"""
╔══════════════════════════════════════════════════════════════════════╗
║  🚀 VENS-DOWNLOADER SERVER v12.6 — FINAL 10/10                    ║
║  📦 yt-dlp: {yt_dlp.version.__version__}                                      ║
║  ✅ FFmpeg: {'✅' if FFMPEG_AVAILABLE else '❌'}                                                    ║
║  🌐 BASE_URL: {BASE_URL}     ║
║  🔐 API Key: {'✅' if API_KEY else '❌'}                                                       ║
║  📁 Downloads: {DOWNLOAD_DIR}          ║
║  🔍 Debug: {'✅' if DEBUG else '❌'}                                                  ║
║  📂 Fallback: ❌ NON (404 strict)                                      ║
║  🖼️ Preview: /preview/ disponible                                      ║
║  ✨ Auto-détection du domaine: OUI                                   ║
╚══════════════════════════════════════════════════════════════════════╝
    """)
    
    app.run(host="0.0.0.0", port=port, debug=debug)
