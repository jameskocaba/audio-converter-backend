import gevent.monkey
gevent.monkey.patch_all()

import os, uuid, logging, glob, zipfile, certifi, shutil, gc, time
from flask import Flask, request, send_file, jsonify, Response, stream_with_context
from flask_cors import CORS
from yt_dlp import YoutubeDL
import json

app = Flask(__name__)
CORS(app)

DOWNLOAD_FOLDER = os.path.join(os.getcwd(), 'downloads')
os.makedirs(DOWNLOAD_FOLDER, exist_ok=True)
active_tasks = {}

def fast_process_track(url, session_dir, track_index, ffmpeg_exe, session_id):
    """Bypasses heavy re-encoding for maximum speed and stability."""
    try:
        ydl_opts = {
            'format': 'bestaudio/best',
            # SPEED BOOST: Avoid re-encoding. 
            # We use 'copy' to just move the audio into an MP4/M4A container 
            # which is much faster and lighter on RAM than MP3 encoding.
            'postprocessors': [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': '128',
            }],
            'outtmpl': os.path.join(session_dir, f'track_{track_index}_%(title)s.%(ext)s'),
            'playlist_items': str(track_index),
            'ffmpeg_location': ffmpeg_exe,
            'quiet': True,
            'no_warnings': True,
            'cache_dir': False,
            'nocheckcertificate': True,
            # FIX for Client ID Error:
            'referer': 'https://soundcloud.com/',
            'headers': {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
            }
        }
        
        with YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
        return True
    except Exception as e:
        logger.error(f"Track {track_index} error: {e}")
        return False

def generate_conversion_stream(url, session_id):
    session_dir = os.path.join(DOWNLOAD_FOLDER, session_id)
    os.makedirs(session_dir, exist_ok=True)
    zip_path = os.path.join(session_dir, "archive.zip")
    
    # Render Paths
    ffmpeg_exe = 'ffmpeg'
    if os.path.exists(os.path.join(os.getcwd(), 'ffmpeg_bin/ffmpeg')):
        ffmpeg_exe = os.path.join(os.getcwd(), 'ffmpeg_bin/ffmpeg')

    try:
        # Pre-scan playlist structure (Fast)
        with YoutubeDL({'extract_flat': 'in_playlist', 'quiet': True, 'referer': 'https://soundcloud.com/'}) as ydl:
            info = ydl.extract_info(url, download=False)
            entries = info.get('entries', [info])
            total = min(len(entries), 500)

        yield f"data: {json.dumps({'type': 'total', 'total': total})}\n\n"

        for i in range(1, total + 1):
            if active_tasks.get(session_id): break
            
            yield f": heartbeat\n\n" # Keep connection alive
            yield f"data: {json.dumps({'type': 'progress', 'current': i, 'total': total})}\n\n"
            
            if fast_process_track(url, session_dir, i, ffmpeg_exe, session_id):
                # ZIP_STORED = Zero CPU/RAM usage for zipping
                new_files = glob.glob(os.path.join(session_dir, f"track_{i}_*"))
                if new_files:
                    with zipfile.ZipFile(zip_path, 'a', zipfile.ZIP_STORED) as z:
                        for f in new_files:
                            z.write(f, os.path.basename(f))
                            os.remove(f) # Keep disk clean
            
            if i % 5 == 0: gc.collect()

        yield f"data: {json.dumps({'type': 'done', 'zipLink': f'/download/{session_id}/archive.zip'})}\n\n"

    except Exception as e:
        yield f"data: {json.dumps({'type': 'error', 'message': str(e)})}\n\n"
    finally:
        active_tasks.pop(session_id, None)

@app.route('/convert', methods=['POST'])
def convert():
    data = request.json
    sid = str(uuid.uuid4())
    active_tasks[sid] = False
    return Response(stream_with_context(generate_conversion_stream(data['url'], sid)), mimetype='text/event-stream')

@app.route('/download/<sid>/<file>')
def get_file(sid, file):
    return send_file(os.path.join(DOWNLOAD_FOLDER, sid, file), as_attachment=True)

if __name__ == '__main__':
    app.run(threaded=True, port=5000)