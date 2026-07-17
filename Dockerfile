FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# AJOUTEZ httpx ici !
RUN pip install --no-cache-dir "yt-dlp[default]" fastapi "uvicorn[standard]" pydantic httpx

COPY server.py /app/server.py

ENV PORT=8080
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD curl -fsS "http://127.0.0.1:${PORT}/health" || exit 1

CMD ["sh", "-c", "uvicorn server:app --host 0.0.0.0 --port ${PORT}"]
