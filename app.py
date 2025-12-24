import os
import uuid
import logging
import requests
import re
import glob
import shutil
from bs4 import BeautifulSoup
from flask import Flask, request, send_file, jsonify, after_this_request
from flask_cors import CORS
from yt_dlp import YoutubeDL

logging.basicConfig(level=logging.INFO, format='[%(asctime)s] %(levelname)s: %(message)s')
logger = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app)

DOWNLOAD_FOLDER = os.path.join(os.getcwd(), 'downloads')
os.makedirs(DOWNLOAD_FOLDER, exist_ok=True)

def get_clean_metadata(url):
    """Scrapes and cleans metadata to prevent 'Special Character' errors."""
    try:
        headers = {'User-Agent': 'Mozilla/5.0'}
        response = requests.get(url, headers=headers, timeout=10)
        soup = BeautifulSoup(response.text, 'html.parser')
        raw_title = soup.title.string if soup.title else ""
        
        # 1. Remove "Apple Music" branding and "Single" tags
        clean_name = raw_title.split(' on Apple Music')[0]
        clean_name = clean_name.replace('Song by ', '').replace(' - Single', '')
        
        # 2. STRIP NON-ASCII CHARACTERS (The "Smoking Gun" fix)
        # This removes those â€Ž and MÃ¶ symbols that break the search
        clean_name = re.sub(r'[^\x00-\x7F]+', ' ', clean_name).strip()
        
        return f"{clean_name} audio"
    except:
        return "latest hit song"

@app.route('/convert', methods=['POST'])
def convert_audio():
    data = request.json
    url = data.get('url', '')
    
    search_term = get_clean_metadata(url)
    session_id = str(uuid.uuid4())
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
        # Allow it to work even if cookies are invalid
        'ignoreerrors': True, 
        'cookiefile': 'cookies.txt', 
        'quiet': False,
        'extractor_args': {'youtube': {'player_client': ['android', 'web']}},
    }

    try:
        logger.info(f"Clean Search Query: {search_term}")
        with YoutubeDL(ydl_opts) as ydl:
            # We add 'official' to improve search accuracy
            ydl.download([f"ytsearch1:{search_term} official"])
        
        mp3_files = glob.glob(os.path.join(session_dir, "*.mp3"))
        if mp3_files:
            relative_path = f"{session_id}/{os.path.basename(mp3_files[0])}"
            return jsonify({"downloadLink": f"/download/{relative_path}"})
        else:
            logger.error(f"Search failed for '{search_term}'. Folder empty.")
            return jsonify({"error": "No results found. Try a different link."}), 500
            
    except Exception as e:
        logger.error(f"Error: {e}")
        return jsonify({"error": "Conversion error."}), 500

@app.route('/download/<session_id>/<filename>', methods=['GET'])
def download_file(session_id, filename):
    file_path = os.path.join(DOWNLOAD_FOLDER, session_id, filename)
    if os.path.exists(file_path):
        @after_this_request
        def cleanup(response):
            shutil.rmtree(os.path.join(DOWNLOAD_FOLDER, session_id), ignore_errors=True)
            return response
        return send_file(file_path, as_attachment=True)
    return "File not found.", 404

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=os.environ.get("PORT", 5000))