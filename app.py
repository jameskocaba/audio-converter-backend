import os
import uuid
import logging
import requests
from bs4 import BeautifulSoup
from flask import Flask, request, send_file, jsonify, after_this_request
from flask_cors import CORS
from yt_dlp import YoutubeDL

# --- 1. SETUP LOGGING ---
# This ensures logs show up in your Render "Logs" tab with timestamps
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
    try:
        headers = {'User-Agent': 'Mozilla/5.0'}
        response = requests.get(url, headers=headers, timeout=10)
        soup = BeautifulSoup(response.text, 'html.parser')
        page_title = soup.title.string if soup.title else ""
        clean_title = page_title.split(' on Apple Music')[0].replace(' - Single', '')
        return f"{clean_title} official audio"
    except Exception as e:
        logger.warning(f"Metadata extraction failed: {e}")
        return url

@app.route('/convert', methods=['POST'])
def convert_audio():
    data = request.json
    url = data.get('url', '')

    if not url:
        logger.error("Request received with no URL")
        return jsonify({"error": "No URL provided"}), 400

    logger.info(f"Incoming request for: {url}")
    
    search_query = get_song_metadata(url)
    if "youtube.com" not in search_query and "youtu.be" not in search_query:
        search_query = f"ytsearch1:{search_query}"

    session_id = str(uuid.uuid4())
    output_template = os.path.join(DOWNLOAD_FOLDER, session_id)

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
        'quiet': False, # Keep False so yt-dlp internal logs show up in Render
        'extractor_args': {
            'youtube': {
                'player_client': ['android', 'web'],
            }
        },
        'user_agent': 'Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Mobile Safari/537.36'
    }

    try:
        logger.info(f"Starting yt-dlp search/download for: {search_query}")
        with YoutubeDL(ydl_opts) as ydl:
            ydl.download([search_query])
        
        expected_filename = f"{session_id}.mp3"
        actual_file_path = os.path.join(DOWNLOAD_FOLDER, expected_filename)
        
        if os.path.exists(actual_file_path):
            logger.info(f"Successfully converted: {expected_filename}")
            return jsonify({"downloadLink": f"/download/{expected_filename}"})
        else:
            logger.error(f"Conversion finished but file {expected_filename} is missing from disk")
            return jsonify({"error": "Conversion failed - file missing."}), 500
            
    except Exception as e:
        # This will print the full technical error in your Render logs
        logger.error(f"CRITICAL ERROR: {str(e)}", exc_info=True)
        return jsonify({"error": "YouTube blocked the request. Check server logs."}), 500

@app.route('/download/<filename>', methods=['GET'])
def download_file(filename):
    safe_filename = os.path.basename(filename)
    file_path = os.path.join(DOWNLOAD_FOLDER, safe_filename)
    
    if os.path.exists(file_path):
        @after_this_request
        def remove_file(response):
            try:
                os.remove(file_path)
                logger.info(f"Cleaned up file: {safe_filename}")
            except Exception as error:
                logger.warning(f"Cleanup failed for {safe_filename}: {error}")
            return response
        return send_file(file_path, as_attachment=True)
    
    logger.warning(f"Download attempted for missing file: {safe_filename}")
    return "File not found.", 404

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)