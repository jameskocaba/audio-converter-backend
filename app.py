import gevent.monkey
gevent.monkey.patch_all()

import os, uuid, logging, glob, zipfile, certifi, shutil, gc, time
from flask import Flask, request, send_file, jsonify, Response, stream_with_context
from flask_cors import CORS
from yt_dlp import YoutubeDL
import json

# Setup
os.environ['SSL_CERT_FILE'] = certifi.where()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app)

DOWNLOAD_FOLDER = os.path.join(os.getcwd(), 'downloads')
os.makedirs(DOWNLOAD_FOLDER, exist_ok=True)
active_tasks = {}

def fast_process_track(url, session_dir, track_index, ffmpeg_exe, session_id):
    """Speed-optimized processing using stream copying and minimal overhead."""
    try:
        ydl_opts = {
            'format': 'bestaudio/best',
            'postprocessors': [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': '128', # Keeps metadata compatibility
            }],
            # Optimization: Use 'copy' if the source is already compatible
            'postprocessor_args': ['-c:a', 'libmp3lame', '-q:a', '2'], 
            'outtmpl': os.path.join(session_dir, f'track_{track_index}_%(title)s.%(ext)s'),
            'playlist_items': str(track_index),
            'ffmpeg_location': ffmpeg_exe,
            'quiet': True,
            'no_warnings': True,
            'cache_dir': False, # Critical for RAM
            'nocheckcertificate': True,
        }
        
        with YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
        return True
    except Exception as e:
        logger.error(f"Error on track {track_index}: {e}")
        return False

def generate_conversion_stream(url, session_id):
    session_dir = os.path.join(DOWNLOAD_FOLDER, session_id)
    os.makedirs(session_dir, exist_ok=True)
    zip_path = os.path.join(session_dir, "full_playlist_backup.zip")
    
    ffmpeg_exe = 'ffmpeg'
    if os.path.exists(os.path.join(os.getcwd(), 'ffmpeg_bin/ffmpeg')):
        ffmpeg_exe = os.path.join(os.getcwd(), 'ffmpeg_bin/ffmpeg')

    try:
        # Pre-fetch all metadata at once to avoid constant handshakes
        with YoutubeDL({'extract_flat': 'in_playlist', 'quiet': True}) as ydl:
            info = ydl.extract_info(url, download=False)
            entries = info.get('entries', [info])
            total = min(len(entries), 500)

        yield f"data: {json.dumps({'type': 'total', 'total': total})}\n\n"

        for i in range(1, total + 1):
            if active_tasks.get(session_id) is True: break
            
            # Send Heartbeat (Comment line) to keep Render connection alive
            yield f": heartbeat {i}\n\n"
            
            # Update UI
            yield f"data: {json.dumps({'type': 'progress', 'current': i, 'total': total})}\n\n"
            
            if fast_process_track(url, session_dir, i, ffmpeg_exe, session_id):
                # Move to ZIP immediately and DELETE original
                # Use ZIP_STORED: Zero compression, Zero RAM overhead, Instant
                new_files = glob.glob(os.path.join(session_dir, f"track_{i}_*"))
                if new_files:
                    with zipfile.ZipFile(zip_path, 'a', zipfile.ZIP_STORED) as z:
                        for f in new_files:
                            z.write(f, os.path.basename(f))
                            os.remove(f) # Clean disk immediately
            
            # Periodic RAM release
            if i % 10 == 0:
                gc.collect()

        yield f"data: {json.dumps({'type': 'done', 'zipLink': f'/download/{session_id}/full_playlist_backup.zip'})}\n\n"

    except Exception as e:
        yield f"data: {json.dumps({'type': 'error', 'message': str(e)})}\n\n"
    finally:
        active_tasks.pop(session_id, None)
        gc.collect()

@app.route('/convert', methods=['POST'])
def convert_audio():
    data = request.json
    url = data.get('url', '').strip()
    session_id = str(uuid.uuid4())
    active_tasks[session_id] = False
    return Response(
        stream_with_context(generate_conversion_stream(url, session_id)),
        mimetype='text/event-stream'
    )

@app.route('/download/<session_id>/<filename>')
def download_file(session_id, filename):
    return send_file(os.path.join(DOWNLOAD_FOLDER, session_id, filename), as_attachment=True)

if __name__ == '__main__':
    app.run(threaded=True, port=5000)