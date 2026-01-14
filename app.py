import gevent.monkey
gevent.monkey.patch_all()

import os, uuid, logging, glob, zipfile, certifi, gc
from flask import Flask, request, send_file, jsonify, Response, stream_with_context
from flask_cors import CORS
from yt_dlp import YoutubeDL
import json

# Concurrency & Safety Tools
from gevent.pool import Pool
from gevent.queue import Queue, Empty
from gevent.lock import BoundedSemaphore

os.environ['SSL_CERT_FILE'] = certifi.where()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}})

DOWNLOAD_FOLDER = os.path.join(os.getcwd(), 'downloads')
os.makedirs(DOWNLOAD_FOLDER, exist_ok=True)

# --- TUNING ---
CONCURRENT_WORKERS = 3 
MAX_SONGS = 500

# Global State
active_tasks = {} 
zip_locks = {}

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

def worker_task(url, session_dir, track_index, ffmpeg_exe, session_id, queue, zip_path, lock, track_name):
    if active_tasks.get(session_id, False): return

    temp_filename_base = f"track_{track_index}_{uuid.uuid4().hex[:6]}"
    
    # METADATA & STABILITY CONFIG
    ydl_opts = {
        'format': 'bestaudio/best',
        'outtmpl': os.path.join(session_dir, f"{temp_filename_base}.%(ext)s"),
        'ffmpeg_location': ffmpeg_exe,
        'quiet': True,
        'no_warnings': True,
        'nocheckcertificate': True,
        'cache_dir': False,
        # KEY FIX: Metadata injection
        'writethumbnail': True,
        'postprocessors': [
            {
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': '192', # Bumped to 192 for better meta support
            },
            {
                'key': 'EmbedThumbnail',
            },
            {
                'key': 'FFmpegMetadata',
                'add_metadata': True,
            }
        ],
    }

    try:
        # STEP 1: DOWNLOADING
        queue.put({'type': 'detail', 'current': track_index, 'status': 'Downloading...'})
        
        with YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])

        if active_tasks.get(session_id, False): return

        # STEP 2: ZIPPING & TAGGING
        mp3_files = glob.glob(os.path.join(session_dir, f"{temp_filename_base}*.mp3"))
        
        if mp3_files:
            queue.put({'type': 'detail', 'current': track_index, 'status': 'Zipping...'})
            file_to_zip = mp3_files[0]
            
            # Clean Filename
            clean_name = "".join([c for c in track_name if c.isalnum() or c in (' ', '-', '_', '.')]).rstrip()
            if not clean_name: clean_name = f"Track_{track_index}"
            zip_entry_name = f"{clean_name}.mp3"

            with lock:
                with zipfile.ZipFile(zip_path, 'a', zipfile.ZIP_DEFLATED) as z:
                    z.write(file_to_zip, zip_entry_name)
            
            # Send SUCCESS so main loop counts it
            queue.put({'type': 'success', 'track': track_name})
        else:
            raise Exception("Download failed (File not created)")

    except Exception as e:
        logger.error(f"Track {track_index} failed: {e}")
        # Send FAIL so main loop counts it and doesn't hang
        queue.put({'type': 'skipped', 'track': track_name, 'reason': str(e)})
        
    finally:
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
    
    zip_locks[session_id] = BoundedSemaphore(1)
    msg_queue = Queue()
    
    ffmpeg_exe = 'ffmpeg'
    if os.path.exists('ffmpeg_bin/ffmpeg'):
        ffmpeg_exe = 'ffmpeg_bin/ffmpeg'

    pool = Pool(CONCURRENT_WORKERS)

    try:
        yield f"data: {json.dumps({'type': 'status', 'message': 'Fetching metadata...'})}\n\n"
        
        with YoutubeDL({'extract_flat': 'in_playlist', 'quiet': True, 'nocheckcertificate': True}) as ydl:
            info = ydl.extract_info(url, download=False)
            entries = list(info['entries']) if 'entries' in info else [info]
            
            valid_entries = []
            for i, e in enumerate(entries[:MAX_SONGS]):
                if e:
                    title = e.get('title', f"Track {i+1}")
                    track_url = e.get('url', url) 
                    valid_entries.append((i+1, track_url, title))
            
            total_real = len(valid_entries)

        yield f"data: {json.dumps({'type': 'total', 'total': total_real})}\n\n"

        for idx, t_url, t_title in valid_entries:
            if active_tasks.get(session_id, False): break
            pool.spawn(worker_task, t_url, session_dir, idx, ffmpeg_exe, session_id, msg_queue, zip_path, zip_locks[session_id], t_title)

        processed_count = 0
        skipped_tracks = []

        # MAIN LOOP: Must account for Success AND Skips to avoid hanging
        while processed_count < total_real:
            if active_tasks.get(session_id, False): raise Exception("USER_CANCELLED")
                
            try:
                msg = msg_queue.get(timeout=2) 
                
                if msg['type'] == 'detail':
                    # Real-time granular updates
                    yield f"data: {json.dumps(msg)}\n\n"
                    
                elif msg['type'] == 'success':
                    processed_count += 1
                    # Progress bar update
                    yield f"data: {json.dumps({'type': 'progress', 'current': processed_count, 'total': total_real, 'track': msg['track']})}\n\n"
                    
                elif msg['type'] == 'skipped':
                    processed_count += 1 # Increment anyway so we don't hang!
                    skipped_tracks.append(msg['track'])
                    yield f"data: {json.dumps({'type': 'progress', 'current': processed_count, 'total': total_real, 'track': f'Skipped: {msg['track']}'})}\n\n"
                    
            except Empty:
                if pool.free_count() == CONCURRENT_WORKERS and msg_queue.empty():
                    break
                yield ": heartbeat\n\n"

        pool.join()

        result = {
            "type": "done",
            "zipLink": f"/download/{session_id}/playlist_backup.zip",
            "total_processed": processed_count - len(skipped_tracks),
            "total_expected": total_real,
            "skipped": skipped_tracks,
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
        active_tasks.pop(session_id, None)
        zip_locks.pop(session_id, None)
        pool.kill() 
        cleanup_memory()

@app.route('/convert', methods=['POST'])
def convert_audio():
    data = request.json
    url = data.get('url', '').strip()
    session_id = data.get('session_id', str(uuid.uuid4()))
    active_tasks[session_id] = False 
    return Response(stream_with_context(generate_conversion_stream(url, session_id)), mimetype='text/event-stream')

@app.route('/download/<session_id>/<filename>')
def download_file(session_id, filename):
    file_path = os.path.join(DOWNLOAD_FOLDER, session_id, filename)
    if os.path.exists(file_path):
        return send_file(file_path, as_attachment=True)
    return "File not found", 404

if __name__ == '__main__':
    app.run(debug=False, port=5000, threaded=True)