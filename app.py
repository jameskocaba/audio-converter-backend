import gevent.monkey
gevent.monkey.patch_all()

import os, uuid, logging, glob, zipfile, certifi
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

@app.route('/convert', methods=['POST'])
def convert_audio():
    data = request.json
    url = data.get('url', '').strip()
    session_id = str(uuid.uuid4())
    session_dir = os.path.join(DOWNLOAD_FOLDER, session_id)
    os.makedirs(session_dir, exist_ok=True)
    
    # Locate FFmpeg
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
        'ignoreerrors': True, # Important for skipping bad tracks
        'cookiefile': 'cookies.txt' if os.path.exists('cookies.txt') else None,
    }

    try:
        with YoutubeDL(ydl_opts) as ydl:
            # 1. Map expected tracks
            info = ydl.extract_info(url, download=False)
            expected_titles = []
            if 'entries' in info:
                expected_titles = [e['title'] for e in info['entries'] if e]
            else:
                expected_titles = [info['title']]

            # 2. Perform Download
            ydl.download([url])

        # 3. Identify what actually downloaded
        mp3_files = glob.glob(os.path.join(session_dir, "*.mp3"))
        downloaded_names = [os.path.basename(f) for f in mp3_files]
        
        # 4. Filter out skipped titles
        skipped = []
        for title in expected_titles:
            # Check if any downloaded file contains the title (yt-dlp sanitizes names)
            if not any(title[:20] in d_name for d_name in downloaded_names):
                skipped.append(title)

        tracks = [{"name": n, "downloadLink": f"/download/{session_id}/{n}"} for n in downloaded_names]

        # 5. Create ZIP
        zip_link = None
        if len(mp3_files) > 1:
            zip_name = "breakfast_bundle.zip"
            with zipfile.ZipFile(os.path.join(session_dir, zip_name), 'w') as z:
                for f in mp3_files:
                    z.write(f, os.path.basename(f))
            zip_link = f"/download/{session_id}/{zip_name}"

        return jsonify({
            "status": "success", 
            "tracks": tracks, 
            "zipLink": zip_link, 
            "skipped": skipped
        })

    except Exception as e:
        logger.exception("Conversion error")
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/download/<session_id>/<filename>')
def download_file(session_id, filename):
    file_path = os.path.join(DOWNLOAD_FOLDER, session_id, filename)
    return send_file(file_path, as_attachment=True)

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)