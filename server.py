"""
VENS-DOWNLOADER — Production Server v11.3 (10/10)
✅ FIX: after_this_request remplacé par threading.Timer
✅ FIX: Accept-Encoding sans "br"
✅ FIX: socket_timeout ajouté
✅ Architecture finale optimisée
"""

import os
import re
import time
import uuid
import shutil
import threading
import logging
from pathlib import Path
from urllib.parse import urlparse

from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
import yt_dlp


# =========================
# CONFIGURATION
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

# Variables d'environnement
API_KEY = os.environ.get("API_KEY", "").strip()
DOWNLOAD_DIR = Path(os.environ.get("DOWNLOAD_DIR", "/tmp/vens_downloads"))
MAX_FILE_SIZE = int(os.environ.get("MAX_FILE_SIZE", "500")) * 1024 * 1024
FILE_EXPIRE_TIME = int(os.environ.get("FILE_EXPIRE_TIME", "300"))
MAX_CONCURRENT_DOWNLOADS = int(os.environ.get("MAX_CONCURRENT_DOWNLOADS", "2"))
DELETE_AFTER_DOWNLOAD = os.environ.get("DELETE_AFTER_DOWNLOAD", "true").lower() == "true"
CLEANUP_DELAY = int(os.environ.get("CLEANUP_DELAY", "10"))  # Secondes avant suppression

# Créer le dossier
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

# Logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Compteur de téléchargements actifs
active_downloads = 0
download_lock = threading.Lock()

# =========================
# VÉRIFICATION FFmpeg
# =========================

def check_ffmpeg():
    """Vérifier que FFmpeg est installé"""
    ffmpeg_path = shutil.which("ffmpeg")
    if ffmpeg_path:
        logger.info(f"✅ FFmpeg trouvé: {ffmpeg_path}")
        version = os.popen("ffmpeg -version 2>/dev/null | head -n1").read().strip()
        logger.info(f"   Version: {version}")
        return True
    else:
        logger.error("❌ FFmpeg NON trouvé - Installation requise")
        logger.error("   Sur Render: ajouter 'ffmpeg' dans Dockerfile")
        return False

# Vérifier au démarrage
FFMPEG_AVAILABLE = check_ffmpeg()

if not FFMPEG_AVAILABLE:
    logger.warning("⚠️  ATTENTION: FFmpeg est requis pour la fusion audio/vidéo")

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
    """Nettoyer le nom de fichier"""
    return re.sub(r"[^a-zA-Z0-9._-]", "_", filename)

def is_within_download_dir(filepath):
    """Vérifier que le fichier est bien dans DOWNLOAD_DIR"""
    try:
        resolved_path = filepath.resolve()
        return DOWNLOAD_DIR.resolve() in resolved_path.parents or resolved_path.parent == DOWNLOAD_DIR.resolve()
    except:
        return False

# =========================
# FORMAT - SIMPLIFIÉ
# =========================

def get_format(quality):
    """Obtenir le format yt-dlp - limité à 720p pour Render"""
    q = (quality or "720").lower()
    
    if q == "audio":
        return "bestaudio/best"
    
    # Limiter à 720p maximum pour Render
    if q in ("2160", "4k", "1080"):
        logger.warning(f"Qualité {q} demandée, limitée à 720p pour Render")
        q = "720"
    
    return "bestvideo[height<=720]+bestaudio/best[height<=720]/best"

# =========================
# DÉTECTION PLATEFORME
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
        "snapchat.com": "Snapchat",
        "dailymotion.com": "Dailymotion",
        "vimeo.com": "Vimeo"
    }
    
    for domain, name in platforms.items():
        if domain in url:
            return name
    return "Unknown"

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
        "version": "11.3",
        "mode": "production",
        "auth_required": bool(API_KEY),
        "ffmpeg": FFMPEG_AVAILABLE,
        "max_quality": "720p",
        "active_downloads": active_downloads,
        "max_concurrent": MAX_CONCURRENT_DOWNLOADS,
        "download_dir": str(DOWNLOAD_DIR),
        "storage_free_mb": shutil.disk_usage(DOWNLOAD_DIR).free // (1024*1024),
        "delete_after_download": DELETE_AFTER_DOWNLOAD,
        "cleanup_delay": CLEANUP_DELAY
    })

# =========================
# MOTEUR DE TÉLÉCHARGEMENT - V11.3
# =========================

def download_media(url, quality):
    """Télécharger un média avec yt-dlp"""
    global active_downloads
    
    # Vérifier les téléchargements concurrents
    with download_lock:
        if active_downloads >= MAX_CONCURRENT_DOWNLOADS:
            raise Exception("Serveur occupé, réessayez dans quelques instants")
        active_downloads += 1
    
    try:
        # 1. Générer un ID unique
        file_uuid = str(uuid.uuid4())
        output_template = str(DOWNLOAD_DIR / f"{file_uuid}.%(ext)s")
        
        # 2. Configurer yt-dlp (SANS extractor_args)
        ydl_opts = {
            "format": get_format(quality),
            "outtmpl": output_template,
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "nocheckcertificate": True,
            "ignoreerrors": False,
            "retries": 5,
            "fragment_retries": 5,
            "merge_output_format": "mp4" if not quality.lower() == "audio" else None,
            "extract_flat": False,
            "geo_bypass": True,
            "geo_bypass_country": "FR",
            "sleep_interval": 1,
            "max_sleep_interval": 3,
            "socket_timeout": 30,  # ⭐ NOUVEAU - Évite les blocages
            "http_headers": {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
                "Accept-Language": "fr-FR,fr;q=0.9,en-US;q=0.8,en;q=0.7",
                "Accept-Encoding": "gzip, deflate",  # ⭐ CORRIGÉ - Sans "br"
                "DNT": "1",
                "Connection": "keep-alive"
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
        
        # 3. Télécharger
        platform = detect_platform(url)
        logger.info(f"📥 [{platform}] Téléchargement: {url[:60]}... (qualité: {quality})")
        
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                
            if not info:
                raise Exception("Aucune information récupérée")
                
        except Exception as e:
            logger.error(f"❌ Erreur yt-dlp: {e}")
            for f in DOWNLOAD_DIR.glob(f"{file_uuid}.*"):
                f.unlink(missing_ok=True)
            raise Exception(f"Erreur de téléchargement: {str(e)}")
        
        # 4. Trouver le fichier - Méthode robuste
        final_file = None
        
        # Méthode 1: Pattern exact
        files = list(DOWNLOAD_DIR.glob(f"{file_uuid}.*"))
        if files:
            final_file = files[0]
        
        # Méthode 2: Fichiers récents
        if not final_file:
            current_time = time.time()
            for file in DOWNLOAD_DIR.iterdir():
                if file.is_file() and (current_time - file.stat().st_mtime) < 10:
                    if file.name.startswith(file_uuid) or not final_file:
                        final_file = file
                        if file.name.startswith(file_uuid):
                            break
        
        # Méthode 3: Dernier fichier
        if not final_file:
            files = list(DOWNLOAD_DIR.iterdir())
            if files:
                final_file = max(files, key=lambda f: f.stat().st_mtime)
        
        if not final_file or not final_file.exists():
            raise Exception("Fichier introuvable après téléchargement")
        
        # 5. Vérifier la taille
        file_size = final_file.stat().st_size
        if file_size == 0:
            final_file.unlink()
            raise Exception("Fichier vide")
        
        if file_size > MAX_FILE_SIZE:
            final_file.unlink()
            raise Exception(f"Fichier trop volumineux ({file_size} > {MAX_FILE_SIZE})")
        
        # 6. Renommer proprement
        ext = final_file.suffix
        clean_name = f"vens_{int(time.time())}_{file_uuid[:8]}{ext}"
        clean_path = DOWNLOAD_DIR / clean_name
        
        if final_file != clean_path:
            final_file.rename(clean_path)
            final_file = clean_path
        
        logger.info(f"✅ [{platform}] Téléchargement réussi: {clean_name} ({file_size//1024} KB)")
        return final_file, info
        
    finally:
        with download_lock:
            active_downloads -= 1

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
            "title": info.get("title", "VENS-DOWNLOADER"),
            "thumbnail": info.get("thumbnail", ""),
            "duration": info.get("duration", 0),
            "platform": detect_platform(url),
            "filename": file_path.name,
            "filesize": file_path.stat().st_size,
            "media_type": "audio" if quality.lower() == "audio" else "video",
            "download_url": f"/download/{file_path.name}",
            "ext": file_path.suffix[1:],
            "quality": quality,
            "expires_in": FILE_EXPIRE_TIME
        })
    
    except Exception as e:
        try:
            for f in DOWNLOAD_DIR.iterdir():
                if f.is_file() and (time.time() - f.stat().st_mtime) < 5:
                    f.unlink(missing_ok=True)
        except:
            pass
        
        logger.error(f"❌ Erreur extraction: {e}")
        return jsonify({
            "error": str(e),
            "detail": "Erreur lors du téléchargement"
        }), 502

# =========================
# DOWNLOAD ROUTE (AVEC SUPPRESSION DIFFÉRÉE)
# =========================

@app.get("/download/<filename>")
def download(filename):
    if not check_api_key():
        return jsonify({"error": "Invalid API key"}), 401
    
    filename = safe_filename(filename)
    file_path = DOWNLOAD_DIR / filename
    
    # Vérifier que le fichier est bien dans le dossier
    if not is_within_download_dir(file_path):
        logger.warning(f"⚠️ Tentative de sortie du dossier: {filename}")
        return jsonify({"error": "Accès interdit"}), 403
    
    if not file_path.exists():
        return jsonify({"error": "Fichier expiré ou introuvable"}), 404
    
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
    
    # ⭐ CORRIGÉ - Suppression différée avec threading.Timer
    if DELETE_AFTER_DOWNLOAD:
        def delete_later():
            try:
                if file_path.exists():
                    file_path.unlink()
                    logger.info(f"🗑️ Fichier supprimé après {CLEANUP_DELAY}s: {filename}")
            except Exception as e:
                logger.error(f"Erreur suppression: {e}")
        
        timer = threading.Timer(CLEANUP_DELAY, delete_later)
        timer.daemon = True
        timer.start()
    
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
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept-Encoding": "gzip, deflate"
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
            if height and height <= 720:
                available_qualities.add(f"{height}p")
        
        return jsonify({
            "title": info.get("title", "VENS-DOWNLOADER"),
            "thumbnail": info.get("thumbnail", ""),
            "duration": info.get("duration", 0),
            "platform": detect_platform(url),
            "available_qualities": sorted(list(available_qualities)) or ["720p"],
            "ext": info.get("ext", "mp4"),
            "description": info.get("description", "")[:200]
        })
    
    except Exception as e:
        return jsonify({"error": str(e)}), 500

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
        "delete_after_download": DELETE_AFTER_DOWNLOAD,
        "cleanup_delay": CLEANUP_DELAY
    })

# =========================
# RUN SERVER
# =========================

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    debug = os.environ.get("DEBUG", "False").lower() == "true"
    
    print("""
╔══════════════════════════════════════════════════════════════════════╗
║  🚀 VENS-DOWNLOADER SERVER v11.3 — 10/10 FINAL                     ║
║  ✅ FFmpeg:  {}                                                    ║
║  📁 Downloads: {}  ║
║  🔐 API Key:  {}                       ║
║  📊 Max concurrent: {} (optimisé Render)                           ║
║  🎯 Max quality: 720p                                              ║
║  🗑️ Delete after download: {} (après {}s)                          ║
║  🌐 Server: http://0.0.0.0:{}                                      ║
║  ⏱️  Socket timeout: 30s                                           ║
╚══════════════════════════════════════════════════════════════════════╝
    """.format(
        "✅" if FFMPEG_AVAILABLE else "❌",
        DOWNLOAD_DIR,
        "✅" if API_KEY else "❌",
        MAX_CONCURRENT_DOWNLOADS,
        "✅" if DELETE_AFTER_DOWNLOAD else "❌",
        CLEANUP_DELAY,
        port
    ))
    
    app.run(host="0.0.0.0", port=port, debug=debug)
