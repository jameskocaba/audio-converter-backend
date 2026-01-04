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

active_tasks = {}

def progress_hook(d, session_id):
    if session_id in active_tasks and active_tasks[session_id]:
        raise Exception("USER_CANCELLED")

@app.route('/convert', methods=['POST'])
def convert_audio():
    data = request.json
    url = data.get('url', '').strip()
    
    if not url or "soundcloud.com" not in url.lower():
        return jsonify({"status": "error", "message": "Invalid SoundCloud link."}), 400

    session_id = str(uuid.uuid4())
    active_tasks[session_id] = False 
    session_dir = os.path.join(DOWNLOAD_FOLDER, session_id)
    os.makedirs(session_dir, exist_ok=True)
    
    ffmpeg_exe = os.path.join(os.getcwd(), 'ffmpeg_bin/ffmpeg')
    for root, dirs, files in os.walk(os.path.join(os.getcwd(), 'ffmpeg_bin')):
        if 'ffmpeg' in files:
            ffmpeg_exe = os.path.join(root, 'ffmpeg')
            os.chmod(ffmpeg_exe, 0o755)
            break

    ydl_opts = {
        'format': 'bestaudio/best',
        'writethumbnail': True,
        'postprocessors': [
            {
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': '0',
            },
            {
                # FORCE CONVERSION: This fixes the "already in jpg format" lie from SoundCloud
                'key': 'FFmpegThumbnailsConvertor',
                'format': 'jpg',
                'when': 'always', 
            },
            {
                'key': 'EmbedThumbnail',
            },
            {
                'key': 'FFmpegMetadata',
                'add_metadata': True,
            }
        ],
        # COMPATIBILITY FLAGS: Makes art visible in Windows/Phone players
        'postprocessor_args': {
            'ffmpeg': [
                '-id3v2_version', '3', 
                '-metadata:s:v', 'title="Album cover"', 
                '-metadata:s:v', 'comment="Cover (Front)"'
            ]
        },
        'outtmpl': os.path.join(session_dir, '%(title)s.%(ext)s'),
        'noplaylist': False,
        'playlist_items': f'1-{MAX_SONGS}',
        'ffmpeg_location': ffmpeg_exe,
        'ignoreerrors': True, 
        'progress_hooks': [lambda d: progress_hook(d, session_id)],
    }

    try:
        with YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])

        mp3_files = glob.glob(os.path.join(session_dir, "*.mp3"))
        tracks = [{"name": os.path.basename(f), "downloadLink": f"/download/{session_id}/{os.path.basename(f)}"} for f in mp3_files]

        zip_link = None
        if len(mp3_files) > 1:
            zip_name = "bundle.zip"
            with zipfile.ZipFile(os.path.join(session_dir, zip_name), 'w') as z:
                for f in mp3_files:
                    z.write(f, os.path.basename(f))
            zip_link = f"/download/{session_id}/{zip_name}"

        return jsonify({"status": "success", "tracks": tracks, "zipLink": zip_link, "session_id": session_id})

    except Exception as e:
        logger.exception("Conversion error")
        return jsonify({"status": "error", "message": str(e)}), 500
    finally:
        active_tasks.pop(session_id, None)

@app.route('/download/<session_id>/<filename>')
def download_file(session_id, filename):
    file_path = os.path.join(DOWNLOAD_FOLDER, session_id, filename)
    return send_file(file_path, as_attachment=True) if os.path.exists(file_path) else ("Not found", 404)

if __name__ == '__main__':
    app.run(debug=True, port=5000)