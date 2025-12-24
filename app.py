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

# 1. Path to FFmpeg (from your build.sh)
FFMPEG_PATH = os.path.join(os.getcwd(), 'ffmpeg_bin')

def get_clean_metadata(url):
    try:
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
        response = requests.get(url, headers=headers, timeout=10)
        soup = BeautifulSoup(response.text, 'html.parser')
        raw_title = soup.title.string if soup.title else ""
        clean_name = raw_title.split(' on Apple Music')[0]
        clean_name = clean_name.replace('Song by ', '').replace(' - Single', '')
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

    # 2. Get Proxy from Render Environment Variables (Optional but recommended)
    # Format: http://username:password@host:port
    proxy_url = os.environ.get("PROXY_URL") 

    ydl_opts = {
        'format': 'bestaudio/best',
        'ffmpeg_location': FFMPEG_PATH,
        'noplaylist': True,
        'postprocessors': [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '320', # BEST FORMAT QUALITY
        }],
        'outtmpl': output_template,
        
        # --- BYPASS SETTINGS ---
        'proxy': proxy_url,  # Uses the proxy if provided
        'cookiefile': 'cookies.txt', 
        'quiet': False,
        'nocheckcertificate': True,
        'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36',
        'extractor_args': {
            'youtube': {
                'player_client': ['android', 'ios'], # Mobile clients are key
                'player_skip': ['webpage', 'configs'],
            }
        },
    }

    try:
        logger.info(f"Searching for: {search_term}")
        if proxy_url:
            logger.info("Using Proxy for download...")

        with YoutubeDL(ydl_opts) as ydl:
            ydl.download([f"ytsearch1:{search_term} official"])
        
        mp3_files = glob.glob(os.path.join(session_dir, "*.mp3"))
        if mp3_files:
            relative_path = f"{session_id}/{os.path.basename(mp3_files[0])}"
            return jsonify({"downloadLink": f"/download/{relative_path}"})
        else:
            return jsonify({"error": "YouTube blocked the request. Try refreshing cookies."}), 403
            
    except Exception as e:
        logger.error(f"Error: {e}")
        return jsonify({"error": str(e)}), 500

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