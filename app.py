import os
import uuid
import requests
from bs4 import BeautifulSoup
from flask import Flask, request, send_file, jsonify, after_this_request
from flask_cors import CORS
from yt_dlp import YoutubeDL

app = Flask(__name__)
CORS(app)

DOWNLOAD_FOLDER = os.path.join(os.getcwd(), 'downloads')
if not os.path.exists(DOWNLOAD_FOLDER):
    os.makedirs(DOWNLOAD_FOLDER)

def get_song_metadata(url):
    """Scrapes song title/artist from URL to use for search instead of direct link."""
    try:
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
        response = requests.get(url, headers=headers, timeout=10)
        soup = BeautifulSoup(response.text, 'html.parser')
        page_title = soup.title.string if soup.title else ""
        # Clean title for YouTube search
        clean_title = page_title.split(' on Apple Music')[0].replace(' - Single', '')
        return f"{clean_title} official audio"
    except:
        return url

@app.route('/convert', methods=['POST'])
def convert_audio():
    data = request.json
    url = data.get('url', '')

    if not url:
        return jsonify({"error": "No URL provided"}), 400

    # Convert the URL into a search string to bypass bot detection
    search_query = get_song_metadata(url)
    if "youtube.com" not in search_query and "youtu.be" not in search_query:
        search_query = f"ytsearch1:{search_query}"

    session_id = str(uuid.uuid4())
    output_template = os.path.join(DOWNLOAD_FOLDER, session_id)

    # yt-dlp 2025 Hardened Options
    ydl_opts = {
        'format': 'bestaudio/best',
        'noplaylist': True,
        'postprocessors': [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '192',
        }],
        'outtmpl': output_template,
        'cookiefile': 'cookies.txt',  # Still helpful if the file is fresh
        'quiet': False,
        # FORCE YT-DLP TO IMITATE MOBILE (Harder to block)
        'extractor_args': {
            'youtube': {
                'player_client': ['android', 'web'],
                'player_skip': ['configs', 'webpage']
            }
        },
        'user_agent': 'Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Mobile Safari/537.36'
    }

    try:
        print(f"DEBUG: Processing {search_query}")
        with YoutubeDL(ydl_opts) as ydl:
            ydl.download([search_query])
        
        expected_filename = f"{session_id}.mp3"
        actual_file_path = os.path.join(DOWNLOAD_FOLDER, expected_filename)
        
        if os.path.exists(actual_file_path):
            return jsonify({"downloadLink": f"/download/{expected_filename}"})
        else:
            return jsonify({"error": "Conversion finished but file was not found."}), 500
            
    except Exception as e:
        print(f"CRITICAL ERROR: {str(e)}")
        return jsonify({"error": f"YouTube blocked the request. Try a fresh cookies.txt."}), 500

@app.route('/download/<filename>', methods=['GET'])
def download_file(filename):
    safe_filename = os.path.basename(filename)
    file_path = os.path.join(DOWNLOAD_FOLDER, safe_filename)
    
    if os.path.exists(file_path):
        @after_this_request
        def remove_file(response):
            try:
                os.remove(file_path)
            except:
                pass
            return response
        return send_file(file_path, as_attachment=True)
    return "File not found.", 404

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)