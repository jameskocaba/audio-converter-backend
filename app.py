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
CORS(app)

DOWNLOAD_FOLDER = os.path.join(os.getcwd(), 'downloads')
os.makedirs(DOWNLOAD_FOLDER, exist_ok=True)

# 512MB RAM OPTIMIZATION
MAX_SONGS = 500
BATCH_SIZE = 25  # Smaller batches are safer for 512MB
CLEANUP_INTERVAL = 3600 
# Only allow 1 conversion at a time to stay under 512MB RAM
memory_guard = Semaphore(1)

active_tasks = {}

def cleanup_old_files():
    while True:
        gevent.sleep(600)
        now = time.time()
        for session_id in os.listdir(DOWNLOAD_FOLDER):
            path = os.path.join(DOWNLOAD_FOLDER, session_id)
            if os.path.isdir(path) and os.path.getmtime(path) < (now - CLEANUP_INTERVAL):
                shutil.rmtree(path, ignore_errors=True)

gevent.spawn(cleanup_old_files)

@app.route('/convert', methods=['POST'])
def convert_audio():
    # Use the semaphore to queue requests
    with memory_guard:
        data = request.json
        url = data.get('url', '').strip()
        session_id = str(uuid.uuid4())
        active_tasks[session_id] = False 
        session_dir = os.path.join(DOWNLOAD_FOLDER, session_id)
        os.makedirs(session_dir, exist_ok=True)

        # FFmpeg Detection
        ffmpeg_exe = 'ffmpeg' # Assumes it's in system PATH
        
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
            'progress_hooks': [lambda d: progress_hook(d, session_id)],
        }

        try:
            # Step 1: Extract Metadata (RAM Efficient)
            with YoutubeDL({'quiet': True}) as ydl:
                info = ydl.extract_info(url, download=False)
                entries = info.get('entries', [info])
                total_to_download = min(len(entries), MAX_SONGS)

            # Step 2: Download in Batches
            for i in range(0, total_to_download, BATCH_SIZE):
                if active_tasks.get(session_id): raise Exception("USER_CANCELLED")
                
                batch_opts = ydl_opts_base.copy()
                batch_opts['playlist_items'] = f"{i+1}-{min(i+BATCH_SIZE, total_to_download)}"
                
                with YoutubeDL(batch_opts) as ydl:
                    ydl.download([url])

            # Step 3: Create Final Zip
            mp3_files = glob.glob(os.path.join(session_dir, "*.mp3"))
            zip_name = f"soundcloud_export_{session_id[:5]}.zip"
            zip_path = os.path.join(session_dir, zip_name)
            
            with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as z:
                for f in mp3_files:
                    z.write(f, os.path.basename(f))

            # Match your frontend keys: "tracks" and "zipLink"
            return jsonify({
                "status": "success",
                "session_id": session_id,
                "zipLink": f"/download/{session_id}/{zip_name}",
                "tracks": [{"name": os.path.basename(f), "url": f"/download/{session_id}/{os.path.basename(f)}"} for f in mp3_files]
            })

        except Exception as e:
            logger.error(f"Error: {e}")
            return jsonify({"status": "error", "message": str(e)}), 500
        finally:
            active_tasks.pop(session_id, None)

@app.route('/download/<session_id>/<filename>')
def download(session_id, filename):
    return send_file(os.path.join(DOWNLOAD_FOLDER, session_id, filename), as_attachment=True)