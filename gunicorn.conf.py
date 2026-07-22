# gunicorn.conf.py - VENS-DOWNLOADER v20.0
import os

# ⭐ Configuration dynamique (modifiable via variables d'environnement)
bind = f"0.0.0.0:{os.environ.get('PORT', '8000')}"

workers = int(os.environ.get("WEB_CONCURRENCY", "2"))
threads = int(os.environ.get("THREADS", "4"))

worker_class = "gthread"

timeout = 120
graceful_timeout = 30
keepalive = 5

preload_app = False

max_requests = 1000
max_requests_jitter = 100

# ⭐ Logs vers stdout (pour Render)
accesslog = "-"
errorlog = "-"
loglevel = "info"

def post_fork(server, worker):
    """Hook appelé après le fork de chaque worker"""
    os.environ["WORKER_ID"] = str(worker.pid)
    os.environ["GUNICORN_WORKER"] = "true"
    print(f"🔧 Worker {worker.pid} démarré")

def worker_int(worker):
    """Hook appelé lors de l'arrêt du worker"""
    print(f"🧹 Worker {worker.pid} arrêté")

def worker_abort(worker):
    """Hook appelé lors de l'abandon du worker"""
    print(f"⚠️ Worker {worker.pid} abandonné")
