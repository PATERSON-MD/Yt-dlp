FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN ffmpeg -version

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py .

ENV PORT=8000
ENV DOWNLOAD_DIR=/tmp/vens_downloads
ENV MAX_FILE_SIZE=500
ENV FILE_EXPIRE_TIME=300
ENV MAX_CONCURRENT_DOWNLOADS=2
ENV DELETE_AFTER_DOWNLOAD=true

EXPOSE 8000

CMD ["gunicorn", "-w", "2", "-k", "gthread", "--threads", "4", "-t", "120", "-b", "0.0.0.0:8000", "server:app"]
