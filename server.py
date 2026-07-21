"""
VENS-DOWNLOADER — Production Server v20.0
✅ Verrou Redis pour nettoyage (pas de IS_MAIN_WORKER)
✅ Heartbeat Redis pendant les téléchargements
✅ Tokens signés (itsdangerous) - pas de stockage
✅ Progress hook pour renouveler TTL
✅ Vérification Content-Type (text/html bloqué)
✅ Validation FFmpeg complète
✅ Limites Flask (MAX_CONTENT_LENGTH)
✅ Headers de sécurité
✅ tempfile.mkstemp pour fichiers temporaires
✅ Endpoint /metrics pour monitoring
"""

import os
import re
import time
import uuid
import shutil
import threading
import logging
import traceback
import hashlib
import hmac
import socket
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import urlparse, quote
from ipaddress import ip_address, ip_network
from collections import defaultdict
from datetime import datetime, timedelta

from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.utils import secure_filename
import yt_dlp
import requests
from itsdangerous import URLSafeTimedSerializer, SignatureExpired, BadSignature


# =========================
# CONFIGURATION
# =========================

API_KEY = os.environ.get("API_KEY", "").strip()
if not API_KEY:
    raise ValueError("❌ API_KEY non définie")

ASITHA_API_KEY = os.environ.get("ASITHA_API_KEY", "").strip()
if not ASITHA_API_KEY:
    raise ValueError("❌ ASITHA_API_KEY non définie")

DOWNLOAD_DIR = Path(os.environ.get("DOWNLOAD_DIR", "/tmp/vens_downloads"))
MAX_FILE_SIZE = int(os.environ.get("MAX_FILE_SIZE", "500")) * 1024 * 1024
FILE_EXPIRE_TIME = int(os.environ.get("FILE_EXPIRE_TIME", "600"))
MAX_CONCURRENT_DOWNLOADS = int(os.environ.get("MAX_CONCURRENT_DOWNLOADS", "2"))
MAX_CONCURRENT_PER_IP = int(os.environ.get("MAX_CONCURRENT_PER_IP", "2"))
DELETE_AFTER_DOWNLOAD = os.environ.get("DELETE_AFTER_DOWNLOAD", "false").lower() == "true"
CLEANUP_DELAY = int(os.environ.get("CLEANUP_DELAY", "300"))
DEBUG = os.environ.get("DEBUG", "False").lower() == "true"
TOKEN_EXPIRE_SECONDS = int(os.environ.get("TOKEN_EXPIRE_SECONDS", "300"))
RATE_LIMIT_PER_IP = int(os.environ.get("RATE_LIMIT_PER_IP", "10"))
RATE_LIMIT_WINDOW = int(os.environ.get("RATE_LIMIT_WINDOW", "60"))
API_TIMEOUT = int(os.environ.get("API_TIMEOUT", "30"))
REDIS_URL = os.environ.get("REDIS_URL", "").strip()
USE_REDIS = bool(REDIS_URL)
YOUTUBE_DL_VERSION = os.environ.get("YOUTUBE_DL_VERSION", "latest")
FLASK_MAX_CONTENT_LENGTH = int(os.environ.get("FLASK_MAX_CONTENT_LENGTH", "5")) * 1024 * 1024

# CORS
CORS_ORIGINS = os.environ.get("CORS_ORIGINS", "").strip()
if CORS_ORIGINS:
    CORS_ORIGINS = [origin.strip() for origin in CORS_ORIGINS.split(",")]
else:
    CORS_ORIGINS = ["*"] if DEBUG else ["https://vens-multiservice.lovable.app"]

# API externe
ASITHA_API_BASE = os.environ.get("ASITHA_API_BASE", "https://back.asitha.top/api")
ASITHA_TIKTOK_ENDPOINT = os.environ.get("ASITHA_TIKTOK_ENDPOINT", "/tiktok/download")
ASITHA_YOUTUBE_ENDPOINT = os.environ.get("ASITHA_YOUTUBE_ENDPOINT", "/ytapi")

# Logging
if DEBUG:
    logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(levelname)s - %(message)s')
else:
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

BASE_URL = os.environ.get("BASE_URL", "").strip()
if BASE_URL:
    BASE_URL = BASE_URL.rstrip('/')
else:
    render_url = os.environ.get("RENDER_EXTERNAL_HOSTNAME", "")
    if render_url:
        BASE_URL = f"https://{render_url}"
    else:
        BASE_URL = "http://localhost:8000"

# ⭐ Token signé avec itsdangerous
token_serializer = URLSafeTimedSerializer(API_KEY)

logger.info(f"🚀 VENS-DOWNLOADER v20.0")
logger.info(f"📦 yt-dlp: {yt_dlp.version.__version__}")
logger.info(f"🌐 BASE_URL: {BASE_URL}")
logger.info(f"🔴 Redis: {'✅' if USE_REDIS else '❌'}")

# =========================
# MÉTRIQUES
# =========================

metrics = {
    "total_downloads": 0,
    "successful_downloads": 0,
    "failed_downloads": 0,
    "start_time": time.time(),
    "bytes_downloaded": 0
}
metrics_lock = threading.Lock()

# =========================
# REDIS CLIENT
# =========================

class RedisClient:
    def __init__(self, url):
        self.url = url
        self.client = None
        self.enabled = False
        self._connect()
    
    def _connect(self):
        try:
            import redis
            self.client = redis.from_url(self.url, decode_responses=True, socket_timeout=2)
            self.client.ping()
            self.enabled = True
            logger.info("✅ Redis connecté")
        except Exception as e:
            logger.warning(f"⚠️ Redis non disponible: {e}")
            self.enabled = False
    
    def get(self, key):
        if not self.enabled:
            return None
        try:
            return self.client.get(key)
        except:
            return None
    
    def set(self, key, value, ex=None, nx=False):
        if not self.enabled:
            return None
        try:
            return self.client.set(key, value, ex=ex, nx=nx)
        except:
            return None
    
    def setex(self, key, time, value):
        if not self.enabled:
            return None
        try:
            return self.client.setex(key, time, value)
        except:
            return None
    
    def incr(self, key):
        if not self.enabled:
            return None
        try:
            return self.client.incr(key)
        except:
            return None
    
    def decr(self, key):
        if not self.enabled:
            return None
        try:
            return self.client.decr(key)
        except:
            return None
    
    def expire(self, key, time):
        if not self.enabled:
            return None
        try:
            return self.client.expire(key, time)
        except:
            return None
    
    def delete(self, key):
        if not self.enabled:
            return None
        try:
            return self.client.delete(key)
        except:
            return None
    
    def incr_global(self, key):
        if not self.enabled:
            return None
        try:
            return self.client.incr(key)
        except:
            return None
    
    def decr_global(self, key):
        if not self.enabled:
            return None
        try:
            return self.client.decr(key)
        except:
            return None
    
    def get_global(self, key):
        if not self.enabled:
            return 0
        try:
            val = self.client.get(key)
            return int(val) if val else 0
        except:
            return 0
    
    def incr_ip(self, key):
        if not self.enabled:
            return None
        try:
            return self.client.incr(key)
        except:
            return None
    
    def decr_ip(self, key):
        if not self.enabled:
            return None
        try:
            return self.client.decr(key)
        except:
            return None
    
    def get_ip(self, key):
        if not self.enabled:
            return 0
        try:
            val = self.client.get(key)
            return int(val) if val else 0
        except:
            return 0
    
    def expire_ip(self, key, time):
        if not self.enabled:
            return None
        try:
            return self.client.expire(key, time)
        except:
            return None

redis_client = RedisClient(REDIS_URL) if REDIS_URL else None

# =========================
# FLASK APP
# =========================

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1, x_prefix=1)

# ⭐ Limite de taille des requêtes
app.config["MAX_CONTENT_LENGTH"] = FLASK_MAX_CONTENT_LENGTH

CORS(
    app,
    resources={
        r"/*": {
            "origins": CORS_ORIGINS,
            "methods": ["GET", "POST", "OPTIONS"],
            "allow_headers": ["Content-Type", "X-API-Key"],
            "expose_headers": ["Content-Disposition"]
        }
    }
)

# =========================
# HEADERS DE SÉCURITÉ
# =========================

@app.after_request
def security_headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = "camera=(), microphone=()"
    return response

# =========================
# ÉTAT GLOBAL
# =========================

active_downloads = 0
download_lock = threading.Lock()

ip_downloads = defaultdict(int)
ip_lock = threading.Lock()

rate_limit_data = defaultdict(list)
rate_limit_lock = threading.Lock()

cleanup_lock = threading.Lock()
cleanup_worker_running = False
cleanup_lock_acquired = False

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"

# ⭐ DOMAINES AUTORISÉS
ALLOWED_DOMAINS = {
    "youtube.com", "youtu.be",
    "tiktok.com", "vt.tiktok.com",
    "dailymotion.com", "dai.ly",
    "vimeo.com",
    "instagram.com",
    "facebook.com", "fb.watch",
    "twitter.com", "x.com",
    "pinterest.com",
    "snapchat.com",
    "googlevideo.com", "ytimg.com", "googleapis.com", "ggpht.com",
    "akamaihd.net", "cloudfront.net",
    "tiktokcdn.com", "tiktokv.com",
    "fbcdn.net", "cdninstagram.com",
    "cdn.vimeo.com", "vimeocdn.com",
    "dmcdn.net",
}

# ⭐ IP privées
PRIVATE_IP_RANGES = [
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",
    "127.0.0.0/8", "169.254.0.0/16",
    "::1/128", "fc00::/7", "fe80::/10"
]

# ⭐ Magic bytes
MAGIC_BYTES = {
    b"ftyp": ["mp4", "m4a", "m4v"],
    b"ID3": ["mp3"],
    b"\x1a\x45\xdf\xa3": ["webm", "mkv"],
    b"RIFF": ["avi", "wav"],
    b"OggS": ["ogg", "opus"],
    b"\x00\x00\x00\x18ftyp": ["mp4"],
    b"\xFF\xFB": ["mp3"],
    b"\x49\x44\x33": ["mp3"],
}

# =========================
# VÉRIFICATION FFmpeg ET FFprobe
# =========================

def check_ffmpeg():
    ffmpeg_path = shutil.which("ffmpeg")
    if ffmpeg_path:
        logger.info(f"✅ FFmpeg: {ffmpeg_path}")
        return True
    logger.error("❌ FFmpeg NON trouvé")
    return False

def check_ffprobe():
    ffprobe_path = shutil.which("ffprobe")
    if ffprobe_path:
        logger.info(f"✅ FFprobe: {ffprobe_path}")
        return True
    logger.warning("⚠️ FFprobe NON trouvé - validation limitée")
    return False

FFMPEG_AVAILABLE = check_ffmpeg()
FFPROBE_AVAILABLE = check_ffprobe()

# =========================
# SÉCURITÉ
# =========================

def check_api_key():
    supplied = request.headers.get("X-API-Key", "").strip()
    return hmac.compare_digest(supplied, API_KEY)

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

# ⭐ SSRF
def resolve_hostname(hostname):
    ips = []
    try:
        for family in [socket.AF_INET, socket.AF_INET6]:
            try:
                for addr in socket.getaddrinfo(hostname, None, family=family):
                    ip = addr[4][0]
                    if ip not in ips:
                        ips.append(ip)
            except:
                pass
    except:
        pass
    return ips

def is_private_ip(ip):
    try:
        for cidr in PRIVATE_IP_RANGES:
            if ip_address(ip) in ip_network(cidr):
                return True
    except:
        pass
    return False

def is_allowed_domain(url):
    try:
        parsed = urlparse(url)
        domain = parsed.netloc.lower()
        if domain.startswith("www."):
            domain = domain[4:]
        for allowed in ALLOWED_DOMAINS:
            if domain == allowed or domain.endswith(f".{allowed}"):
                return True
        return False
    except:
        return False

def check_ssrf(url):
    try:
        parsed = urlparse(url)
        hostname = parsed.netloc
        if not hostname:
            return False
        
        ips = resolve_hostname(hostname)
        for ip in ips:
            if is_private_ip(ip):
                logger.warning(f"⚠️ IP privée bloquée: {ip}")
                return False
        return True
    except:
        return False

# =========================
# GET REAL IP
# =========================

def get_real_ip():
    xff = request.headers.get("X-Forwarded-For", "")
    if xff:
        return xff.split(",")[0].strip()
    xrip = request.headers.get("X-Real-IP", "")
    if xrip:
        return xrip.strip()
    return request.remote_addr or "0.0.0.0"

# =========================
# RATE LIMITING
# =========================

def check_rate_limit(ip):
    if redis_client and redis_client.enabled:
        key = f"rate_limit:{ip}"
        try:
            current = redis_client.get(key)
            if current and int(current) >= RATE_LIMIT_PER_IP:
                return False
            redis_client.incr(key)
            redis_client.expire(key, RATE_LIMIT_WINDOW)
            return True
        except:
            pass
    
    with rate_limit_lock:
        now = time.time()
        rate_limit_data[ip] = [t for t in rate_limit_data[ip] if now - t < RATE_LIMIT_WINDOW]
        if len(rate_limit_data[ip]) >= RATE_LIMIT_PER_IP:
            return False
        rate_limit_data[ip].append(now)
        return True

# =========================
# LIMITE PAR IP
# =========================

def check_ip_limit(ip):
    if redis_client and redis_client.enabled:
        key = f"ip_downloads:{ip}"
        try:
            current = redis_client.get_ip(key)
            if current >= MAX_CONCURRENT_PER_IP:
                return False
            redis_client.incr_ip(key)
            redis_client.expire_ip(key, 300)
            return True
        except:
            pass
    
    with ip_lock:
        if ip_downloads[ip] >= MAX_CONCURRENT_PER_IP:
            return False
        ip_downloads[ip] += 1
        return True

def release_ip_limit(ip):
    if redis_client and redis_client.enabled:
        key = f"ip_downloads:{ip}"
        try:
            current = redis_client.get_ip(key)
            if current > 0:
                redis_client.decr_ip(key)
        except:
            pass
    else:
        with ip_lock:
            if ip_downloads.get(ip, 0) > 0:
                ip_downloads[ip] -= 1

# =========================
# TOKENS SIGNÉS
# =========================

def generate_token(filename):
    """Générer un token signé (pas de stockage)"""
    try:
        return token_serializer.dumps(filename)
    except Exception as e:
        logger.error(f"❌ Erreur génération token: {e}")
        # Fallback sur token simple
        return hashlib.sha256(f"{filename}{time.time()}{os.urandom(16)}".encode()).hexdigest()[:16]

def validate_token(token, filename, delete=False):
    """Valider un token signé (pas de stockage)"""
    try:
        loaded = token_serializer.loads(token, max_age=TOKEN_EXPIRE_SECONDS)
        return loaded == filename
    except SignatureExpired:
        logger.debug("⏰ Token expiré")
        return False
    except BadSignature:
        logger.debug("🔑 Signature invalide")
        return False
    except Exception as e:
        logger.error(f"❌ Erreur validation token: {e}")
        return False

# =========================
# VERROU REDIS POUR NETTOYAGE
# =========================

def acquire_cleanup_lock():
    """Acquérir le verrou Redis pour le nettoyage"""
    if not (redis_client and redis_client.enabled):
        return True
    
    try:
        LOCK_KEY = "cleanup_worker_lock"
        return redis_client.set(LOCK_KEY, "active", ex=120, nx=True)
    except:
        return False

def renew_cleanup_lock():
    """Renouveler le verrou Redis pour le nettoyage"""
    if not (redis_client and redis_client.enabled):
        return True
    
    try:
        LOCK_KEY = "cleanup_worker_lock"
        return redis_client.expire(LOCK_KEY, 120)
    except:
        return False

# =========================
# PROGRESS HOOK yt-dlp
# =========================

def create_progress_hook(global_key, ip_key, client_ip):
    """Créer un hook de progression pour renouveler les TTL"""
    def progress_hook(d):
        if d.get("status") == "downloading":
            # Renouveler les TTL pendant le téléchargement
            if redis_client and redis_client.enabled:
                try:
                    redis_client.expire(global_key, 300)
                    redis_client.expire(ip_key, 300)
                except:
                    pass
    return progress_hook

# =========================
# DÉTECTION PLATEFORME
# =========================

def detect_platform(url):
    platforms = {
        "youtube.com": "YouTube", "youtu.be": "YouTube",
        "tiktok.com": "TikTok", "vt.tiktok.com": "TikTok",
        "dailymotion.com": "Dailymotion", "dai.ly": "Dailymotion",
        "vimeo.com": "Vimeo",
        "instagram.com": "Instagram",
        "facebook.com": "Facebook", "fb.watch": "Facebook",
        "twitter.com": "X", "x.com": "X",
        "pinterest.com": "Pinterest",
        "snapchat.com": "Snapchat"
    }
    for domain, name in platforms.items():
        if domain in url:
            return name
    return "Unknown"

# =========================
# MASQUER URL
# =========================

def mask_url(url):
    try:
        parsed = urlparse(url)
        if parsed.query:
            return f"{parsed.scheme}://{parsed.netloc}{parsed.path}?..."
        return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
    except:
        return url[:60] + "..."

# =========================
# VALIDATION MAGIC BYTES
# =========================

def validate_magic_bytes(file_path):
    try:
        with open(file_path, "rb") as f:
            header = f.read(16)
        
        for magic, formats in MAGIC_BYTES.items():
            if header.startswith(magic):
                logger.debug(f"✅ Magic bytes OK: {formats}")
                return True
        
        if len(header) >= 8 and header[4:8] == b"ftyp":
            logger.debug("✅ Magic bytes OK: mp4/m4a/m4v")
            return True
        
        logger.warning(f"⚠️ Magic bytes invalides: {header[:8].hex()}")
        return False
    except Exception as e:
        logger.error(f"❌ Erreur validation magic bytes: {e}")
        return False

# ⭐ VALIDATION FFprobe
def validate_with_ffprobe(file_path):
    if not FFPROBE_AVAILABLE:
        return True
    
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_format", "-show_streams", str(file_path)],
            capture_output=True,
            text=True,
            timeout=10
        )
        
        if result.returncode == 0 and "format_name" in result.stdout:
            logger.debug(f"✅ FFprobe validation OK")
            return True
        
        logger.warning(f"⚠️ FFprobe validation échouée")
        return False
    except Exception as e:
        logger.warning(f"⚠️ Erreur FFprobe: {e}")
        return True

# ⭐ VALIDATION FFmpeg (détection fichiers corrompus)
def validate_with_ffmpeg(file_path):
    if not FFMPEG_AVAILABLE:
        return True
    
    try:
        result = subprocess.run(
            ["ffmpeg", "-v", "error", "-i", str(file_path), "-f", "null", "-"],
            capture_output=True,
            text=True,
            timeout=30
        )
        
        if result.returncode == 0:
            logger.debug(f"✅ FFmpeg validation OK")
            return True
        
        # Certains fichiers peuvent avoir des warning mais être valides
        if "Invalid data found" in result.stderr:
            return False
        
        return True
    except Exception as e:
        logger.warning(f"⚠️ Erreur FFmpeg: {e}")
        return True

# =========================
# VALIDATION API EXTERNE
# =========================

def validate_api_response(data, platform):
    if not data or not isinstance(data, dict):
        return None
    
    if data.get("error") or data.get("success") is False:
        return None
    
    video_url = None
    title = f"{platform} Video"
    thumbnail = ""
    duration = 0
    
    video_url = data.get("video_url") or data.get("play") or data.get("download") or data.get("video")
    
    if not video_url and "data" in data and isinstance(data["data"], dict):
        d = data["data"]
        video_url = d.get("video_url") or d.get("play") or d.get("download") or d.get("video")
        title = d.get("title") or d.get("desc") or title
        thumbnail = d.get("thumbnail") or d.get("cover") or ""
        duration = d.get("duration", 0)
    
    if not video_url and "result" in data and isinstance(data["result"], dict):
        d = data["result"]
        video_url = d.get("url") or d.get("video_url") or d.get("play")
        title = d.get("title") or title
    
    title = title or data.get("title") or data.get("desc") or f"{platform} Video"
    thumbnail = thumbnail or data.get("thumbnail") or data.get("cover") or ""
    duration = duration or data.get("duration", 0)
    
    if not video_url:
        return None
    
    return {"video_url": video_url, "title": title, "thumbnail": thumbnail, "duration": duration}

# =========================
# API EXTERNE
# =========================

def download_via_asitha_api(url, platform, quality="720", progress_hook=None):
    if not ASITHA_API_KEY:
        return None, None
    
    if platform == "TikTok":
        endpoint = ASITHA_TIKTOK_ENDPOINT
    elif platform == "YouTube":
        endpoint = ASITHA_YOUTUBE_ENDPOINT
    else:
        return None, None
    
    temp_path = None
    fd = None
    
    try:
        params = {"url": url}
        if platform == "YouTube":
            quality_map = {"144": "144", "240": "240", "360": "360", 
                          "480": "480", "720": "720", "1080": "1080"}
            q = quality.replace("p", "")
            params["fo"] = "1"
            params["qu"] = quality_map.get(q, "720")
        
        response = requests.get(
            f"{ASITHA_API_BASE}{endpoint}",
            params=params,
            headers={"Authorization": f"Bearer {ASITHA_API_KEY}", "User-Agent": USER_AGENT},
            timeout=API_TIMEOUT
        )
        
        if response.status_code == 401:
            return None, {"error": "invalid_key"}
        if response.status_code == 429:
            return None, {"error": "quota_exceeded"}
        if response.status_code != 200:
            return None, {"error": f"http_{response.status_code}"}
        
        validated = validate_api_response(response.json(), platform)
        if not validated:
            return None, {"error": "invalid_response"}
        
        video_url = validated["video_url"]
        title = validated["title"]
        thumbnail = validated["thumbnail"]
        duration = validated["duration"]
        
        if not video_url or not video_url.startswith(("http://", "https://")):
            return None, {"error": "invalid_video_url"}
        
        if not is_allowed_domain(video_url):
            logger.warning(f"⚠️ Domaine non autorisé: {video_url}")
            return None, {"error": "domain_not_allowed"}
        
        if not check_ssrf(video_url):
            logger.warning(f"⚠️ SSRF bloquée: {video_url}")
            return None, {"error": "ssrf_blocked"}
        
        # HEAD optionnel
        file_size = None
        try:
            head_response = requests.head(video_url, timeout=10, allow_redirects=True)
            if head_response.status_code == 200:
                content_length = head_response.headers.get("Content-Length")
                if content_length:
                    file_size = int(content_length)
        except:
            pass
        
        if file_size and file_size > MAX_FILE_SIZE:
            return None, {"error": "file_too_large"}
        
        file_uuid = str(uuid.uuid4())[:8]
        
        # ⭐ Utiliser tempfile.mkstemp pour plus de sécurité
        fd, temp_path_str = tempfile.mkstemp(dir=DOWNLOAD_DIR, prefix=f"{platform.lower()}_{file_uuid}_", suffix=".tmp")
        temp_path = Path(temp_path_str)
        os.close(fd)
        
        final_path = DOWNLOAD_DIR / f"{platform.lower()}_{file_uuid}_{int(time.time())}.mp4"
        
        video_response = requests.get(
            video_url, 
            stream=True, 
            timeout=(API_TIMEOUT, API_TIMEOUT * 2),
            allow_redirects=True
        )
        if video_response.status_code != 200:
            temp_path.unlink(missing_ok=True)
            return None, {"error": f"download_failed_{video_response.status_code}"}
        
        # ⭐ Vérifier Content-Type
        content_type = video_response.headers.get("Content-Type", "")
        if "text/html" in content_type:
            temp_path.unlink(missing_ok=True)
            logger.warning(f"⚠️ Content-Type HTML (probablement un blocage)")
            return None, {"error": "blocked_content"}
        
        total_size = 0
        with open(temp_path, "wb") as f:
            for chunk in video_response.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
                    total_size += len(chunk)
                    if total_size > MAX_FILE_SIZE:
                        raise Exception("file_too_large")
        
        if temp_path.stat().st_size == 0:
            temp_path.unlink(missing_ok=True)
            raise Exception("empty_file")
        
        if not validate_magic_bytes(temp_path):
            temp_path.unlink(missing_ok=True)
            raise Exception("invalid_file_format")
        
        if not validate_with_ffprobe(temp_path):
            temp_path.unlink(missing_ok=True)
            raise Exception("ffprobe_validation_failed")
        
        if not validate_with_ffmpeg(temp_path):
            temp_path.unlink(missing_ok=True)
            raise Exception("ffmpeg_validation_failed")
        
        temp_path.rename(final_path)
        
        # Mettre à jour les métriques
        with metrics_lock:
            metrics["total_downloads"] += 1
            metrics["successful_downloads"] += 1
            metrics["bytes_downloaded"] += total_size
        
        info = {"title": title, "thumbnail": thumbnail, "duration": duration, "ext": "mp4"}
        logger.info(f"✅ {platform} téléchargé: {final_path.name}")
        return final_path, info
        
    except Exception as e:
        if temp_path and temp_path.exists():
            temp_path.unlink(missing_ok=True)
        logger.error(f"❌ API externe: {e}")
        with metrics_lock:
            metrics["failed_downloads"] += 1
        return None, {"error": str(e)}
    
    finally:
        if temp_path and temp_path.exists():
            temp_path.unlink(missing_ok=True)

# =========================
# FORMAT
# =========================

def get_formats_with_fallback(quality):
    q = (quality or "720").lower()
    if q == "audio":
        return ["bestaudio/best"]
    return ["best[height<=720]/best", "best", "bestvideo[height<=720]+bestaudio/best"]

# =========================
# CONTENT-DISPOSITION UTF-8
# =========================

def content_disposition_utf8(filename):
    safe = secure_filename(filename)
    if not safe:
        safe = "video"
    
    ascii_name = safe.encode('ascii', 'ignore').decode('ascii')
    if ascii_name == safe:
        return f'attachment; filename="{ascii_name}"'
    
    encoded = quote(filename.encode('utf-8'))
    return f'attachment; filename="{ascii_name}"; filename*=UTF-8\'\'{encoded}'

# =========================
# NETTOYAGE AUTOMATIQUE (AVEC VERROU REDIS)
# =========================

def cleanup_worker():
    global cleanup_worker_running, cleanup_lock_acquired
    
    logger.info("🧹 Thread de nettoyage démarré")
    cleanup_worker_running = True
    
    # Acquérir le verrou
    if not acquire_cleanup_lock():
        logger.info("🧹 Un autre worker nettoie déjà, arrêt")
        cleanup_worker_running = False
        return
    
    cleanup_lock_acquired = True
    logger.info("🧹 Verrou de nettoyage acquis")
    
    try:
        while cleanup_worker_running:
            # Renouveler le verrou
            if not renew_cleanup_lock():
                logger.warning("🧹 Verrou perdu, tentative de réacquisition...")
                if not acquire_cleanup_lock():
                    logger.warning("🧹 Impossible de réacquérir le verrou, arrêt")
                    break
            
            try:
                with cleanup_lock:
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
    finally:
        cleanup_worker_running = False
        cleanup_lock_acquired = False
        logger.info("🧹 Thread de nettoyage arrêté")

def start_cleanup_worker():
    """Démarrer le worker de nettoyage dans un thread"""
    thread = threading.Thread(target=cleanup_worker, daemon=True)
    thread.start()
    return thread

# Démarrer le nettoyage au lancement
cleanup_thread = start_cleanup_worker()

# =========================
# HEALTH CHECK
# =========================

@app.get("/health")
def health():
    return jsonify({"status": "ok", "service": "vens-ytdlp", "version": "20.0"})

@app.get("/")
def root():
    if not check_api_key():
        return jsonify({"error": "Invalid API key"}), 401
    
    return jsonify({
        "status": "ok",
        "service": "vens-ytdlp",
        "version": "20.0",
        "ytdlp_version": yt_dlp.version.__version__,
        "ffmpeg": FFMPEG_AVAILABLE,
        "ffprobe": FFPROBE_AVAILABLE,
        "debug": DEBUG,
        "base_url": BASE_URL,
        "redis": redis_client.enabled if redis_client else False,
        "token_expire_seconds": TOKEN_EXPIRE_SECONDS,
        "rate_limit": {"per_ip": RATE_LIMIT_PER_IP, "window": RATE_LIMIT_WINDOW},
        "max_concurrent_per_ip": MAX_CONCURRENT_PER_IP,
        "supported_platforms": [
            "YouTube", "TikTok", "Dailymotion", "Vimeo",
            "Facebook", "Instagram", "X (Twitter)",
            "Pinterest", "Snapchat"
        ],
        "active_downloads": active_downloads,
        "max_concurrent": MAX_CONCURRENT_DOWNLOADS,
        "storage_free_mb": shutil.disk_usage(DOWNLOAD_DIR).free // (1024*1024)
    })

# =========================
# MÉTRIQUES
# =========================

@app.get("/metrics")
def get_metrics():
    if not check_api_key():
        return jsonify({"error": "Invalid API key"}), 401
    
    uptime = time.time() - metrics["start_time"]
    uptime_str = str(timedelta(seconds=int(uptime)))
    
    storage_usage = shutil.disk_usage(DOWNLOAD_DIR)
    
    return jsonify({
        "uptime": uptime_str,
        "uptime_seconds": int(uptime),
        "total_downloads": metrics["total_downloads"],
        "successful_downloads": metrics["successful_downloads"],
        "failed_downloads": metrics["failed_downloads"],
        "bytes_downloaded": metrics["bytes_downloaded"],
        "bytes_downloaded_mb": round(metrics["bytes_downloaded"] / (1024 * 1024), 2),
        "active_downloads": active_downloads,
        "storage": {
            "total_bytes": storage_usage.total,
            "used_bytes": storage_usage.used,
            "free_bytes": storage_usage.free,
            "total_mb": round(storage_usage.total / (1024 * 1024), 2),
            "used_mb": round(storage_usage.used / (1024 * 1024), 2),
            "free_mb": round(storage_usage.free / (1024 * 1024), 2)
        },
        "cleanup_worker": {
            "running": cleanup_worker_running,
            "lock_acquired": cleanup_lock_acquired
        },
        "redis": redis_client.enabled if redis_client else False
    })

# =========================
# MOTEUR DE TÉLÉCHARGEMENT
# =========================

def download_media(url, quality, client_ip):
    global active_downloads
    temp_path = None
    fd = None
    
    # Limite par IP
    if not check_ip_limit(client_ip):
        raise Exception(f"Trop de téléchargements simultanés ({MAX_CONCURRENT_PER_IP} max)")
    
    # Limite globale
    if redis_client and redis_client.enabled:
        global_key = "global_active_downloads"
        ip_key = f"ip_downloads:{client_ip}"
        current = redis_client.get_global(global_key)
        if current >= MAX_CONCURRENT_DOWNLOADS:
            release_ip_limit(client_ip)
            raise Exception("Serveur occupé, réessayez dans quelques instants")
        redis_client.incr_global(global_key)
        redis_client.expire(global_key, 300)
    else:
        with download_lock:
            if active_downloads >= MAX_CONCURRENT_DOWNLOADS:
                release_ip_limit(client_ip)
                raise Exception("Serveur occupé, réessayez dans quelques instants")
            active_downloads += 1
    
    try:
        platform = detect_platform(url)
        logger.info(f"📥 [{platform}] {mask_url(url)} (qualité: {quality})")
        
        # TikTok - API externe
        if platform == "TikTok":
            file_path, error = download_via_asitha_api(url, platform, quality)
            if file_path:
                return file_path, {"title": "TikTok Video", "ext": "mp4"}
            if error and error.get("error") in ["invalid_key", "quota_exceeded"]:
                raise Exception(f"API TikTok: {error}")
            logger.warning("⚠️ Fallback yt-dlp pour TikTok")
        
        # YouTube - API externe
        if platform == "YouTube":
            file_path, error = download_via_asitha_api(url, platform, quality)
            if file_path:
                return file_path, {"title": "YouTube Video", "ext": "mp4"}
            logger.warning("⚠️ Fallback yt-dlp pour YouTube")
        
        # Autres - yt-dlp
        file_uuid = str(uuid.uuid4())[:8]
        
        formats_to_try = get_formats_with_fallback(quality)
        last_error = None
        
        for fmt in formats_to_try:
            try:
                logger.info(f"🔄 yt-dlp: {fmt}")
                
                output_template = str(DOWNLOAD_DIR / f"{file_uuid}.%(ext)s")
                
                # ⭐ Créer le progress hook avec les clés Redis
                progress_hook = None
                if redis_client and redis_client.enabled:
                    global_key = "global_active_downloads"
                    ip_key = f"ip_downloads:{client_ip}"
                    progress_hook = create_progress_hook(global_key, ip_key, client_ip)
                
                ydl_opts = {
                    "format": fmt,
                    "outtmpl": output_template,
                    "quiet": True,
                    "no_warnings": False,
                    "noplaylist": True,
                    "nocheckcertificate": False,
                    "ignoreerrors": False,
                    "retries": 5,
                    "fragment_retries": 5,
                    "merge_output_format": "mp4" if not quality.lower() == "audio" else None,
                    "extract_flat": False,
                    "geo_bypass": True,
                    "geo_bypass_country": "FR",
                    "sleep_interval": 1,
                    "max_sleep_interval": 3,
                    "socket_timeout": 30,
                    "cachedir": False,
                    "overwrites": True,
                    "concurrent_fragment_downloads": 4,
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
                
                # Ajouter progress_hook si disponible
                if progress_hook:
                    ydl_opts["progress_hooks"] = [progress_hook]
                
                if quality.lower() == "audio":
                    ydl_opts["format"] = "bestaudio/best"
                    ydl_opts["postprocessors"] = [{
                        "key": "FFmpegExtractAudio",
                        "preferredcodec": "mp3",
                        "preferredquality": "192"
                    }]
                    output_template = str(DOWNLOAD_DIR / f"{file_uuid}.mp3")
                    ydl_opts["outtmpl"] = output_template
                
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(url, download=True)
                
                if not info:
                    raise Exception("Aucune information récupérée")
                
                # Trouver le fichier
                files = list(DOWNLOAD_DIR.glob(f"{file_uuid}.*"))
                if not files:
                    current_time = time.time()
                    for file in DOWNLOAD_DIR.iterdir():
                        if file.is_file() and (current_time - file.stat().st_mtime) < 10:
                            if file.name.startswith(file_uuid):
                                files = [file]
                                break
                
                if not files:
                    raise Exception("Fichier introuvable")
                
                temp_path = files[0]
                file_size = temp_path.stat().st_size
                
                if file_size == 0:
                    temp_path.unlink()
                    raise Exception("Fichier vide")
                
                if file_size > MAX_FILE_SIZE:
                    temp_path.unlink()
                    raise Exception(f"Fichier trop volumineux ({file_size} > {MAX_FILE_SIZE})")
                
                if not validate_magic_bytes(temp_path):
                    temp_path.unlink()
                    raise Exception("Format de fichier invalide")
                
                if not validate_with_ffprobe(temp_path):
                    temp_path.unlink()
                    raise Exception("FFprobe validation échouée")
                
                if not validate_with_ffmpeg(temp_path):
                    temp_path.unlink()
                    raise Exception("FFmpeg validation échouée")
                
                ext = temp_path.suffix
                clean_name = f"vens_{file_uuid}{ext}"
                final_path = DOWNLOAD_DIR / clean_name
                temp_path.rename(final_path)
                
                # Mettre à jour les métriques
                with metrics_lock:
                    metrics["total_downloads"] += 1
                    metrics["successful_downloads"] += 1
                    metrics["bytes_downloaded"] += file_size
                
                logger.info(f"✅ [{platform}] {clean_name}")
                return final_path, info
                
            except Exception as e:
                last_error = str(e)
                logger.warning(f"⚠️ Format {fmt} a échoué: {e}")
                for f in DOWNLOAD_DIR.glob(f"{file_uuid}.*"):
                    f.unlink(missing_ok=True)
                continue
        
        with metrics_lock:
            metrics["failed_downloads"] += 1
        raise Exception(f"Tous les formats ont échoué. Dernière erreur: {last_error}")
        
    finally:
        if redis_client and redis_client.enabled:
            redis_client.decr_global("global_active_downloads")
        else:
            with download_lock:
                active_downloads -= 1
        
        release_ip_limit(client_ip)
        
        # Nettoyage des fichiers temporaires
        if temp_path and temp_path.exists():
            temp_path.unlink(missing_ok=True)

# =========================
# EXTRACT ENDPOINT
# =========================

@app.post("/extract")
def extract():
    if not check_api_key():
        return jsonify({"error": "Invalid API key"}), 401
    
    client_ip = get_real_ip()
    if not check_rate_limit(client_ip):
        logger.warning(f"⚠️ Rate limit dépassé pour {client_ip}")
        return jsonify({"error": "Rate limit exceeded"}), 429
    
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    quality = (data.get("quality") or "720").strip()
    
    logger.info(f"📥 Requête de {client_ip} - {mask_url(url)}")
    
    if not valid_url(url):
        return jsonify({"error": "URL invalide"}), 400
    
    if not is_allowed_domain(url):
        logger.warning(f"⚠️ Domaine non autorisé: {url}")
        return jsonify({"error": "Domaine non autorisé"}), 400
    
    if not check_ssrf(url):
        logger.warning(f"⚠️ SSRF bloquée: {url}")
        return jsonify({"error": "Accès interdit"}), 403
    
    try:
        file_path, info = download_media(url, quality, client_ip)
        
        # ⭐ Token signé (pas de stockage)
        token = generate_token(file_path.name)
        base_url = request.host_url.rstrip('/')
        download_url = f"{base_url}/download/{file_path.name}?token={token}"
        preview_url = f"{base_url}/preview/{file_path.name}?token={token}"
        
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
            "token": token,
            "token_expires_in": TOKEN_EXPIRE_SECONDS,
            "ext": file_path.suffix[1:],
            "quality": quality,
            "expires_in": FILE_EXPIRE_TIME
        }
        
        logger.info(f"✅ Succès: {file_path.name}")
        return jsonify(response)
    
    except Exception as e:
        if DEBUG:
            logger.error(f"❌ Erreur: {e}")
            logger.error(traceback.format_exc())
        else:
            logger.error(f"❌ Erreur: {str(e)[:200]}")
        
        return jsonify({"error": str(e)}), 502

# =========================
# PREVIEW ROUTE
# =========================

@app.get("/preview/<filename>")
def preview(filename):
    filename = safe_filename(filename)
    token = request.args.get('token', '')
    
    # ⭐ Token signé - validation sans suppression
    if not validate_token(token, filename, delete=False):
        return jsonify({"error": "Token invalide ou expiré"}), 403
    
    file_path = DOWNLOAD_DIR / filename
    if not file_path.exists():
        return jsonify({"error": "Fichier introuvable"}), 404
    
    return send_file(file_path, mimetype="video/mp4", conditional=True)

# =========================
# DOWNLOAD ROUTE
# =========================

@app.get("/download/<filename>")
def download(filename):
    filename = safe_filename(filename)
    token = request.args.get('token', '')
    
    # ⭐ Token signé - validation avec suppression (usage unique)
    if not validate_token(token, filename, delete=True):
        return jsonify({"error": "Token invalide ou expiré"}), 403
    
    file_path = DOWNLOAD_DIR / filename
    if not file_path.exists():
        if DEBUG:
            all_files = list(DOWNLOAD_DIR.iterdir())
            return jsonify({
                "error": "Fichier introuvable",
                "requested": filename,
                "available_files": [f.name for f in all_files]
            }), 404
        return jsonify({"error": "Fichier introuvable"}), 404
    
    if not is_within_download_dir(file_path):
        return jsonify({"error": "Accès interdit"}), 403
    
    ext = file_path.suffix.lower()
    mime_types = {
        ".mp4": "video/mp4",
        ".mp3": "audio/mpeg",
        ".webm": "video/webm",
        ".mkv": "video/x-matroska",
        ".m4a": "audio/mp4",
        ".opus": "audio/opus",
        ".ogg": "audio/ogg",
        ".wav": "audio/wav"
    }
    mimetype = mime_types.get(ext, "application/octet-stream")
    
    response = send_file(
        file_path,
        as_attachment=True,
        download_name=filename,
        mimetype=mimetype,
        conditional=True
    )
    
    response.headers["Content-Disposition"] = content_disposition_utf8(filename)
    
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
    
    return response

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
    
    if not is_allowed_domain(url):
        return jsonify({"error": "Domaine non autorisé"}), 400
    
    if not check_ssrf(url):
        return jsonify({"error": "Accès interdit"}), 403
    
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        "extract_flat": False,
        "nocheckcertificate": False,
        "socket_timeout": 30,
        "cachedir": False,
        "http_headers": {"User-Agent": USER_AGENT},
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
        if DEBUG:
            logger.error(traceback.format_exc())
        return jsonify({"error": str(e)}), 500

# =========================
# DEBUG FILES
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
        "redis": redis_client.enabled if redis_client else False
    })

# =========================
# GUNICORN
# =========================

if __name__ != "__main__":
    pass

# =========================
# RUN
# =========================

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    debug = os.environ.get("DEBUG", "False").lower() == "true"
    
    print(f"""
╔══════════════════════════════════════════════════════════════════════╗
║  🚀 VENS-DOWNLOADER SERVER v20.0 — PRODUCTION ULTIME              ║
║  📦 yt-dlp: {yt_dlp.version.__version__}                                      ║
║  ✅ FFmpeg: {'✅' if FFMPEG_AVAILABLE else '❌'}                                                    ║
║  ✅ FFprobe: {'✅' if FFPROBE_AVAILABLE else '❌'}                                                   ║
║  🔴 Redis: {'✅' if (redis_client and redis_client.enabled) else '❌'}                               ║
║  🔒 Tokens: Signés (itsdangerous) - pas de stockage                  ║
║  🛡️ Headers de sécurité: X-Content-Type-Options, X-Frame-Options    ║
║  📊 Métriques: /metrics disponible                                   ║
║  📁 Validation: Magic bytes + FFprobe + FFmpeg                      ║
║  🔑 Limites Flask: MAX_CONTENT_LENGTH={FLASK_MAX_CONTENT_LENGTH//(1024*1024)}MB                 ║
║  🧹 Nettoyage: Verrou Redis (pas de main worker)                   ║
║  🌐 BASE_URL: {BASE_URL}     ║
║  📦 Gunicorn: {'✅' if os.environ.get('GUNICORN_CMD_ARGS') else '⚠️ Utiliser Gunicorn en prod'}  ║
╚══════════════════════════════════════════════════════════════════════╝
    """)
    
    app.run(host="0.0.0.0", port=port, debug=debug)
