import os
import uuid
from flask import Flask, request, send_file, jsonify
from flask_cors import CORS
from yt_dlp import YoutubeDL

app = Flask(__name__)
CORS(app)

DOWNLOAD_FOLDER = os.path.join(os.getcwd(), 'downloads')
if not os.path.exists(DOWNLOAD_FOLDER):
    os.makedirs(DOWNLOAD_FOLDER)

@app.route('/convert', methods=['POST'])
def convert_audio():
    data = request.json
    url = data.get('url', '')

    if "playlist" in url.lower() or "/pl." in url.lower():
        return jsonify({"error": "Playlists not supported on free tier."}), 400

    # FORCE SEARCH FOR APPLE MUSIC
    # Instead of giving yt-dlp the link, we tell it to SEARCH YouTube Music
    # This bypasses the "Unsupported URL" block.
    search_query = url
    if "music.apple.com" in url:
        # We tell yt-dlp to treat the URL as a search term
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
        'cookiefile': 'cookies.txt', # Ensure this file exists in your repo!
    }

    try:
        with YoutubeDL(ydl_opts) as ydl:
            # We use the search_query instead of the raw URL
            ydl.download([search_query])
        
        return jsonify({"downloadLink": f"/download/{session_id}.mp3"})
    except Exception as e:
        print(f"Detailed Error: {str(e)}")
        return jsonify({"error": "YouTube blocked this request. Try a different song or check cookies."}), 500

@app.route('/download/<filename>', methods=['GET'])
def download_file(filename):
    file_path = os.path.join(DOWNLOAD_FOLDER, filename)
    if os.path.exists(file_path):
        return send_file(file_path, as_attachment=True)
    return jsonify({"error": "File not found"}), 404

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)