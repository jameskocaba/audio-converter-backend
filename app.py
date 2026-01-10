import gevent.monkey
gevent.monkey.patch_all()

import os, uuid, logging, glob, zipfile, certifi, shutil, time
from flask import Flask, request, send_file, jsonify
from flask_cors import CORS
from yt_dlp import YoutubeDL
import gevent
from gevent.lock import Semaphore

# SSL & Logging
os.environ['SSL_CERT_FILE'] = certifi.where()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}})

DOWNLOAD_FOLDER = os.path.join(os.getcwd(), 'downloads')
os.makedirs(DOWNLOAD_FOLDER, exist_ok=True)

# 512MB RAM CONFIG
MAX_SONGS = 500
BATCH_SIZE = 10 
memory_guard = Semaphore(1)
task_status = {} # Global dictionary to track progress

def run_conversion_task(url, session_id, session_dir):
    with memory_guard:
        try:
            ydl_opts_base = {
                'format': 'bestaudio/best',
                'writethumbnail': True,
                'postprocessors': [
                    {'key': 'FFmpegExtractAudio', 'preferredcodec': 'mp3', 'preferredquality': '0'},
                    {'key': 'FFmpegThumbnailsConvertor', 'format': 'jpg'},
                    {'key': 'EmbedThumbnail'},
                    {'key': 'FFmpegMetadata', 'add_metadata': True}
                ],
                'outtmpl': os.path.join(session_dir, '%(title)s.%(ext)s'),
                'ignoreerrors': True,
                'ffmpeg_location': 'ffmpeg' 
            }

            # 1. Get total tracks (Flat extract to save RAM)
            with YoutubeDL({'quiet': True, 'extract_flat': True}) as ydl:
                info = ydl.extract_info(url, download=False)
                entries = info.get('entries', [])
                total_tracks = min(len(entries), MAX_SONGS) if entries else 1

            # 2. Download in small batches
            for i in range(0, total_tracks, BATCH_SIZE):
                # Check for cancellation
                if task_status.get(session_id, {}).get('status') == "cancelled":
                    return

                batch_opts = ydl_opts_base.copy()
                batch_opts['playlist_items'] = f"{i+1}-{min(i+BATCH_SIZE, total_tracks)}"
                
                with YoutubeDL(batch_opts) as ydl:
                    ydl.download([url])
                
                # Update progress count for polling
                current_mp3s = glob.glob(os.path.join(session_dir, "*.mp3"))
                task_status[session_id]["count"] = len(current_mp3s)

            # 3. Zip files using disk-write method (RAM safe)
            mp3_files = glob.glob(os.path.join(session_dir, "*.mp3"))
            zip_link = None
            if len(mp3_files) > 0:
                zip_name = f"bundle_{session_id[:5]}.zip"
                zip_path = os.path.join(session_dir, zip_name)
                with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as z:
                    for f in mp3_files:
                        z.write(f, os.path.basename(f))
                zip_link = f"/download/{session_id}/{zip_name}"

            # 4. Success State
            task_status[session_id] = {
                "status": "completed",
                "zipLink": zip_link,
                "tracks": [{"name": os.path.basename(f), "downloadLink": f"/download/{session_id}/{os.path.basename(f)}"} for f in mp3_files if not f.endswith('.zip')]
            }

        except Exception as e:
            logger.error(f"Task Error: {e}")
            task_status[session_id] = {"status": "error", "message": str(e)}

@app.route('/convert', methods=['POST'])
def convert_audio():
    data = request.json
    url = data.get('url', '').strip()
    session_id = str(uuid.uuid4())
    session_dir = os.path.join(DOWNLOAD_FOLDER, session_id)
    os.makedirs(session_dir, exist_ok=True)

    # Initialize status BEFORE starting background thread
    task_status[session_id] = {"status": "processing", "count": 0}
    
    gevent.spawn(run_conversion_task, url, session_id, session_dir)
    return jsonify({"status": "started", "session_id": session_id}), 202

@app.route('/status/<session_id>', methods=['GET'])
def get_status(session_id):
    # Returns the current progress
    return jsonify(task_status.get(session_id, {"status": "not_found"}))

@app.route('/cancel', methods=['POST'])
def cancel():
    session_id = request.json.get('session_id')
    if session_id in task_status:
        task_status[session_id]["status"] = "cancelled"
    return jsonify({"status": "ok"})

@app.route('/download/<session_id>/<filename>')
def download_file(session_id, filename):
    return send_file(os.path.join(DOWNLOAD_FOLDER, session_id, filename), as_attachment=True)

if __name__ == '__main__':
    from gevent.pywsgi import WSGIServer
    WSGIServer(('0.0.0.0', 5000), app).serve_forever()