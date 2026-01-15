import gevent.monkey
gevent.monkey.patch_all()

import os, uuid, logging, glob, zipfile, certifi, gc, tempfile, shutil
from flask import Flask, request, send_file, jsonify, Response, stream_with_context
from flask_cors import CORS
from yt_dlp import YoutubeDL
import json

from gevent.pool import Pool
from gevent.queue import Queue, Empty
from gevent.lock import BoundedSemaphore

os.environ['SSL_CERT_FILE'] = certifi.where()
logging.basicConfig(level=logging.WARNING)  # Reduce logging overhead
logger = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}})

DOWNLOAD_FOLDER = os.path.join(os.getcwd(), 'downloads')
os.makedirs(DOWNLOAD_FOLDER, exist_ok=True)

# ULTRA-OPTIMIZED FOR RENDER FREE TIER
CONCURRENT_WORKERS = 1  # Single worker = most stable, prevents memory spikes
MAX_SONGS = 200
BATCH_SIZE = 10  # Process in batches, clean memory between batches

active_tasks = {} 
zip_locks = {}

def cleanup_memory():
    """Ultra-aggressive memory cleanup"""
    gc.collect()
    gc.collect()
    gc.collect()

def cleanup_old_sessions():
    """Remove sessions older than 30 minutes"""
    try:
        import time
        current_time = time.time()
        for session in os.listdir(DOWNLOAD_FOLDER):
            session_path = os.path.join(DOWNLOAD_FOLDER, session)
            if os.path.isdir(session_path):
                if current_time - os.path.getmtime(session_path) > 1800:  # 30 min
                    shutil.rmtree(session_path, ignore_errors=True)
    except:
        pass

@app.route('/cancel', methods=['POST'])
def cancel_conversion():
    data = request.json
    session_id = data.get('session_id')
    if session_id and session_id in active_tasks:
        active_tasks[session_id] = True 
        return jsonify({"status": "cancelling"}), 200
    return jsonify({"status": "not_found"}), 404

def worker_task(url, session_dir, track_index, ffmpeg_exe, session_id, queue, zip_path, lock, track_name, artist_name):
    if active_tasks.get(session_id, False): 
        return

    temp_filename_base = f"track_{track_index}"
    
    # MINIMAL OPTIONS - Speed over quality
    ydl_opts = {
        'format': 'http-128',  # SoundCloud specific, fastest
        'outtmpl': os.path.join(session_dir, f"{temp_filename_base}.%(ext)s"),
        'ffmpeg_location': ffmpeg_exe,
        'quiet': True,
        'no_warnings': True,
        'nocheckcertificate': True,
        'geo_bypass': True,
        'socket_timeout': 30,
        'retries': 2,
        'fragment_retries': 2,
        'postprocessors': [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '128',
        }],
    }

    try:
        queue.put({'type': 'detail', 'current': track_index, 'status': 'Downloading...'})
        
        # Download with metadata extraction
        with YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            actual_title = info.get('title', track_name)
            actual_artist = info.get('uploader', info.get('artist', artist_name))
            track_name = actual_title
            artist_name = actual_artist

        if active_tasks.get(session_id, False): 
            return

        mp3_files = glob.glob(os.path.join(session_dir, f"{temp_filename_base}*.mp3"))
        
        if mp3_files:
            queue.put({'type': 'detail', 'current': track_index, 'status': 'Tagging...'})
            file_to_zip = mp3_files[0]
            
            # Quick metadata injection - no temp file
            import subprocess
            try:
                subprocess.run([
                    ffmpeg_exe, '-i', file_to_zip,
                    '-metadata', f'title={track_name}',
                    '-metadata', f'artist={artist_name}',
                    '-c', 'copy', '-y',
                    file_to_zip + '.tmp'
                ], check=True, capture_output=True, timeout=15, stderr=subprocess.DEVNULL)
                
                os.replace(file_to_zip + '.tmp', file_to_zip)
            except:
                if os.path.exists(file_to_zip + '.tmp'):
                    os.remove(file_to_zip + '.tmp')
            
            # Clean filename
            clean_artist = "".join([c for c in artist_name[:50] if c.isalnum() or c in (' ', '-', '_')]).strip()
            clean_track = "".join([c for c in track_name[:80] if c.isalnum() or c in (' ', '-', '_')]).strip()
            
            if clean_artist and clean_track:
                zip_entry_name = f"{clean_artist} - {clean_track}.mp3"
            elif clean_track:
                zip_entry_name = f"{clean_track}.mp3"
            else:
                zip_entry_name = f"Track_{track_index}.mp3"

            with lock:
                with zipfile.ZipFile(zip_path, 'a', zipfile.ZIP_STORED) as z:  # No compression = faster
                    z.write(file_to_zip, zip_entry_name)
            
            queue.put({'type': 'success', 'track': f"{artist_name} - {track_name}"})
        else:
            raise Exception("Download failed")

    except Exception as e:
        logger.error(f"Track {track_index} failed: {e}")
        queue.put({'type': 'skipped', 'track': f"{artist_name} - {track_name}", 'reason': str(e)})
        
    finally:
        # Immediate cleanup
        try:
            for f in glob.glob(os.path.join(session_dir, f"{temp_filename_base}*")):
                try:
                    os.remove(f)
                except:
                    pass
        except:
            pass
        cleanup_memory()

def generate_conversion_stream(url, session_id):
    cleanup_old_sessions()
    
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
        yield f"data: {json.dumps({'type': 'status', 'message': 'Fetching playlist...'})}\n\n"
        
        # Fast flat extraction
        with YoutubeDL({
            'extract_flat': True,
            'quiet': True, 
            'no_warnings': True,
        }) as ydl:
            info = ydl.extract_info(url, download=False)
            entries = info.get('entries', [info]) if info else []
            
            valid_entries = []
            for i, e in enumerate(entries[:MAX_SONGS]):
                if e and not active_tasks.get(session_id, False):
                    track_url = e.get('url') or e.get('webpage_url') or e.get('id', '')
                    if not track_url.startswith('http'):
                        track_url = f"https://soundcloud.com/track/{e.get('id', i)}"
                    
                    title = e.get('title') or f"Track {i+1}"
                    artist = e.get('uploader') or e.get('creator') or 'Unknown'
                    valid_entries.append((i+1, track_url, title, artist))
            
            total_real = len(valid_entries)

        if total_real == 0:
            yield f"data: {json.dumps({'type': 'error', 'message': 'No tracks found'})}\n\n"
            return

        yield f"data: {json.dumps({'type': 'total', 'total': total_real})}\n\n"

        # Process in small batches to control memory
        processed_count = 0
        skipped_tracks = []
        
        for batch_start in range(0, total_real, BATCH_SIZE):
            batch_end = min(batch_start + BATCH_SIZE, total_real)
            batch = valid_entries[batch_start:batch_end]
            
            # Spawn batch
            for idx, t_url, t_title, t_artist in batch:
                if active_tasks.get(session_id, False): 
                    break
                pool.spawn(worker_task, t_url, session_dir, idx, ffmpeg_exe, session_id, msg_queue, zip_path, zip_locks[session_id], t_title, t_artist)

            # Wait for batch to complete
            batch_processed = 0
            while batch_processed < len(batch):
                if active_tasks.get(session_id, False): 
                    raise Exception("USER_CANCELLED")
                    
                try:
                    msg = msg_queue.get(timeout=5)
                    
                    if msg['type'] == 'detail':
                        yield f"data: {json.dumps(msg)}\n\n"
                        
                    elif msg['type'] == 'success':
                        batch_processed += 1
                        processed_count += 1
                        yield f"data: {json.dumps({'type': 'progress', 'current': processed_count, 'total': total_real, 'track': msg['track']})}\n\n"
                        
                    elif msg['type'] == 'skipped':
                        batch_processed += 1
                        processed_count += 1
                        skipped_tracks.append(msg['track'])
                        yield f"data: {json.dumps({'type': 'progress', 'current': processed_count, 'total': total_real, 'track': f'Skipped: {msg["track"]}'})}\n\n"
                        
                except Empty:
                    if pool.free_count() == CONCURRENT_WORKERS:
                        break
                    yield ": heartbeat\n\n"

            # Clean memory after each batch
            pool.join()
            cleanup_memory()
            
            # Check if cancelled
            if active_tasks.get(session_id, False):
                break

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
            yield f"data: {json.dumps({'type': 'error', 'message': str(e)})}\n\n"
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

@app.route('/health')
def health():
    return jsonify({"status": "ok"}), 200

if __name__ == '__main__':
    app.run(debug=False, port=5000, threaded=True)