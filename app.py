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
        # verify=False prevents SSL crashes on the scraper
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

    # 3. Clean Proxy URL
    proxy_url = os.environ.get("PROXY_URL", "").strip()
    if not proxy_url:
        proxy_url = None

    # --- PROXY HEALTH CHECK ---
    if proxy_url:
        try:
            proxies = {"http": proxy_url, "https": proxy_url}
            test_response = requests.get('https://api.ipify.org', proxies=proxies, timeout=15, verify=False)
            logger.info(f"HEALTH CHECK: Proxy is working. Outgoing IP: {test_response.text}")
        except Exception as e:
            logger.error(f"HEALTH CHECK FAILED: {e}")
    else:
        logger.warning("HEALTH CHECK: No PROXY_URL found.")

    # --- FINAL BYPASS YDL_OPTS ---
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
        
        # Connection stability settings
        'proxy': proxy_url,
        'cookiefile': 'cookies.txt', 
        'quiet': False,
        'nocheckcertificate': True,
        'socket_timeout': 30,
        'retries': 10,
        
        # --- THE "PLAYER RESPONSE" BYPASS ---
        'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36',
        'http_headers': {
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'en-us,en;q=0.5',
            'Sec-Fetch-Mode': 'navigate',
        },
        'extractor_args': {
            'youtube': {
                # 'tv' and 'mweb' are currently the best for bypassing extraction errors
                'player_client': ['tv', 'mweb', 'android', 'ios'],
                'player_skip': ['webpage', 'configs'],
            }
        },
    }

    try:
        logger.info(f"Searching for: {search_term}")

        with YoutubeDL(ydl_opts) as ydl:
            # Using ytsearch1 is safer than direct URLs for bypass
            ydl.download([f"ytsearch1:{search_term} official"])
        
        mp3_files = glob.glob(os.path.join(session_dir, "*.mp3"))
        if mp3_files:
            relative_path = f"{session_id}/{os.path.basename(mp3_files[0])}"
            return jsonify({"downloadLink": f"/download/{relative_path}"})
        else:
            return jsonify({"error": "YouTube blocked extraction. Fresh cookies or proxy change required."}), 403
            
    except Exception as e:
        logger.error(f"Extraction Error: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/download/<session_id>/<filename>', methods=['GET'])
def download_file(session_id, filename):
    file_path = os.path.join(DOWNLOAD_FOLDER, session_id, filename)
    if os.path.exists(file_path):
        @after_this_request
        def cleanup(response):
            # Deletes the file after it is sent to the user
            shutil.rmtree(os.path.join(DOWNLOAD_FOLDER, session_id), ignore_errors=True)
            return response
        return send_file(file_path, as_attachment=True)
    return "File not found.", 404

if __name__ == '__main__':
    # PORT is assigned by Render
    app.run(host='0.0.0.0', port=os.environ.get("PORT", 5000))