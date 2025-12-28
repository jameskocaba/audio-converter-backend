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

# CRITICAL: Tell Python to use certifi's certificate bundle
os.environ['SSL_CERT_FILE'] = certifi.where()

logging.basicConfig(level=logging.INFO, format='[%(asctime)s] %(levelname)s: %(message)s')
logger = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app)
# Initialize SocketIO for the progress bar
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='gevent')

DOWNLOAD_FOLDER = os.path.join(os.getcwd(), 'downloads')
os.makedirs(DOWNLOAD_FOLDER, exist_ok=True)

# Progress hook for the WebSocket progress bar
def progress_hook(d):
    if d['status'] == 'downloading':
        # Clean the percentage string (e.g., ' 45.2%' -> 45.2)
        p = d.get('_percent_str', '0%').replace('%', '').strip()
        try:
            percent = float(p)
            socketio.emit('download_progress', {'percentage': percent})
        except:
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

    # Robust FFmpeg discovery
    ffmpeg_base = os.path.join(os.getcwd(), 'ffmpeg_bin')
    ffmpeg_final_path = ffmpeg_base
    for root, dirs, files in os.walk(ffmpeg_base):
        if 'ffmpeg' in files:
            ffmpeg_final_path = root
            break

    ydl_opts = {
        # --- QUALITY SETTINGS ---
        'format': 'bestaudio/best',
        'postprocessors': [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '0', # '0' is best VBR quality
        }],
        'postprocessor_args': ['-q:a', '0'],
        
        # --- PROGRESS BAR HOOK ---
        'progress_hooks': [progress_hook],
        
        # --- STABILITY & PATHS ---
        'noplaylist': True,
        'ignoreerrors': True,
        'logger': error_logger,
        'outtmpl': output_template,
        'nocheckcertificate': True,
        'cookiefile': 'cookies.txt',
        'ffmpeg_location': ffmpeg_final_path,
        'socket_timeout': 60,
        'retries': 10,
        'fragment_retries': 10,
        'extract_flat': False,
        'youtube_include_dash_manifest': False,
        'proxy': os.environ.get("PROXY_URL"),
    }

    try:
        with YoutubeDL(ydl_opts) as ydl:
            is_search = not any(x in url for x in ["soundcloud.com", "youtube.com", "bandcamp.com", "youtu.be"])
            query = f"scsearch1:{url}" if is_search else url
            ydl.download([query])

        mp3_files = glob.glob(os.path.join(session_dir, "*.mp3"))
        if mp3_files:
            relative_path = f"{session_id}/{os.path.basename(mp3_files[0])}"
            return jsonify({"status": "success", "downloadLink": f"/download/{relative_path}"})
        else:
            error_msg = "Track skipped."
            if error_logger.last_error:
                if "404" in error_logger.last_error: error_msg = "Track not found (404)."
                else: error_msg = f"Skipped: {error_logger.last_error}"
            return jsonify({"status": "skipped", "message": error_msg}), 200

    except Exception as e:
        logger.exception