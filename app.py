import os
import uuid
from flask import Flask, request, send_file, jsonify, after_this_request
from flask_cors import CORS
from yt_dlp import YoutubeDL

app = Flask(__name__)
CORS(app)

# Ensure the downloads folder exists
DOWNLOAD_FOLDER = os.path.join(os.getcwd(), 'downloads')
if not os.path.exists(DOWNLOAD_FOLDER):
    os.makedirs(DOWNLOAD_FOLDER)

@app.route('/', methods=['GET'])
def home():
    return "Backend is running!"

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
    output_template = os.path.join(DOWNLOAD_FOLDER, session_id)

    # yt-dlp Options
    ydl_opts = {
        'format': 'bestaudio/best',
        'noplaylist': True,
        'postprocessors': [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '192',
        }],
        'outtmpl': output_template,
        'cookiefile': 'cookies.txt',  # Ensure cookies.txt is in your GitHub repo!
        'quiet': False,               # Changed to False to see more in logs
    }

    try:
        print(f"DEBUG: Starting conversion for: {search_query}")
        with YoutubeDL(ydl_opts) as ydl:
            ydl.download([search_query])
        
        expected_filename = f"{session_id}.mp3"
        actual_file_path = os.path.join(DOWNLOAD_FOLDER, expected_filename)
        
        if os.path.exists(actual_file_path):
            print(f"DEBUG: Successfully created {actual_file_path}")
            return jsonify({"downloadLink": f"/download/{expected_filename}"})
        else:
            print(f"ERROR: yt-dlp finished but {actual_file_path} is missing!")
            return jsonify({"error": "File created but lost on server."}), 500
            
    except Exception as e:
        # This will show the real error in Render Logs
        print(f"CRITICAL ERROR DURING CONVERSION: {str(e)}")
        return jsonify({"error": f"Internal Server Error: {str(e)}"}), 500

@app.route('/download/<filename>', methods=['GET'])
def download_file(filename):
    safe_filename = os.path.basename(filename)
    file_path = os.path.join(DOWNLOAD_FOLDER, safe_filename)
    
    if os.path.exists(file_path):
        @after_this_request
        def remove_file(response):
            try:
                os.remove(file_path)
                print(f"DEBUG: Deleted temporary file {file_path}")
            except Exception as error:
                print(f"DEBUG: Error deleting file: {error}")
            return response

        return send_file(file_path, as_attachment=True)
    else:
        return f"File not found.", 404

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)