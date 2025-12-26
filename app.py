import os
import uuid
import logging
import requests
import re
import glob
import shutil
import urllib3
from bs4 import BeautifulSoup
from flask import Flask, request, send_file, jsonify, after_this_request
from flask_cors import CORS
from yt_dlp import YoutubeDL

# 1. Suppress SSL warnings (crucial for proxy stability)
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logging.basicConfig(level=logging.INFO, format='[%(asctime)s] %(levelname)s: %(message)s')
logger = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app)

DOWNLOAD_FOLDER = os.path.join(os.getcwd(), 'downloads')
os.makedirs(DOWNLOAD_FOLDER, exist_ok=True)

# 2. Path to FFmpeg (ensured by your build.sh)
FFMPEG_PATH = os.path.join(os.getcwd(), 'ffmpeg_bin')

def get_clean_metadata(url):
    try:
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
        response = requests.get(url, headers=headers, timeout=10, verify=False)
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

    # --- 3. BEYOND THE BOT DETECTION: TOKEN INJECTION ---
    # These must be set in your Render environment variables
    proxy_url = os.environ.get("PROXY_URL", "").strip() or None
    po_token = os.environ.get("PO_TOKEN", "").strip()
    visitor_data = os.environ.get("VISITOR_DATA", "").strip()

    # --- PROXY HEALTH CHECK ---
    if proxy_url:
        try:
            proxies = {"http": proxy_url, "https": proxy_url}
            test_response = requests.get('https://api.ipify.org', proxies=proxies, timeout=15, verify=False)
            logger.info(f"HEALTH CHECK: Proxy working. Outgoing IP: {test_response.text}")
        except Exception as e:
            logger.error(f"HEALTH CHECK FAILED: {e}")

    # --- UPDATED 2025 BYPASS YDL_OPTS ---
    ydl_opts = {
        'format': 'bestaudio/best',
        'ffmpeg_location': FFMPEG_PATH,
        'noplaylist': True,
        'postprocessors': [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '192',
        }],
        'outtmpl': output_template,
        
        # Connection stability
        'proxy': proxy_url,
        'cookiefile': 'cookies.txt', 
        'quiet': False,
        'nocheckcertificate': True,
        'socket_timeout': 30,
        'retries': 10,
        
        # User Agent: Mimic a modern iPhone to match the 'mweb' client
        'user_agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1',
        
        # --- THE 2025 HARD-BLOCK BYPASS ---
        'extractor_args': {
            'youtube': {
                # 'mweb' is the most stable for PO Token usage right now
                'player_client': ['mweb', 'tv'],
                'po_token': [f'mweb+{po_token}'] if po_token else None,
                'visitor_data': visitor_data if visitor_data else None,
                'player_skip': ['webpage', 'configs'],
            }
        },
    }

    try:
        logger.info(f"Searching for: {search_term}")
        with YoutubeDL(ydl_opts) as ydl:
            ydl.download([f"ytsearch1:{search_term} official"])
        
        mp3_files = glob.glob(os.path.join(session_dir, "*.mp3"))
        if mp3_files:
            relative_path = f"{session_id}/{os.path.basename(mp3_files[0])}"
            return jsonify({"downloadLink": f"/download/{relative_path}"})
        else:
            return jsonify({"error": "Extraction failed. Check logs