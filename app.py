import gevent.monkey
gevent.monkey.patch_all()  # Crucial for WebSockets on Render

import os
import uuid
import logging
import glob
import shutil
import certifi
from flask import Flask, request, send_file, jsonify, after_this_request
from flask_cors import CORS
from flask_socketio import SocketIO, emit
from yt_dlp import YoutubeDL

# --- CRITICAL CONFIGURATION ---
os.environ['SSL_CERT_FILE'] = certifi.where()

logging.basicConfig(level=logging.INFO, format='[%(asctime)s] %(levelname)s: %(message)s')
logger = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='gevent')

DOWNLOAD_FOLDER = os.path.join(os.getcwd(), 'downloads')
os.makedirs(DOWNLOAD_FOLDER, exist_ok=True)

def progress_hook(d):
    if d['status'] == 'downloading':
        p = d.get('_percent_str', '0%').replace('%', '').strip()
        try:
            percent = float(p)
            socketio.emit('download_progress', {'percentage': percent})
        except Exception:
            pass

class MyLogger:
    def __init__(self):
        self.last_error = None
    def debug(self, msg): pass
    def warning(self, msg): pass
    def error(self, msg):
        self.last_error = msg
        logger.error(f"yt-dlp Error: {msg}")

@app.route('/convert', methods=['POST'])
def convert_audio():
    data = request.json
    url = data.get('url', '').strip()
    session_id = str(uuid.uuid4())
    session_dir = os.path.join(DOWNLOAD_FOLDER, session_id)
    os.makedirs(session_dir, exist_ok=True)
    
    error_logger = MyLogger()
    output_template = os.path.join(session_dir, 'audio.%(ext)s')

    ffmpeg_base = os.path.join(os.getcwd(), 'ffmpeg_bin')
    ffmpeg_final_path = ffmpeg_base
    for root, dirs, files in os.walk(ffmpeg_base):
        if 'ffmpeg' in files:
            ffmpeg_final_path = root
            break

    ydl_opts = {
        'format': 'bestaudio/best',
        'postprocessors': [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '0',
        }],
        'postprocessor_args': ['-q:a', '0'],
        'progress_hooks': [progress_hook],
        'logger': error_logger,
        'outtmpl': output_template,
        'noplaylist': True,
        'nocheckcertificate': True,
        'ffmpeg_location': ffmpeg_final_path,
        'cookiefile': 'cookies.txt',
        'socket_timeout': 60,
        'retries': 10,
        
        # --- GEO-BYPASS & BROWSER SPOOFING ---
        'geo_bypass': True,
        'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'referer': 'https://www.google.com/',
    }

    try:
        with YoutubeDL(ydl_opts) as ydl:
            is_search = not any(x in url for x in ["soundcloud.com", "youtube.com", "bandcamp.com", "youtu.be"])
            query = f"scsearch1:{url}" if is_search else url
            
            logger.info(f"Attempting download: {query}")
            
            # Use internal try-except to handle Geo-Restriction specifically
            try:
                ydl.download([query])
            except Exception as e:
                err_str = str(e).lower()
                if "geo restriction" in err_str or "not available" in err_str:
                    logger.warning("Geo-restricted on SoundCloud. Switching to YouTube search fallback...")
                    # Fallback to YouTube search using the original URL or search term
                    fallback_query = f"ytsearch1:{url}"
                    ydl.download([fallback_query])
                else:
                    raise e

        mp3_files = glob.glob(os.path.join(session_dir, "*.mp3"))
        if mp3_files:
            relative_path = f"{session_id}/{os.path.basename(mp3_files[0])}"
            return jsonify({"status": "success", "downloadLink": f"/download/{relative_path}"})
        else:
            return jsonify({"status": "skipped", "message": "Track unavailable or restricted."}), 200

    except Exception as e:
        logger.exception("Conversion error")
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/download/<session_id>/<filename>', methods=['GET'])
def download_file(session_id, filename):
    file_path = os.path.join(DOWNLOAD_FOLDER, session_id, filename)
    if os.path.exists(file_path):
        @after_this_request
        def cleanup(response):
            shutil.rmtree(os.path.dirname(file_path), ignore_errors=True)
            return response
        return send_file(file_path, as_attachment=True)
    return "File not found.", 404

if __name__ == '__main__':
    socketio.run(app, host='0.0.0.0', port=int(os.environ.get("PORT", 5000)))