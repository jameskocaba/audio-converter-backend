import gevent.monkey
gevent.monkey.patch_all()  # MUST BE AT THE VERY TOP

import os
import uuid
import logging
import glob
import shutil
import certifi
import zipfile
from flask import Flask, request, send_file, jsonify, after_this_request
from flask_cors import CORS
from flask_socketio import SocketIO
from yt_dlp import YoutubeDL

# SSL Configuration
os.environ['SSL_CERT_FILE'] = certifi.where()

logging.basicConfig(level=logging.INFO, format='[%(asctime)s] %(levelname)s: %(message)s')
logger = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}})
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='gevent')

DOWNLOAD_FOLDER = os.path.join(os.getcwd(), 'downloads')
os.makedirs(DOWNLOAD_FOLDER, exist_ok=True)
MAX_SONGS = 15

def progress_hook(d):
    if d['status'] == 'downloading':
        p = d.get('_percent_str', '0%').replace('%', '').strip()
        try:
            socketio.emit('download_progress', {'percentage': float(p)})
        except: pass

@app.route('/convert', methods=['POST'])
def convert_audio():
    data = request.json
    url = data.get('url', '').strip()
    session_id = str(uuid.uuid4())
    session_dir = os.path.join(DOWNLOAD_FOLDER, session_id)
    os.makedirs(session_dir, exist_ok=True)
    
    # FFmpeg Discovery
    ffmpeg_exe = os.path.join(os.getcwd(), 'ffmpeg_bin/ffmpeg')
    for root, dirs, files in os.walk(os.path.join(os.getcwd(), 'ffmpeg_bin')):
        if 'ffmpeg' in files:
            ffmpeg_exe = os.path.join(root, 'ffmpeg')
            os.chmod(ffmpeg_exe, 0o755)
            break

    ydl_opts = {
        'format': 'bestaudio/best',
        'postprocessors': [{'key': 'FFmpegExtractAudio','preferredcodec': 'mp3','preferredquality': '0'}],
        'progress_hooks': [progress_hook],
        'outtmpl': os.path.join(session_dir, '%(title)s.%(ext)s'),
        'noplaylist': False,
        'playlist_items': f'1-{MAX_SONGS}',
        'ffmpeg_location': ffmpeg_exe,
        'ignoreerrors': True,
        'cookiefile': 'cookies.txt',
        'geo_bypass': True,
        'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    }

    try:
        with YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])

        mp3_files = glob.glob(os.path.join(session_dir, "*.mp3"))
        if not mp3_files:
            return jsonify({"status": "error", "message": "No tracks found."}), 400

        tracks = [{"name": os.path.basename(f), "downloadLink": f"/download/{session_id}/{os.path.basename(f)}"} for f in mp3_files]

        zip_link = None
        if len(mp3_files) > 1:
            zip_name = "playlist_all.zip"
            zip_path = os.path.join(session_dir, zip_name)
            with zipfile.ZipFile(zip_path, 'w') as zipf:
                for f in mp3_files:
                    zipf.write(f, os.path.basename(f))
            zip_link = f"/download/{session_id}/{zip_name}"

        return jsonify({"status": "success", "tracks": tracks, "zipLink": zip_link})
    except Exception as e:
        logger.exception("Conversion error")
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/download/<session_id>/<filename>')
def download_file(session_id, filename):
    file_path = os.path.join(DOWNLOAD_FOLDER, session_id, filename)
    if os.path.exists(file_path):
        return send_file(file_path, as_attachment=True)
    return "File not found.", 404

if __name__ == '__main__':
    socketio.run(app, host='0.0.0.0', port=int(os.environ.get("PORT", 5000)))