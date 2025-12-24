import os
import uuid
from flask import Flask, request, send_file, jsonify
from flask_cors import CORS
from yt_dlp import YoutubeDL

app = Flask(__name__)
CORS(app)

# Ensure the downloads folder exists in the current directory
DOWNLOAD_FOLDER = os.path.join(os.getcwd(), 'downloads')
if not os.path.exists(DOWNLOAD_FOLDER):
    os.makedirs(DOWNLOAD_FOLDER)

@app.route('/convert', methods=['POST'])
def convert_audio():
    data = request.json
    url = data.get('url', '')

    if not url:
        return jsonify({"error": "No URL provided"}), 400

    # Playlist Guard
    if "playlist" in url.lower() or "/pl." in url.lower():
        return jsonify({"error": "Playlists not supported on free tier."}), 400

    # Force search to bypass "Unsupported URL" for Apple Music
    search_query = url
    if "music.apple.com" in url:
        search_query = f"ytsearch:{url}"

    session_id = str(uuid.uuid4())
    # The template for the filename (yt-dlp adds the .mp3)
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
        'cookiefile': 'cookies.txt',  # Requires your cookies.txt file in the repo
        'quiet': True
    }

    try:
        with YoutubeDL(ydl_opts) as ydl:
            ydl.download([search_query])
        
        # Verify the file exists with the .mp3 extension
        expected_filename = f"{session_id}.mp3"
        actual_file_path = os.path.join(DOWNLOAD_FOLDER, expected_filename)
        
        if os.path.exists(actual_file_path):
            # Send back ONLY the filename for the download route
            return jsonify({"downloadLink": f"/download/{expected_filename}"})
        else:
            return jsonify({"error": "Conversion finished but file was not found."}), 500
            
    except Exception as e:
        print(f"Error: {str(e)}")
        return jsonify({"error": "Conversion failed. Check Render logs for details."}), 500

@app.route('/download/<filename>', methods=['GET'])
def download_file(filename):
    # Security check: ensure the filename is just a filename, not a path
    safe_filename = os.path.basename(filename)
    file_path = os.path.join(DOWNLOAD_FOLDER, safe_filename)
    
    if os.path.exists(file_path):
        return send_file(file_path, as_attachment=True)
    else:
        return f"File not found on server at: {file_path}", 404

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)