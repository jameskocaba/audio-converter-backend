import gevent.monkey
gevent.monkey.patch_all()

import os, uuid, logging, glob, zipfile, certifi, shutil, gc, time, threading
from flask import Flask, request, send_file, jsonify, Response, stream_with_context
from flask_cors import CORS
from yt_dlp import YoutubeDL
from gevent.pool import Pool
import json

# SSL & Logging
os.environ['SSL_CERT_FILE'] = certifi.where()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}})

# Use /tmp for faster I/O and to avoid persistent storage issues on Render
DOWNLOAD_FOLDER = '/tmp/downloads'
os.makedirs(DOWNLOAD_FOLDER, exist_ok=True)
MAX_SONGS = 200

active_tasks = {}

def cleanup_memory():
    gc.collect()

def auto_cleanup_janitor():
    """Background thread to delete folders older than 30 minutes to save disk space"""
    while True:
        try:
            now = time.time()
            if os.path.exists(DOWNLOAD_FOLDER):
                for folder in os.listdir(DOWNLOAD_FOLDER):
                    path = os.path.join(DOWNLOAD_FOLDER, folder)
                    if os.path.getmtime(path) < now - 1800:
                        shutil.rmtree(path, ignore_errors=True)
        except Exception as e:
            logger.error(f"Janitor Error: {e}")
        time.sleep(300)

threading.Thread(target=auto_cleanup_janitor, daemon=True).start()

def process_single_track(url, session_dir, track_index, ffmpeg_exe, session_id):
    """Process a single track with optimized FFmpeg embedding"""
    try:
        ydl_opts = {
            'format': 'bestaudio/best',
            'writethumbnail': True,
            'postprocessors': [
                {'key': 'FFmpegThumbnailsConvertor', 'format': 'jpg'},
                {'key': 'FFmpegExtractAudio', 'preferredcodec': 'mp3', 'preferredquality': '128'},
                {'key': 'EmbedThumbnail'},
                {'key': 'FFmpegMetadata', 'add_metadata': True}
            ],
            'postprocessor_args': [
                '-preset', 'ultrafast', 
                '-threads', '1',
                '-id3v2_version', '3',
            ],
            'outtmpl': os.path.join(session_dir, '%(title)s.%(ext)s'),
            'playlist_items': str(track_index),
            'ffmpeg_location': ffmpeg_exe,
            'ignoreerrors': True,
            'quiet': True,
            'no_warnings': True,
            'buffersize': 1024,
            'nocheckcertificate': True,
        }
        
        with YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
        
        # Immediate cleanup of temporary files and loose images (since they are embedded now)
        for ext in ['*.webp', '*.part', '*.ytdl', '*.tmp', '*.jpg', '*.png']:
            for file in glob.glob(os.path.join(session_dir, ext)):
                try: os.remove(file)
                except: pass
        
        return True
    except Exception as e:
        logger.error(f"Error processing track {track_index}: {e}")
        return False

def generate_conversion_stream(url, session_id):
    session_dir = os.path.join(DOWNLOAD_FOLDER, session_id)
    os.makedirs(session_dir, exist_ok=True)
    
    # Improved FFmpeg detection for Render Buildpacks
    ffmpeg_exe = shutil.which('ffmpeg') or '/usr/bin/ffmpeg'
    local_ffmpeg = os.path.join(os.getcwd(), 'ffmpeg_bin/ffmpeg')
    if os.path.exists(local_ffmpeg):
        ffmpeg_exe = local_ffmpeg

    try:
        yield f"data: {json.dumps({'type': 'status', 'message': 'Analyzing metadata...'})}\n\n"
        
        with YoutubeDL({'extract_flat': 'in_playlist', 'quiet': True}) as ydl:
            info = ydl.extract_info(url, download=False)
            entries = info.get('entries', [info])
            all_entries = [e for e in entries if e]
            total_tracks = min(len(all_entries), MAX_SONGS)
            expected_titles = [e.get('title', 'Unknown Track') for e in all_entries[:total_tracks]]

        yield f"data: {json.dumps({'type': 'total', 'total': total_tracks})}\n\n"

        # Concurrency: 2 tracks at once. High enough to be fast, low enough for 512MB RAM.
        pool = Pool(size=2)
        successful_tracks = []
        failed_tracks = []

        def track_task(i):
            if active_tasks.get(session_id) is True: return None
            track_name = expected_titles[i-1] if i-1 < len(expected_titles) else f"Track {i}"
            
            # This is processed in a thread, so we return result to the main generator
            success = process_single_track(url, session_dir, i, ffmpeg_exe, session_id)
            return {"index": i, "success": success, "name": track_name}

        # Progress tracking loop
        processed_count = 0
        for result in pool.imap_unordered(track_task, range(1, total_tracks + 1)):
            if not result: continue
            processed_count += 1
            
            # Send progress bar updates
            yield f"data: {json.dumps({'type': 'progress', 'current': processed_count, 'total': total_tracks, 'track': result['name']})}\n\n"
            
            if result['success']:
                successful_tracks.append(result['index'])
                yield f"data: {json.dumps({'type': 'complete', 'track': result['name']})}\n\n"
            else:
                failed_tracks.append(result['index'])
                yield f"data: {json.dumps({'type': 'failed', 'track': result['name']})}\n\n"
            
            cleanup_memory()

        # Finalize links for frontend
        mp3_files = glob.glob(os.path.join(session_dir, "*.mp3"))
        tracks_list = [{"name": os.path.basename(f), "downloadLink": f"/download/{session_id}/{os.path.basename(f)}"} for f in mp3_files]
        
        zip_link = None
        if len(mp3_files) > 1:
            yield f"data: {json.dumps({'type': 'status', 'message': 'Creating ZIP bundle...'})}\n\n"
            zip_name = f"bundle_{session_id[:8]}.zip"
            zip_path = os.path.join(session_dir, zip_name)
            with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_STORED) as z:
                for f in mp3_files:
                    z.write(f, os.path.basename(f))
            zip_link = f"/download/{session_id}/{zip_name}"

        yield f"data: {json.dumps({'type': 'done', 'tracks': tracks_list, 'zipLink': zip_link, 'total_processed': len(successful_tracks)})}\n\n"

    except Exception as e:
        logger.exception("Stream error")
        yield f"data: {json.dumps({'type': 'error', 'message': str(e)})}\n\n"
    finally:
        active_tasks.pop(session_id, None)
        cleanup_memory()

@app.route('/convert', methods=['POST'])
def convert_audio():
    data = request.json
    url = data.get('url', '').strip()
    session_id = str(uuid.uuid4())
    active_tasks[session_id] = False
    return Response(
        stream_with_context(generate_conversion_stream(url, session_id)), 
        mimetype='text/event-stream',
        headers={'X-Accel-Buffering': 'no', 'Cache-Control': 'no-cache'}
    )

@app.route('/download/<session_id>/<filename>')
def download_file(session_id, filename):
    return send_file(os.path.join(DOWNLOAD_FOLDER, session_id, filename), as_attachment=True)

@app.route('/cancel', methods=['POST'])
def cancel_conversion():
    session_id = request.json.get('session_id')
    if session_id in active_tasks:
        active_tasks[session_id] = True
        return jsonify({"status": "cancelling"}), 200
    return jsonify({"status": "not_found"}), 404

if __name__ == '__main__':
    app.run(port=5000)