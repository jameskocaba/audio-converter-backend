import os
import uuid
import time
from flask import Flask, request, send_file, jsonify, after_this_request
from flask_cors import CORS
from yt_dlp import YoutubeDL

app = Flask(__name__)
CORS(app)

# Ensure the downloads folder exists
DOWNLOAD_FOLDER = os.path.join(os.getcwd(), 'downloads')
if not os.path.exists(DOWNLOAD_FOLDER):
    os.makedirs(DOWNLOAD_FOLDER)

@app.route('/convert', methods=['POST'])
def convert_audio():
    data = request.json
    url = data.get('url', '')

    if not url:
        return jsonify({"error": "No URL provided"}), 400

    if "playlist" in url.lower() or "/pl." in url.lower():
        return jsonify({"error": "Playlists not supported on free tier."}), 400

    search_query = url
    if "music.apple.com" in url:
        search_query = f"ytsearch:{url}"

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
        'quiet': True
    }

    try:
        with YoutubeDL(ydl_opts) as ydl:
            ydl.download([search_query])
        
        expected_filename = f"{session_id}.mp3"
        actual_file_path = os.path.join(DOWNLOAD_FOLDER, expected_filename)
        
        if os.path.exists(actual_file_path):
            return jsonify({"downloadLink": f"/download/{expected_filename}"})
        else:
            return jsonify({"error": "File conversion failed."}), 500
            
    except Exception as e:
        return jsonify({"error": "Conversion failed. Link may be protected."}), 500

@app.route('/download/<filename>', methods=['GET'])
def download_file(filename):
    safe_filename = os.path.basename(filename)
    file_path = os.path.join(DOWNLOAD_FOLDER, safe_filename)
    
    if os.path.exists(file_path):
        # The Magic Cleanup Logic:
        # This tells Flask to run this function AFTER the file is sent
        @after_this_request
        def remove_file(response):
            try:
                os.remove(file_path)
                print(f"Successfully deleted {file_path}")
            except Exception as error:
                print(f"Error deleting file: {error}")
            return response

        return send_file(file_path, as_attachment=True)
    else:
        return f"File not found.", 404

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)