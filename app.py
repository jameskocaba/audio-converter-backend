import os
import uuid
import logging
import requests
import glob
from bs4 import BeautifulSoup
from flask import Flask, request, send_file, jsonify, after_this_request
from flask_cors import CORS
from yt_dlp import YoutubeDL

# --- LOGGING SETUP ---
logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(levelname)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app)

DOWNLOAD_FOLDER = os.path.join(os.getcwd(), 'downloads')
if not os.path.exists(DOWNLOAD_FOLDER):
    os.makedirs(DOWNLOAD_FOLDER)

def get_song_metadata(url):
    """Extracts song info to use YouTube search, bypassing direct link blocks."""
    try:
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
        response = requests.get(url, headers=headers, timeout=10)
        soup = BeautifulSoup(response.text, 'html.parser')
        page_title = soup.title.string if soup.title else ""
        clean_title = page_title.split(' on Apple Music')[0].replace(' - Single', '')
        return f"{clean_title} official audio"
    except Exception as e:
        logger.warning(f"Metadata extraction failed, using raw URL: {e}")
        return url

@app.route('/convert', methods=['POST'])
def convert_audio():
    data = request.json
    url = data.get('url', '')

    if not url:
        return jsonify({"error": "No URL provided"}), 400

    # 1. Prepare Search & Filename
    search_term = get_song_metadata(url)
    search_query = f"ytsearch1:{search_term}" if "youtube.com" not in search_term else search_term
    
    session_id = str(uuid.uuid4())
    # We use a unique folder for this session to avoid file mix-ups
    session_dir = os.path.join(DOWNLOAD_FOLDER, session_id)
    os.makedirs(session_dir, exist_ok=True)
    output_template = os.path.join(session_dir, 'audio.%(ext)s')

    ydl_opts = {
        'format': 'bestaudio/best',
        'noplaylist': True,
        'postprocessors': [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '192',
        }],
        'outtmpl': output_template,
        'cookiefile': 'cookies.txt', 
        'quiet': False,
        'extractor_args': {'youtube': {'player_client': ['android', 'web']}},
        'user_agent': 'Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Mobile Safari/537.36'
    }

    try:
        logger.info(f"Processing: {search_term}")
        with YoutubeDL(ydl_opts) as ydl:
            ydl.download([search_query])
        
        # 2. Find the resulting MP3 (yt-dlp adds .mp3 after conversion)
        # We look for any .mp3 file in the specific session folder
        mp3_files = glob.glob(os.path.join(session_dir, "*.mp3"))
        
        if mp3_files:
            final_file_path = mp3_files[0]
            filename = os.path.join(session_id, os.path.basename(final_file_path))
            logger.info(f"Success! Created: {filename}")
            return jsonify({"downloadLink": f"/download/{filename.replace(os.sep, '/')}"})
        else:
            # DEBUG: What actually happened in that folder?
            actual_contents = os.listdir(session_dir)
            logger.error(f"MP3 missing. Folder contains: {actual_contents}")
            return jsonify({"error": "FFmpeg conversion failed. Check server logs."}), 500
            
    except Exception as e:
        logger.error(f"CRITICAL ERROR: {str(e)}", exc_info=True)
        return jsonify({"error": "Download blocked or failed."}), 500

@app.route('/download/<session_id>/<filename>', methods=['GET'])
def download_file(session_id, filename):
    file_path = os.path.join(DOWNLOAD_FOLDER, session_id, filename)
    
    if os.path.exists(file_path):
        @after_this_request
        def cleanup(response):
            try:
                # Remove the entire session folder after download
                import shutil
                shutil.rmtree(os.path.join(DOWNLOAD_FOLDER, session_id))
                logger.info(f"Deleted session folder: {session_id}")
            except Exception as e:
                logger.error(f"Cleanup error: {e}")
            return response
        return send_file(file_path, as_attachment=True)
    
    return "File not found.", 404

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)