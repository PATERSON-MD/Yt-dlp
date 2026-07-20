FROM python:3.11-slim

# Installer FFmpeg (obligatoire pour la fusion audio/vidéo)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Vérifier FFmpeg
RUN ffmpeg -version

# Créer un utilisateur non-root pour la sécurité
RUN useradd -m -u 1000 vens && \
    mkdir -p /app && \
    chown -R vens:vens /app

WORKDIR /app

# Copier et installer les dépendances Python
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copier le code
COPY server.py .

# Changer les permissions
RUN chown -R vens:vens /app

# Passer à l'utilisateur non-root
USER vens

# Variables d'environnement (API_KEY à définir sur Render)
ENV PORT=8000
ENV DOWNLOAD_DIR=/tmp/vens_downloads
ENV MAX_FILE_SIZE=500
ENV FILE_EXPIRE_TIME=300
ENV MAX_CONCURRENT_DOWNLOADS=2
ENV DELETE_AFTER_DOWNLOAD=true

EXPOSE 8000

# Lancer avec Gunicorn optimisé
CMD ["gunicorn", "-w", "2", "-k", "gthread", "--threads", "4", "-t", "120", "-b", "0.0.0.0:8000", "server:app"]
