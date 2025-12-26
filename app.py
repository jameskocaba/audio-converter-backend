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

# 1. Suppress SSL warnings
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logging.basicConfig(level=logging.INFO, format='[%(asctime)s] %(levelname)s: %(message)s')
logger = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app)

DOWNLOAD_FOLDER = os.path.join(os.getcwd(), 'downloads')
os.makedirs(DOWNLOAD_FOLDER, exist_ok=True)

# Use absolute path to ensure Render finds FFmpeg inside the project folder
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FFMPEG_PATH = os.path.join(BASE_DIR, 'ffmpeg_bin')

def get_clean_metadata(url):
    try:
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
        response = requests.get(url, headers=headers, timeout=10, verify=False)
        soup = BeautifulSoup(response.text, 'html.parser')
        raw_title = soup.title.string if soup.title else ""
        # Clean Apple Music specific text
        clean_name = raw_title.split(' on Apple Music')[0]
        clean_name = clean_name.replace('Song by ', '').replace(' - Single', '')
        clean_name = re.sub(r'[^\x00-\x7F]+', ' ', clean_name).strip()
        return f"{clean_name} audio"
    except:
        return "latest hit song"

@app.route('/convert', methods=['POST'])
def convert_audio():
    data = request.json
    url = data.get('url', '').strip()
    
    search_term = get_clean_metadata(url)
    session_id = str(uuid.uuid4())
    session_dir = os.path.join(DOWNLOAD_FOLDER, session_id)
    os.makedirs(session_dir, exist_ok=True)
    
    output_template = os.path.join(session_dir, 'audio.%(ext)s')
    proxy_url = os.environ.get("PROXY_URL", "").strip() or None

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
        'proxy': proxy_url,
        'nocheckcertificate': True,
        'quiet': False,
        'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
    }

    try:
        # Check if it's a direct link to a supported site
        is_direct_supported = any(x in url for x in ["soundcloud.com", "bandcamp.com", "audiomack.com", "vimeo.com", "tiktok.com"])

        with YoutubeDL(ydl_opts) as ydl:
            if is_direct_supported:
                logger.info(f"Direct download from: {url}")
                ydl.download([url])
            else:
                # NEW SEARCH HIERARCHY: TikTok -> SoundCloud -> Bandcamp -> YouTube
                logger.info(f"Initiating multi-site search for: {search_term}")
                
                success = False
                
                # 1. Attempt TikTok Search (Note: yt-dlp uses general search for TikTok)
                try:
                    logger.info("Trying TikTok search...")
                    # Searching via TikTok often yields the viral audio used in clips
                    ydl.download([f"https://www.tiktok.com/search?q={search_term.replace(' ', '%20')}"])
                    success = True
                except Exception as e:
                    logger.warning(f"TikTok search failed: {e}")

                # 2. Attempt SoundCloud Search (if TikTok fails)
                if not success:
                    try:
                        logger.info("Trying SoundCloud search...")
                        ydl.download([f"scsearch1:{search_term}"])
                        success = True
                    except Exception as e:
                        logger.warning(f"SoundCloud search failed: {e}")

                # 3. Attempt Bandcamp Search (if others failed)
                if not success:
                    try:
                        logger.info("Trying Bandcamp search...")
                        ydl.download([f"bcsearch1:{search_term}"])
                        success = True
                    except Exception as e:
                        logger.warning(f"Bandcamp search failed: {e}")

                # 4. Final Fallback: YouTube
                if not success:
                    logger.info("Falling back to YouTube search...")
                    ydl.download([f"ytsearch1:{search_term} official"])
        
        mp3_files = glob.glob(os.path.join(session_dir, "*.mp3"))
        if mp3_files:
            relative_path = f"{session_id}/{os.path.basename(mp3_files[0])}"
            return jsonify({"downloadLink": f"/download/{relative_path}"})
        else:
            return jsonify({"error": "Download failed. Check the URL or logs."}), 400
            
    except Exception as e:
        logger.error(f"Error during conversion: {e}")
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