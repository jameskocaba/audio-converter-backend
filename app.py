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
        active_tasks[session_id] = True  
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
            "message": "Invalid link. This tool only supports SoundCloud links."
        }), 400

    session_id = str(uuid.uuid4())
    active_tasks[session_id] = False 
    
    session_dir = os.path.join(DOWNLOAD_FOLDER, session_id)
    os.makedirs(session_dir, exist_ok=True)
    
    # FFmpeg path logic
    ffmpeg_exe = os.path.join(os.getcwd(), 'ffmpeg_bin/ffmpeg')
    for root, dirs, files in os.walk(os.path.join(os.getcwd(), 'ffmpeg_bin')):
        if 'ffmpeg' in files:
            ffmpeg_exe = os.path.join(root, 'ffmpeg')
            os.chmod(ffmpeg_exe, 0o755)
            break

    # THE COMPLETE YDL_OPTS WITH ART EMBEDDING
    ydl_opts = {
        'format': 'bestaudio/best',
        'writethumbnail': True,  # Step 1: Download the image
        'postprocessors': [
            {
                # Step 2: Extract audio to MP3
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': '0',
            },
            {
                # Step 3: Convert the downloaded image to JPG (Crucial for MP3 compatibility)
                'key': 'FFmpegThumbnailsConvertor',
                'format': 'jpg',
            },
            {
                # Step 4: Embed the JPG into the MP3 tags
                'key': 'EmbedThumbnail',
            },
            {
                # Step 5: Add Metadata (Artist/Title)
                'key': 'FFmpegMetadata',
                'add_metadata': True,
            }
        ],
        'outtmpl': os.path.join(session_dir, '%(title)s.%(ext)s'),
        'noplaylist': False,
        'playlist_items': f'1-{MAX_SONGS}',
        'ffmpeg_location': ffmpeg_exe,
        'ignoreerrors': True, 
        'cookiefile': 'cookies.txt' if os.path.exists('cookies.txt') else None,
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

        if active_tasks.get(session_id) is True:
            raise Exception("USER_CANCELLED")

        # Gather final MP3 files
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

        return jsonify({
            "status": "success", 
            "tracks": tracks, 
            "zipLink": zip_link, 
            "skipped": skipped, 
            "session_id": session_id
        })

    except Exception as e:
        if str(e) == "USER_CANCELLED":
            shutil.rmtree(session_dir, ignore_errors=True)
            return jsonify({"status": "cancelled", "message": "Conversion stopped."}), 200
        
        logger.exception("Conversion error")
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
    # It is recommended to keep debug=False in production
    app.run(debug=True, port=5000)