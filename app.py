import gevent.monkey
gevent.monkey.patch_all()

import os, uuid, logging, glob, zipfile, certifi, shutil, gc, time, random
from flask import Flask, request, send_file, jsonify, Response, stream_with_context
from flask_cors import CORS
from yt_dlp import YoutubeDL
import json

# SSL & Logging
os.environ['SSL_CERT_FILE'] = certifi.where()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}})

DOWNLOAD_FOLDER = os.path.join(os.getcwd(), 'downloads')
os.makedirs(DOWNLOAD_FOLDER, exist_ok=True)
MAX_SONGS = 500 # Increased to support user request

active_tasks = {}

def progress_hook(d, session_id):
    if session_id in active_tasks and active_tasks[session_id]:
        raise Exception("USER_CANCELLED")

def cleanup_memory():
    gc.collect()

@app.route('/cancel', methods=['POST'])
def cancel_conversion():
    data = request.json
    session_id = data.get('session_id')
    if session_id and session_id in active_tasks:
        active_tasks[session_id] = True  
        return jsonify({"status": "cancelling"}), 200
    return jsonify({"status": "not_found"}), 404

def process_single_track(url, session_dir, track_index, ffmpeg_exe, session_id):
    """Processes one track and embeds metadata/art."""
    try:
        ydl_opts = {
            'format': 'bestaudio/best',
            'writethumbnail': True,
            'postprocessors': [
                {'key': 'FFmpegExtractAudio', 'preferredcodec': 'mp3', 'preferredquality': '128'},
                {'key': 'FFmpegThumbnailsConvertor', 'format': 'jpg'},
                {'key': 'EmbedThumbnail'},
                {'key': 'FFmpegMetadata', 'add_metadata': True}
            ],
            'outtmpl': os.path.join(session_dir, '%(title)s.%(ext)s'),
            'playlist_items': str(track_index),
            'ffmpeg_location': ffmpeg_exe,
            'quiet': True,
            'no_warnings': True,
            'cache_dir': False, # Avoid RAM-heavy caching
            'nocheckcertificate': True,
            'progress_hooks': [lambda d: progress_hook(d, session_id)],
        }
        
        with YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
        return True
    except Exception as e:
        logger.error(f"Track {track_index} failed: {e}")
        return False

def generate_conversion_stream(url, session_id):
    session_dir = os.path.join(DOWNLOAD_FOLDER, session_id)
    os.makedirs(session_dir, exist_ok=True)
    zip_path = os.path.join(session_dir, "playlist_backup.zip")
    
    ffmpeg_exe = 'ffmpeg'
    local_ffmpeg = os.path.join(os.getcwd(), 'ffmpeg_bin/ffmpeg')
    if os.path.exists(local_ffmpeg):
        ffmpeg_exe = local_ffmpeg

    try:
        yield f"data: {json.dumps({'type': 'status', 'message': 'Analyzing metadata...'})}\n\n"
        
        info_opts = {'extract_flat': 'in_playlist', 'quiet': True, 'nocheckcertificate': True}
        with YoutubeDL(info_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            entries = info.get('entries', [info])
            total_tracks = min(len(entries), MAX_SONGS)
            expected_titles = [e.get('title', f"Track {i+1}") for i, e in enumerate(entries[:total_tracks])]

        yield f"data: {json.dumps({'type': 'total', 'total': total_tracks})}\n\n"

        successful_count = 0
        for i in range(1, total_tracks + 1):
            if active_tasks.get(session_id) is True:
                raise Exception("USER_CANCELLED")
            
            # HEARTBEAT: Sent before processing each track to keep connection alive
            yield ": heartbeat\n\n" 
            
            track_name = expected_titles[i-1]
            yield f"data: {json.dumps({'type': 'progress', 'current': i, 'total': total_tracks, 'track': track_name})}\n\n"
            
            if process_single_track(url, session_dir, i, ffmpeg_exe, session_id):
                # Immediate ZIP and Delete to save Render RAM/Disk
                mp3_files = glob.glob(os.path.join(session_dir, "*.mp3"))
                if mp3_files:
                    current_file = mp3_files[0]
                    with zipfile.ZipFile(zip_path, 'a', zipfile.ZIP_DEFLATED) as z:
                        z.write(current_file, os.path.basename(current_file))
                    
                    os.remove(current_file) # Delete MP3 immediately
                    # Clean up orphaned images/temp files
                    for temp in glob.glob(os.path.join(session_dir, "*.jpg")) + glob.glob(os.path.join(session_dir, "*.webp")):
                        try: os.remove(temp)
                        except: pass
                    successful_count += 1
            
            cleanup_memory()

        result = {
            "type": "done",
            "zipLink": f"/download/{session_id}/playlist_backup.zip",
            "total_processed": successful_count,
            "total_expected": total_tracks,
            "tracks": [] # Individual links are disabled to save server state
        }
        yield f"data: {json.dumps(result)}\n\n"

    except Exception as e:
        if str(e) == "USER_CANCELLED":
            yield f"data: {json.dumps({'type': 'cancelled', 'message': 'Stopped.'})}\n\n"
        else:
            yield f"data: {json.dumps({'type': 'error', 'message': str(e)})}\n\n"
    finally:
        active_tasks.pop(session_id, None)
        cleanup_memory()

@app.route('/convert', methods=['POST'])
def convert_audio():
    data = request.json
    url = data.get('url', '').strip()
    session_id = data.get('session_id', str(uuid.uuid4()))
    active_tasks[session_id] = False
    return Response(
        stream_with_context(generate_conversion_stream(url, session_id)),
        mimetype='text/event-stream',
        headers={'Cache-Control': 'no-cache', 'Transfer-Encoding': 'chunked'}
    )

@app.route('/download/<session_id>/<filename>')
def download_file(session_id, filename):
    file_path = os.path.join(DOWNLOAD_FOLDER, session_id, filename)
    if os.path.exists(file_path):
        return send_file(file_path, as_attachment=True)
    return "File not found", 404

if __name__ == '__main__':
    app.run(debug=False, port=5000, threaded=True)