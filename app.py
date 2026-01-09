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

# 512MB RAM LIMIT CONFIGURATION
MAX_SONGS = 500
BATCH_SIZE = 20  # Smaller batches are safer for low memory
CLEANUP_INTERVAL = 3600 
memory_guard = Semaphore(1) # Only allows 1 user at a time to prevent RAM crash

active_tasks = {}

def progress_hook(d, session_id):
    """Checks if the user clicked 'Cancel' during the download."""
    if session_id in active_tasks and active_tasks[session_id] is True:
        raise Exception("USER_CANCELLED")

def cleanup_old_files():
    """Removes files older than 1 hour from the server."""
    while True:
        gevent.sleep(600)
        now = time.time()
        for session_id in os.listdir(DOWNLOAD_FOLDER):
            session_path = os.path.join(DOWNLOAD_FOLDER, session_id)
            if os.path.isdir(session_path):
                if os.path.getmtime(session_path) < (now - CLEANUP_INTERVAL):
                    shutil.rmtree(session_path, ignore_errors=True)
                    logger.info(f"Cleaned up expired session: {session_id}")

# Start the background cleanup task
gevent.spawn(cleanup_old_files)

@app.route('/cancel', methods=['POST'])
def cancel_conversion():
    data = request.json
    session_id = data.get('session_id')
    if session_id and session_id in active_tasks:
        active_tasks[session_id] = True  
        return jsonify({"status": "cancelling"}), 200
    return jsonify({"status": "not_found"}), 404

@app.route('/convert', methods=['POST'])
def convert_audio():
    with memory_guard: # Prevents multiple simultaneous 500-song conversions from crashing RAM
        data = request.json
        url = data.get('url', '').strip()
        # Use session_id from frontend if provided, otherwise generate
        session_id = data.get('session_id') or str(uuid.uuid4())
        
        active_tasks[session_id] = False 
        session_dir = os.path.join(DOWNLOAD_FOLDER, session_id)
        os.makedirs(session_dir, exist_ok=True)

        # Basic YDL Options
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
            # 1. Get Playlist Info
            with YoutubeDL({'quiet': True}) as ydl:
                info = ydl.extract_info(url, download=False)
                if 'entries' in info:
                    all_entries = [e for e in info['entries'] if e]
                    total_tracks = min(len(all_entries), MAX_SONGS)
                    expected_titles = [e['title'] for e in all_entries[:total_tracks]]
                else:
                    total_tracks = 1
                    expected_titles = [info.get('title', 'Unknown Track')]

            # 2. Batch Download
            for i in range(0, total_tracks, BATCH_SIZE):
                if active_tasks.get(session_id) is True:
                    raise Exception("USER_CANCELLED")
                
                batch_opts = ydl_opts_base.copy()
                batch_opts['playlist_items'] = f"{i+1}-{min(i+BATCH_SIZE, total_tracks)}"
                
                with YoutubeDL(batch_opts) as ydl:
                    ydl.download([url])

            # 3. Finalize and Zip
            mp3_files = glob.glob(os.path.join(session_dir, "*.mp3"))
            
            zip_link = None
            if len(mp3_files) > 1:
                zip_name = f"soundcloud_bundle_{session_id[:5]}.zip"
                with zipfile.ZipFile(os.path.join(session_dir, zip_name), 'w', zipfile.ZIP_DEFLATED) as z:
                    for f in mp3_files:
                        z.write(f, os.path.basename(f))
                zip_link = f"/download/{session_id}/{zip_name}"

            # Prepare tracks list to match your script.js 't.downloadLink'
            tracks = [{"name": os.path.basename(f), "downloadLink": f"/download/{session_id}/{os.path.basename(f)}"} for f in mp3_files]

            return jsonify({
                "status": "success",
                "tracks": tracks,
                "zipLink": zip_link,
                "session_id": session_id
            })

        except Exception as e:
            if str(e) == "USER_CANCELLED":
                shutil.rmtree(session_dir, ignore_errors=True)
                return jsonify({"status": "cancelled"}), 200
            logger.error(f"Conversion Error: {e}")
            return jsonify({"status": "error", "message": str(e)}), 500
        finally:
            active_tasks.pop(session_id, None)

@app.route('/download/<session_id>/<filename>')
def download_file(session_id, filename):
    file_path = os.path.join(DOWNLOAD_FOLDER, session_id, filename)
    if os.path.exists(file_path):
        return send_file(file_path, as_attachment=True)
    return "File not found", 404

if __name__ == '__main__':
    from gevent.pywsgi import WSGIServer
    print("Server running on port 5000...")
    http_server = WSGIServer(('0.0.0.0', 5000), app)
    http_server.serve_forever()