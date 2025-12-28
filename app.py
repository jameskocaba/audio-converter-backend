import os
import uuid
import logging
import glob
import shutil
import certifi  # Added for SSL fix
from flask import Flask, request, send_file, jsonify, after_this_request
from flask_cors import CORS
from yt_dlp import YoutubeDL

# CRITICAL: Tell Python to use certifi's certificate bundle
# This fixes the [SSL: CERTIFICATE_VERIFY_FAILED] error
os.environ['SSL_CERT_FILE'] = certifi.where()

logging.basicConfig(level=logging.INFO, format='[%(asctime)s] %(levelname)s: %(message)s')
logger = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app)

DOWNLOAD_FOLDER = os.path.join(os.getcwd(), 'downloads')
os.makedirs(DOWNLOAD_FOLDER, exist_ok=True)

# Custom logger to catch the specific error string from yt-dlp
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

    # --- FIX FOR "NoneType object is not callable" ---
    # Dynamically find the ffmpeg folder inside ffmpeg_bin
    # This ensures we find it even if the version name creates a subfolder
    ffmpeg_base = os.path.join(os.getcwd(), 'ffmpeg_bin')
    ffmpeg_path = ffmpeg_base
    for root, dirs, files in os.walk(ffmpeg_base):
        if 'ffmpeg' in files:
            ffmpeg_path = root
            break
    
    logger.info(f"Using FFmpeg location: {ffmpeg_path}")

    ydl_opts = {
        'format': 'bestaudio/best',
        'noplaylist': True,
        'ignoreerrors': True,
        'logger': error_logger,
        'outtmpl': output_template,
        'nocheckcertificate': True,  # Backup for SSL issues
        'cookiefile': 'cookies.txt', 
        
        # --- FIX FOR "Read timed out" ---
        'socket_timeout': 60,       # Wait 60s for slow SoundCloud responses
        'retries': 10,              # Retry 10 times if connection drops
        'fragment_retries': 10,     # Specifically helps with HLS streams
        
        'ffmpeg_location': ffmpeg_path, # Points to the EXACT folder containing the binary
        
        'postprocessors': [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '128', # 128 is faster to convert on limited CPUs
        }],
        'proxy': os.environ.get("PROXY_URL"),
    }

    try:
        with YoutubeDL(ydl_opts) as ydl:
            # Determine if it's a search or a direct link
            is_search = not any(x in url for x in ["soundcloud.com", "youtube.com", "bandcamp.com", "youtu.be"])
            query = f"scsearch1:{url}" if is_search else url
            
            logger.info(f"Starting download for: {query}")
            ydl.download([query])

        # Verification step: Did a file actually get created?
        mp3_files = glob.glob(os.path.join(session_dir, "*.mp3"))
        
        if mp3_files:
            relative_path = f"{session_id}/{os.path.basename(mp3_files[0])}"
            return jsonify({"status": "success", "downloadLink": f"/download/{relative_path}"})
        else:
            # Handle the "Skip" case
            error_msg = "Track skipped due to errors."
            if error_logger.last_error:
                if "geo restriction" in error_logger.last_error.lower():
                    error_msg = "Skipped: This track is geo-restricted."
                elif "sign in" in error_logger.last_error.lower():
                    error_msg = "Skipped: This track requires SoundCloud Go+."
                else:
                    error_msg = f"Skipped: {error_logger.last_error}"
            
            return jsonify({
                "status": "skipped",
                "message": error_msg
            }), 200

    except Exception as e:
        logger.exception("Unexpected error during conversion")
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/download/<session_id>/<filename>', methods=['GET'])
def download_file(session_id, filename):
    file_path = os.path.join(DOWNLOAD_FOLDER, session_id, filename)
    if os.path.exists(file_path):
        @after_this_request
        def cleanup(response):
            # Clean up the folder after sending the file
            shutil.rmtree(os.path.dirname(file_path), ignore_errors=True)
            return response
        return send_file(file_path, as_attachment=True)
    return "File not found.", 404

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 5000)))