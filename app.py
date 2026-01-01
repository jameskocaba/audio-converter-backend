import gevent.monkey
gevent.monkey.patch_all()

import os, uuid, logging, glob, zipfile, certifi, shutil
from flask import Flask, request, send_file, jsonify
from flask_cors import CORS
from yt_dlp import YoutubeDL

# SSL & Logging
os.environ['SSL_CERT_FILE'] = certifi.where()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}})

DOWNLOAD_FOLDER = os.path.join(os.getcwd(), 'downloads')
os.makedirs(DOWNLOAD_FOLDER, exist_ok=True)
MAX_SONGS = 15

# Global tracker for active downloads
# Format: { session_id: bool_is_cancelled }
active_tasks = {}

def progress_hook(d, session_id):
    """Checks if the task was cancelled during download."""
    if session_id in active_tasks and active_tasks[session_id]:
        raise Exception("USER_CANCELLED")

@app.route('/cancel', methods=['POST'])
def cancel_conversion():
    data = request.json
    session_id = data.get('session_id')
    if session_id and session_id in active_tasks:
        active_tasks[session_id] = True  # Signal the hook to stop
        logger.info(f"Cancellation requested for session: {session_id}")
        return jsonify({"status": "cancelling"}), 200
    return jsonify({"status": "not_found"}), 404

@app.route('/convert', methods=['POST'])
def convert_audio():
    data = request.json
    url = data.get('url', '').strip()
    
    if not url or "soundcloud.com" not in url.lower():
        return jsonify({
            "status": "error", 
            "message": "Invalid link. This tool only supports SoundCloud shareable links."
        }), 400

    session_id = str(uuid.uuid4())
    active_tasks[session_id] = False # Initialize task as active
    
    session_dir = os.path.join(DOWNLOAD_FOLDER, session_id)
    os.makedirs(session_dir, exist_ok=True)
    
    # ... (FFmpeg path logic remains same) ...
    ffmpeg_exe = os.path.join(os.getcwd(), 'ffmpeg_bin/ffmpeg')
    for root, dirs, files in os.walk(os.path.join(os.getcwd(), 'ffmpeg_bin')):
        if 'ffmpeg' in files:
            ffmpeg_exe = os.path.join(root, 'ffmpeg')
            os.chmod(ffmpeg_exe, 0o755)
            break

    ydl_opts = {
        'format': 'bestaudio/best',
        'postprocessors': [{'key': 'FFmpegExtractAudio','preferredcodec': 'mp3','preferredquality': '0'}],
        'outtmpl': os.path.join(session_dir, '%(title)s.%(ext)s'),
        'noplaylist': False,
        'playlist_items': f'1-{MAX_SONGS}',
        'ffmpeg_location': ffmpeg_exe,
        'ignoreerrors': True, 
        'cookiefile': 'cookies.txt' if os.path.exists('cookies.txt') else None,
        # THE FIX: Add progress hook to monitor cancellation
        'progress_hooks': [lambda d: progress_hook(d, session_id)],
    }

    try:
        expected_titles = []
        with YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            if 'entries' in info:
                expected_titles = [e['title'] for e in info['entries'] if e]
            else:
                expected_titles = [info.get('title', 'Unknown Track')]

            ydl.download([url])

        # Check if we exited because of cancellation
        if active_tasks.get(session_id) is True:
            raise Exception("USER_CANCELLED")

        # ... (Rest of your processing logic: glob files, zip link, etc.) ...
        mp3_files = glob.glob(os.path.join(session_dir, "*.mp3"))
        downloaded_names = [os.path.basename(f).lower() for f in mp3_files]
        
        skipped = []
        for title in expected_titles:
            match_found = any(title[:15].lower() in d_name for d_name in downloaded_names)
            if not match_found:
                skipped.append(title)

        tracks = [{"name": n, "downloadLink": f"/download/{session_id}/{n}"} for n in [os.path.basename(f) for f in mp3_files]]

        zip_link = None
        if len(mp3_files) > 1:
            zip_name = "soundcloud_bundle.zip"
            with zipfile.ZipFile(os.path.join(session_dir, zip_name), 'w') as z:
                for f in mp3_files:
                    z.write(f, os.path.basename(f))
            zip_link = f"/download/{session_id}/{zip_name}"

        return jsonify({"status": "success", "tracks": tracks, "zipLink": zip_link, "skipped": skipped, "session_id": session_id})

    except Exception as e:
        if str(e) == "USER_CANCELLED":
            logger.info(f"Cleanup session {session_id} after cancellation.")
            shutil.rmtree(session_dir, ignore_errors=True)
            return jsonify({"status": "cancelled", "message": "Conversion stopped by user."}), 200
        
        logger.exception("Conversion error")
        return jsonify({"status": "error", "message": str(e)}), 500
    
    finally:
        # Remove from active tasks tracker
        active_tasks.pop(session_id, None)

# ... (rest of your download_file and __main__ remains the same) ...