"""
VENS-DOWNLOADER — yt-dlp download server.
Deploy to Render / Fly / Railway.

Environment:
API_KEY = your secret key

Endpoints:
GET  /                 -> health check
POST /extract          -> download media
GET  /download/<file>  -> serve downloaded file

Header required:
X-API-Key: <API_KEY>
"""

import os
import uuid
import shutil
from flask import Flask, request, jsonify, send_file, after_this_request
from flask_cors import CORS
import yt_dlp


app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}})


API_KEY = os.environ.get("API_KEY", "").strip()

DOWNLOAD_FOLDER = "/tmp/vens_downloads"

os.makedirs(DOWNLOAD_FOLDER, exist_ok=True)


def check_api_key():
    if API_KEY:
        supplied = request.headers.get("X-API-Key", "").strip()
        return supplied == API_KEY
    return True


def pick_format(quality):
    q = (quality or "720").lower()

    if q == "audio":
        return "bestaudio/best"

    # Maximum conseillé pour Render
    return (
        "bestvideo[height<=720]"
        "+bestaudio/best[height<=720]/"
        "best[height<=720]"
    )


@app.get("/")
def health():
    return jsonify({
        "status": "ok",
        "service": "vens-ytdlp",
        "mode": "server-download",
        "auth_required": bool(API_KEY)
    })


@app.post("/extract")
def extract():

    if not check_api_key():
        return jsonify({
            "detail": "Invalid or missing API key"
        }), 401


    data = request.get_json(silent=True) or {}

    url = (data.get("url") or "").strip()
    quality = (data.get("quality") or "720").strip()


    if not url:
        return jsonify({
            "detail": "Missing url"
        }), 400


    file_id = str(uuid.uuid4())

    output_template = os.path.join(
        DOWNLOAD_FOLDER,
        file_id + ".%(ext)s"
    )


    is_audio = quality.lower() == "audio"


    ydl_opts = {
        "format": pick_format(quality),

        "outtmpl": output_template,

        "quiet": True,
        "no_warnings": True,

        "noplaylist": True,

        "nocheckcertificate": True,

        "retries": 5,
        "fragment_retries": 5,

        "merge_output_format": "mp4",

        "postprocessors": []
    }


    if is_audio:
        ydl_opts["postprocessors"] = [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192"
            }
        ]


    try:

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:

            info = ydl.extract_info(
                url,
                download=True
            )


        filename = None

        for file in os.listdir(DOWNLOAD_FOLDER):
            if file.startswith(file_id):
                filename = file
                break


        if not filename:
            return jsonify({
                "detail": "Download failed"
            }), 500


        return jsonify({

            "status": "success",

            "title": info.get("title"),

            "thumbnail": info.get("thumbnail"),

            "duration": info.get("duration"),

            "download_url":
                f"/download/{filename}",

            "filename": filename,

            "extractor":
                info.get("extractor")

        })


    except Exception as e:

        return jsonify({

            "detail": f"Download failed: {str(e)}"

        }), 502



@app.get("/download/<filename>")
def download(filename):

    path = os.path.join(
        DOWNLOAD_FOLDER,
        filename
    )


    if not os.path.exists(path):
        return jsonify({
            "detail": "File expired or missing"
        }), 404


    @after_this_request
    def remove_file(response):

        try:
            os.remove(path)

        except Exception:
            pass

        return response


    return send_file(
        path,
        as_attachment=True
    )



if __name__ == "__main__":

    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 8000))
      )
