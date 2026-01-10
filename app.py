import gevent.monkey
gevent.monkey.patch_all()

import os, uuid, logging, glob, zipfile, certifi, shutil, time, subprocess
from flask import Flask, request, send_file, jsonify
from flask_cors import CORS
from yt_dlp import YoutubeDL
import gevent
from gevent.lock import Semaphore

os.environ['SSL_CERT_FILE'] = certifi.where()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app)

DOWNLOAD_FOLDER = os.path.join(os.getcwd(), 'downloads')
os.makedirs(DOWNLOAD_FOLDER, exist_ok=True)

memory_guard = Semaphore(1)
task_status = {}

def find_ffmpeg():
    """Locates ffmpeg on Render or local environments."""
    paths = ['/usr/bin/ffmpeg', '/usr/local/bin/ffmpeg', 'ffmpeg']
    for path in paths:
        if shutil.which(path):
            return path
    return None

def run_conversion_task(url, session_id, session_dir):
    with memory_guard:
        ffmpeg_path = find_ffmpeg()
        if not ffmpeg_path:
            task_status[session_id] = {"status": "error", "message": "FFmpeg not installed. Please check Render Buildpacks."}
            return

        try:
            ydl_opts = {
                'format': 'bestaudio/best',
                'ffmpeg_location': ffmpeg_path,
                'postprocessors': [{
                    'key': 'FFmpegExtractAudio',
                    'preferredcodec': 'mp3',
                    'preferredquality': '192',
                }],
                'outtmpl': os.path.join(session_dir, '%(title)s.%(ext)s'),
                'quiet': True,
                'no_warnings': True,
            }

            with YoutubeDL(ydl_opts) as ydl:
                ydl.download([url])

            mp3_files = glob.glob(os.path.join(session_dir, "*.mp3"))
            
            if not mp3_files:
                raise Exception("Conversion produced no MP3s. Link might be restricted.")

            zip_link = None
            if len(mp3_files) > 1:
                zip_name = f"bundle_{session_id[:5]}.zip"
                zip_path = os.path.join(session_dir, zip_name)
                with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as z:
                    for f in mp3_files:
                        z.write(f, os.path.basename(f))
                zip_link = f"/download/{session_id}/{zip_name}"

            task_status[session_id] = {
                "status": "completed",
                "zipLink": zip_link,
                "tracks": [{"name": os.path.basename(f), "downloadLink": f"/download/{session_id}/{os.path.basename(f)}"} for f in mp3_files]
            }

        except Exception as e:
            logger.error(f"Task Failed: {e}")
            task_status[session_id] = {"status": "error", "message": str(e)}

@app.route('/convert', methods=['POST'])
def convert():
    data = request.json
    url = data.get('url', '').strip()
    session_id = str(uuid.uuid4())
    session_dir = os.path.join(DOWNLOAD_FOLDER, session_id)
    os.makedirs(session_dir, exist_ok=True)

    task_status[session_id] = {"status": "processing", "count": 0}
    gevent.spawn(run_conversion_task, url, session_id, session_dir)
    return jsonify({"status": "started", "session_id": session_id}), 202

@app.route('/status/<session_id>')
def status(session_id):
    return jsonify(task_status.get(session_id, {"status": "not_found"}))

@app.route('/download/<session_id>/<filename>')
def download(session_id, filename):
    return send_file(os.path.join(DOWNLOAD_FOLDER, session_id, filename), as_attachment=True)

if __name__ == '__main__':
    from gevent.pywsgi import WSGIServer
    WSGIServer(('0.0.0.0', 5000), app).serve_forever()