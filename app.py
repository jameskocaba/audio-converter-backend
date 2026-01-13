import gevent.monkey
gevent.monkey.patch_all()

import os, uuid, logging, glob, zipfile, certifi, gc, time, json
from flask import Flask, request, send_file, jsonify, Response, stream_with_context
from flask_cors import CORS
from yt_dlp import YoutubeDL

# Setup
os.environ['SSL_CERT_FILE'] = certifi.where()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app)

DOWNLOAD_FOLDER = os.path.join(os.getcwd(), 'downloads')
os.makedirs(DOWNLOAD_FOLDER, exist_ok=True)
active_tasks = {}

def progress_hook(d, session_id):
    """Kills the current active FFmpeg/Download process if user cancels."""
    if active_tasks.get(session_id) is True:
        raise Exception("USER_CANCELLED")

def download_task(stream_url, title, session_dir, track_index, ffmpeg_exe, session_id):
    """Downloads using the direct stream URL to bypass metadata lag."""
    try:
        ydl_opts = {
            'format': 'bestaudio/best',
            'postprocessors': [{
                'key': 'FFmpegExtractAudio', 
                'preferredcodec': 'mp3', 
                'preferredquality': '128'
            }],
            'outtmpl': os.path.join(session_dir, f'track_{track_index}.%(ext)s'),
            'ffmpeg_location': ffmpeg_exe,
            'quiet': True,
            'nocheckcertificate': True,
            'cache_dir': False,
            'progress_hooks': [lambda d: progress_hook(d, session_id)], # Kill switch
            'referer': 'https://soundcloud.com/',
            'headers': {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
            }
        }
        with YoutubeDL(ydl_opts) as ydl:
            ydl.download([stream_url])
        return True
    except Exception as e:
        if "USER_CANCELLED" in str(e): raise e
        return False

def generate_conversion_stream(url, session_id):
    session_dir = os.path.join(DOWNLOAD_FOLDER, session_id)
    os.makedirs(session_dir, exist_ok=True)
    zip_path = os.path.join(session_dir, "archive.zip")
    
    ffmpeg_exe = 'ffmpeg'
    if os.path.exists(os.path.join(os.getcwd(), 'ffmpeg_bin/ffmpeg')):
        ffmpeg_exe = os.path.join(os.getcwd(), 'ffmpeg_bin/ffmpeg')

    try:
        # EXTREME SPEED: One-time prefetch of all track metadata
        yield f"data: {json.dumps({'type': 'status', 'message': 'Analyzing playlist tracks...'})}\n\n"
        
        with YoutubeDL({'extract_flat': True, 'quiet': True, 'referer': 'https://soundcloud.com/'}) as ydl:
            info = ydl.extract_info(url, download=False)
            entries = info.get('entries', [info])
            total = min(len(entries), 500)

        yield f"data: {json.dumps({'type': 'total', 'total': total})}\n\n"

        for i, entry in enumerate(entries[:total], 1):
            if active_tasks.get(session_id) is True: raise Exception("USER_CANCELLED")
            
            track_title = entry.get('title', f'Track {i}')
            track_url = entry.get('url')
            
            yield f": heartbeat {i}\n\n" # Keep Render connection alive
            yield f"data: {json.dumps({'type': 'progress', 'current': i, 'total': total, 'track': track_title})}\n\n"
            
            if download_task(track_url, track_title, session_dir, i, ffmpeg_exe, session_id):
                file_pattern = os.path.join(session_dir, f"track_{i}.mp3")
                if os.path.exists(file_pattern):
                    # ZIP_STORED = Zero RAM/CPU usage during bundling
                    with zipfile.ZipFile(zip_path, 'a', zipfile.ZIP_STORED) as z:
                        z.write(file_pattern, f"{track_title}.mp3")
                    os.remove(file_pattern) # Immediate disk cleanup
            
            if i % 10 == 0: gc.collect()

        yield f"data: {json.dumps({'type': 'done', 'zipLink': f'/download/{session_id}/archive.zip', 'total_processed': total, 'total_expected': total})}\n\n"

    except Exception as e:
        if "USER_CANCELLED" in str(e):
            yield f"data: {json.dumps({'type': 'status', 'message': 'Conversion Stopped.'})}\n\n"
        else:
            yield f"data: {json.dumps({'type': 'error', 'message': str(e)})}\n\n"
    finally:
        active_tasks.pop(session_id, None)
        gc.collect()

@app.route('/convert', methods=['POST'])
def convert():
    data = request.json
    sid = data.get('session_id', str(uuid.uuid4()))
    active_tasks[sid] = False
    return Response(stream_with_context(generate_conversion_stream(data['url'], sid)), mimetype='text/event-stream')

@app.route('/cancel', methods=['POST'])
def cancel():
    sid = request.json.get('session_id')
    if sid in active_tasks:
        active_tasks[sid] = True
    return jsonify({"status": "cancelled"}), 200

@app.route('/download/<sid>/<file>')
def get_file(sid, file):
    return send_file(os.path.join(DOWNLOAD_FOLDER, sid, file), as_attachment=True)

if __name__ == '__main__':
    app.run(threaded=True, port=5000)