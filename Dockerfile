FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg curl ca-certificates && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py .

ENV PORT=8000
EXPOSE 8000

CMD ["gunicorn", "-w", "2", "-k", "gthread", "--threads", "8", "-t", "120", "-b", "0.0.0.0:8000", "server:app"]
