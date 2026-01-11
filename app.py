import gevent.monkey
gevent.monkey.patch_all()

import os, uuid, logging, glob, zipfile, certifi, shutil, gc, time, random
from flask import Flask, request, send_file, jsonify, Response, stream_with_context
from flask_cors import CORS
from yt_dlp import YoutubeDL
from gevent.pool import Pool # Added for concurrency
import json

# SSL & Logging
os.environ['SSL_CERT_FILE'] = certifi.where()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}})

# Use /tmp for faster I/O on Render
DOWNLOAD_FOLDER = '/tmp/downloads'
os.makedirs(DOWNLOAD_FOLDER, exist_ok=True)
MAX_SONGS = 200

active_tasks = {}

def cleanup_memory():
    gc.collect()

def process_single_track(url, session_dir, track_index, ffmpeg_exe, session_id):
    """Process a single track with memory-efficient settings"""
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
            # OPTIMIZATION: Faster ffmpeg encoding
            'postprocessor_args': ['-preset', 'ultrafast', '-threads', '1'],
            'outtmpl': os.path.join(session_dir, '%(title)s.%(ext)s'),
            'playlist_items': str(track_index),
            'ffmpeg_location': ffmpeg_exe,
            'ignoreerrors': True,
            'quiet': True,
            'no_warnings': True,
            'buffersize': 1024, # Memory limit for buffer
            'nocheckcertificate': True,
        }
        
        with YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
        
        # Cleanup temp files immediately
        for ext in ['*.webp', '*.part', '*.ytdl', '*.tmp']:
            for file in glob.glob(os.path.join(session_dir, ext)):
                try: os.remove(file)
                except: pass
        
        return True
    except Exception as e:
        logger.error(f"Error track {track_index}: {e}")
        return False

def generate_conversion_stream(url, session_id):
    session_dir = os.path.join(DOWNLOAD_FOLDER, session_id)
    os.makedirs(session_dir, exist_ok=True)
    
    ffmpeg_exe = 'ffmpeg' # Standard on Render
    
    try:
        yield f"data: {json.dumps({'type': 'status', 'message': 'Analyzing metadata...'})}\n\n"
        
        # 1. Fetch metadata first (fast)
        with YoutubeDL({'extract_flat': 'in_playlist', 'quiet': True}) as ydl:
            info = ydl.extract_info(url, download=False)
            entries = info.get('entries', [info])
            all_entries = [e for e in entries if e]
            total_tracks = min(len(all_entries), MAX_SONGS)
            expected_titles = [e.get('title', 'Unknown Track') for e in all_entries[:total_tracks]]

        yield f"data: {json.dumps({'type': 'total', 'total': total_tracks})}\n\n"

        # 2. Parallel Processing with Gevent Pool
        # A pool of 2 is the "sweet spot" for 512MB RAM. 
        pool = Pool(size=2) 
        successful_tracks = []
        failed_tracks = []

        def track_task(i):
            if active_tasks.get(session_id) is True: return None
            
            track_name = expected_titles[i-1] if i-1 < len(expected_titles) else f"Track {i}"
            # Notify frontend that this track has started
            # Note: We return the result to the main generator to yield SSE
            success = process_single_track(url, session_dir, i, ffmpeg_exe, session_id)
            return {"index": i, "success": success, "name": track_name}

        # Use imap_unordered to yield results as they finish
        for result in pool.imap_unordered(track_task, range(1, total_tracks + 1)):
            if not result: continue
            
            if result['success']:
                successful_tracks.append(result['index'])
                yield f"data: {json.dumps({'type': 'complete', 'track': result['name']})}\n\n"
            else:
                failed_tracks.append(result['index'])
                yield f"data: {json.dumps({'type': 'failed', 'track': result['name']})}\n\n"
            
            cleanup_memory()

        # 3. Zip and Finish
        all_files = glob.glob(os.path.join(session_dir, "*.mp3")) + \
                    glob.glob(os.path.join(session_dir, "*.jpg"))
        
        zip_link = None
        if len(all_files) > 1:
            yield f"data: {json.dumps({'type': 'status', 'message': 'Bundling files...'})}\n\n"
            zip_name = f"bundle_{session_id[:8]}.zip"
            zip_path = os.path.join(session_dir, zip_name)
            
            # ZIP_STORED is much faster/lower RAM than DEFLATED
            with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_STORED) as z:
                for f in all_files:
                    z.write(f, os.path.basename(f))
            zip_link = f"/download/{session_id}/{zip_name}"

        yield f"data: {json.dumps({'type': 'done', 'zipLink': zip_link, 'total_processed': len(successful_tracks)})}\n\n"

    except Exception as e:
        logger.exception("Stream error")
        yield f"data: {json.dumps({'type': 'error', 'message': str(e)})}\n\n"
    finally:
        active_tasks.pop(session_id, None)
        cleanup_memory()

# Standard routes (same as your previous code)
@app.route('/convert', methods=['POST'])
def convert_audio():
    data = request.json
    url = data.get('url', '').strip()
    session_id = str(uuid.uuid4())
    active_tasks[session_id] = False
    return Response(stream_with_context(generate_conversion_stream(url, session_id)), mimetype='text/event-stream')

@app.route('/download/<session_id>/<filename>')
def download_file(session_id, filename):
    return send_file(os.path.join(DOWNLOAD_FOLDER, session_id, filename), as_attachment=True)

if __name__ == '__main__':
    app.run(threaded=True, port=5000)