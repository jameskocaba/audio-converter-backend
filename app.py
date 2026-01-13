import gevent.monkey
gevent.monkey.patch_all() # MUST be the very first line

import os, uuid, logging, glob, zipfile, certifi, gc
from flask import Flask, request, send_file, jsonify, Response, stream_with_context
from flask_cors import CORS
from yt_dlp import YoutubeDL
import json

# Concurrency & Safety Tools
from gevent.pool import Pool
from gevent.queue import Queue, Empty
from gevent.lock import BoundedSemaphore

# SSL & Logging Configuration
os.environ['SSL_CERT_FILE'] = certifi.where()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}})

DOWNLOAD_FOLDER = os.path.join(os.getcwd(), 'downloads')
os.makedirs(DOWNLOAD_FOLDER, exist_ok=True)

# --- CONFIGURATION FOR FREE TIER ---
# 3 Workers is the "Sweet Spot". 
# It creates a pipeline (1 downloading, 1 converting, 1 zipping) without blowing up 512MB RAM.
CONCURRENT_WORKERS = 3 
MAX_SONGS = 500

# --- GLOBAL STATE ---
# We use these to track cancellations and prevent file corruption
active_tasks = {} 
zip_locks = {}

def cleanup_memory():
    """Forces RAM release - crucial for Render/Heroku free tiers"""
    gc.collect()

@app.route('/cancel', methods=['POST'])
def cancel_conversion():
    """
    Endpoint called by script.js when 'Cancel' is clicked.
    Sets a flag that all worker threads watch.
    """
    data = request.json
    session_id = data.get('session_id')
    if session_id and session_id in active_tasks:
        active_tasks[session_id] = True  # Set Cancel Flag to True
        logger.info(f"Session {session_id} marked for cancellation.")
        return jsonify({"status": "cancelling"}), 200
    return jsonify({"status": "not_found"}), 404

def worker_task(url, session_dir, track_index, ffmpeg_exe, session_id, queue, zip_path, lock, track_name):
    """
    The background worker. It handles one track at a time.
    It checks for cancellation at multiple steps to ensure instant stopping.
    """
    # CHECK 1: Did user cancel before we even started?
    if active_tasks.get(session_id, False):
        return

    # Unique temp name prevents collision if user tries same song twice
    temp_filename_base = f"track_{track_index}_{uuid.uuid4().hex[:6]}"
    
    ydl_opts = {
        'format': 'bestaudio/best',
        'writethumbnail': True,
        'postprocessors': [
            {'key': 'FFmpegExtractAudio', 'preferredcodec': 'mp3', 'preferredquality': '128'},
            {'key': 'FFmpegThumbnailsConvertor', 'format': 'jpg'},
            {'key': 'EmbedThumbnail'},
            {'key': 'FFmpegMetadata', 'add_metadata': True}
        ],
        'outtmpl': os.path.join(session_dir, f"{temp_filename_base}.%(ext)s"),
        'ffmpeg_location': ffmpeg_exe,
        'quiet': True,
        'no_warnings': True,
        'nocheckcertificate': True,
        'cache_dir': False, # RAM Saver: Disable disk cache
    }

    try:
        # Notify Frontend: "Processing Track X..."
        queue.put({'type': 'progress', 'current': track_index, 'track': track_name})

        # --- HEAVY LIFTING (Download & Convert) ---
        with YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])

        # CHECK 2: Did user cancel while we were downloading?
        if active_tasks.get(session_id, False):
            return

        # --- ZIPPING (Must be Thread-Safe) ---
        # We look for the MP3 we just made
        mp3_files = glob.glob(os.path.join(session_dir, f"{temp_filename_base}*.mp3"))
        
        if mp3_files:
            file_to_zip = mp3_files[0]
            
            # Sanitize filename for the final zip
            clean_name = "".join([c for c in track_name if c.isalnum() or c in (' ', '-', '_', '.')]).rstrip()
            if not clean_name: clean_name = f"Track_{track_index}"
            zip_entry_name = f"{clean_name}.mp3"

            # LOCK: Ensure only ONE thread writes to the zip at a time
            with lock:
                with zipfile.ZipFile(zip_path, 'a', zipfile.ZIP_DEFLATED) as z:
                    z.write(file_to_zip, zip_entry_name)
            
            queue.put({'type': 'success'})

    except Exception as e:
        logger.error(f"Error processing {track_name}: {e}")
        # We don't stop the whole playlist for one failed song, just log it.
        
    finally:
        # --- CLEANUP (Crucial for Cancel/Reset) ---
        # Delete the temp mp3/jpg files immediately to free up disk space
        try:
            for f in glob.glob(os.path.join(session_dir, f"{temp_filename_base}*")):
                os.remove(f)
            cleanup_memory()
        except:
            pass

def generate_conversion_stream(url, session_id):
    session_dir = os.path.join(DOWNLOAD_FOLDER, session_id)
    os.makedirs(session_dir, exist_ok=True)
    zip_path = os.path.join(session_dir, "playlist_backup.zip")
    
    # Initialize Concurrency Tools
    zip_locks[session_id] = BoundedSemaphore(1)
    msg_queue = Queue()
    
    ffmpeg_exe = 'ffmpeg'
    if os.path.exists('ffmpeg_bin/ffmpeg'):
        ffmpeg_exe = 'ffmpeg_bin/ffmpeg'

    # The Worker Pool
    pool = Pool(CONCURRENT_WORKERS)

    try:
        yield f"data: {json.dumps({'type': 'status', 'message': 'Fetching playlist data...'})}\n\n"
        
        # 1. GET METADATA
        info_opts = {'extract_flat': 'in_playlist', 'quiet': True, 'nocheckcertificate': True}
        with YoutubeDL(info_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            entries = list(info['entries']) if 'entries' in info else [info]
            
            # Filter valid entries
            valid_entries = []
            for i, e in enumerate(entries[:MAX_SONGS]):
                if e:
                    title = e.get('title', f"Track {i+1}")
                    # If it's a playlist, 'url' is the video URL. If single track, use input url.
                    track_url = e.get('url', url) 
                    valid_entries.append((i+1, track_url, title))
            
            total_real = len(valid_entries)

        yield f"data: {json.dumps({'type': 'total', 'total': total_real})}\n\n"

        # 2. START WORKERS
        for idx, t_url, t_title in valid_entries:
            if active_tasks.get(session_id, False): break
            
            pool.spawn(
                worker_task, 
                t_url, session_dir, idx, ffmpeg_exe, session_id, 
                msg_queue, zip_path, zip_locks[session_id], t_title
            )

        # 3. LISTEN FOR RESULTS
        completed_count = 0
        
        # We stay in this loop until all tasks are done OR cancelled
        while completed_count < total_real:
            
            # Global Cancel Check
            if active_tasks.get(session_id, False):
                raise Exception("USER_CANCELLED")
                
            try:
                # Wait for a message from any worker (2 second timeout to allow heartbeat)
                msg = msg_queue.get(timeout=2) 
                
                if msg['type'] == 'progress':
                    yield f"data: {json.dumps({'type': 'progress', 'current': msg['current'], 'total': total_real, 'track': msg['track']})}\n\n"
                elif msg['type'] == 'success':
                    completed_count += 1
                    
            except Empty:
                # If no news for 2 seconds, check if we are actually done
                if pool.free_count() == CONCURRENT_WORKERS and msg_queue.empty():
                    # Pool is idle and queue is empty -> We must be finished
                    break
                # Send heartbeat to keep browser connection alive
                yield ": heartbeat\n\n"

        # Ensure all threads finished cleanly
        pool.join()

        # 4. DONE
        result = {
            "type": "done",
            "zipLink": f"/download/{session_id}/playlist_backup.zip",
            "total_processed": completed_count,
            "total_expected": total_real,
            "tracks": [] 
        }
        yield f"data: {json.dumps(result)}\n\n"

    except Exception as e:
        if str(e) == "USER_CANCELLED":
            yield f"data: {json.dumps({'type': 'cancelled', 'message': 'Stopped.'})}\n\n"
        else:
            logger.error(f"Stream Error: {e}")
            yield f"data: {json.dumps({'type': 'error', 'message': 'Processing Error'})}\n\n"
    
    finally:
        # --- THE SAFETY NET ---
        # This block runs on Cancel, Error, OR if the user hits Reset (disconnects)
        active_tasks.pop(session_id, None)
        zip_locks.pop(session_id, None)
        
        # FORCE KILL: Stops all background downloads immediately
        pool.kill() 
        cleanup_memory()

@app.route('/convert', methods=['POST'])
def convert_audio():
    data = request.json
    url = data.get('url', '').strip()
    session_id = data.get('session_id', str(uuid.uuid4()))
    
    # Initialize Cancel Flag as False
    active_tasks[session_id] = False 
    
    return Response(
        stream_with_context(generate_conversion_stream(url, session_id)),
        mimetype='text/event-stream',
        headers={
            'Cache-Control': 'no-cache', 
            'Transfer-Encoding': 'chunked',
            'X-Accel-Buffering': 'no'
        }
    )

@app.route('/download/<session_id>/<filename>')
def download_file(session_id, filename):
    file_path = os.path.join(DOWNLOAD_FOLDER, session_id, filename)
    if os.path.exists(file_path):
        return send_file(file_path, as_attachment=True)
    return "File not found", 404

if __name__ == '__main__':
    app.run(debug=False, port=5000, threaded=True)