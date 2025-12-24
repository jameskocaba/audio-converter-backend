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
    try:
        headers = {'User-Agent': 'Mozilla/5.0'}
        response = requests.get(url, headers=headers, timeout=10)
        soup = BeautifulSoup(response.text, 'html.parser')
        # This grabs the <title> tag (e.g., "Song Name by Artist on Apple Music")
        page_title = soup.title.string if soup.title else ""
        # Clean up the title to remove "on Apple Music" etc.
        clean_title = page_title.split(' on Apple Music')[0].replace(' - Single', '')
        return clean_title
    except:
        return url # Fallback to URL if scraping fails

@app.route('/convert', methods=['POST'])
def convert_audio():
    data = request.json
    url = data.get('url', '')

    # 1. Extract Metadata for Search
    song_info = get_song_metadata(url)
    # We use ytsearch1: to grab the most relevant result
    search_query = f"ytsearch1:{song_info} audio"

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
        # We try WITHOUT cookies first; search is often unblocked
        'quiet': False
    }

    try:
        print(f"DEBUG: Searching YouTube for: {song_info}")
        with YoutubeDL(ydl_opts) as ydl:
            ydl.download([search_query])
        
        expected_filename = f"{session_id}.mp3"
        actual_file_path = os.path.join(DOWNLOAD_FOLDER, expected_filename)
        
        if os.path.exists(actual_file_path):
            return jsonify({"downloadLink": f"/download/{expected_filename}"})
        else:
            return jsonify({"error": "File conversion failed."}), 500
            
    except Exception as e:
        print(f"CRITICAL ERROR: {str(e)}")
        return jsonify({"error": f"Internal Error: {str(e)}"}), 500

@app.route('/download/<filename>', methods=['GET'])
def download_file(filename):
    file_path = os.path.join(DOWNLOAD_FOLDER, os.path.basename(filename))
    if os.path.exists(file_path):
        @after_this_request
        def remove_file(response):
            try: os.remove(file_path)
            except: pass
            return response
        return send_file(file_path, as_attachment=True)
    return "File not found.", 404

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)