import gevent.monkey
gevent.monkey.patch_all()

import os, uuid, logging, glob, zipfile, certifi, gc, shutil, time
from flask import Flask, request, send_file, jsonify
from flask_cors import CORS
from yt_dlp import YoutubeDL
import json

from gevent.pool import Pool
from gevent.lock import BoundedSemaphore
from threading import Thread

os.environ['SSL_CERT_FILE'] = certifi.where()
logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app, resources={
    r"/*": {
        "origins": "*",
        "methods": ["GET", "POST", "OPTIONS"],
        "allow_headers": ["Content-Type"]
    }
})

DOWNLOAD_FOLDER = os.path.join(os.getcwd(), 'downloads')
os.makedirs(DOWNLOAD_FOLDER, exist_ok=True)

# OPTION 4: Safe batching configuration
BATCH_SIZE = 2  # Process 2 tracks simultaneously (can try 3 if stable)
MAX_SONGS = 500

# GLOBAL STATE - Persistent across requests
conversion_jobs = {}  # {session_id: {status, progress, tracks, etc}}
zip_locks = {}

def cleanup_memory():
    gc.collect()
    gc.collect()

def cleanup_old_sessions():
    try:
        current_time = time.time()
        for session in list(conversion_jobs.keys()):
            job = conversion_jobs[session]
            if current_time - job.get('last_update', 0) > 3600:  # 1 hour
                session_dir = os.path.join(DOWNLOAD_FOLDER, session)
                if os.path.exists(session_dir):
                    shutil.rmtree(session_dir, ignore_errors=True)
                del conversion_jobs[session]
                if session in zip_locks:
                    del zip_locks[session]
    except:
        pass

def process_track_isolated(url, session_id, track_index, ffmpeg_exe, zip_path, lock, track_name, artist_name):
    """
    OPTION 4: Process track in ISOLATED directory to prevent file conflicts.
    
    KEY STABILITY FEATURE: Each track gets its own workspace directory.
    This eliminates all file naming conflicts that cause multi-threading crashes.
    """
    job = conversion_jobs.get(session_id)
    if not job or job.get('cancelled'):
        return False

    # CRITICAL: Isolated directory per track prevents conflicts
    session_dir = os.path.join(DOWNLOAD_FOLDER, session_id)
    track_temp_dir = os.path.join(session_dir, f"temp_{track_index}")
    
    # Create isolated workspace
    os.makedirs(track_temp_dir, exist_ok=True)
    
    # Simple filename since it's isolated (no conflicts possible)
    temp_filename = "audio"
    
    # Standard yt-dlp settings
    ydl_opts = {
        'format': 'bestaudio[abr<=128]/bestaudio/best',
        'outtmpl': os.path.join(track_temp_dir, f"{temp_filename}.%(ext)s"),
        'ffmpeg_location': ffmpeg_exe,
        'quiet': True,
        'no_warnings': True,
        'nocheckcertificate': True,
        'socket_timeout': 15,
        'retries': 1,
        'http_chunk_size': 1048576,
        'postprocessors': [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '2',  # VBR quality 2 ≈ 190kbps
        }],
        'overwrites': True,
        'continuedl': False,
        'noprogress': True,
    }

    try:
        # Update job status (thread-safe via GIL)
        job['current_track'] = track_index
        job['current_status'] = f'Downloading track {track_index}...'
        job['last_update'] = time.time()
        
        # Download in isolated directory
        with YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])

        if job.get('cancelled'):
            return False

        # Find MP3 in isolated directory (only one file possible)
        mp3_files = glob.glob(os.path.join(track_temp_dir, "*.mp3"))
        
        if mp3_files:
            file_to_zip = mp3_files[0]
            
            # Create clean filename with artist and track info
            clean_artist = "".join([c for c in artist_name[:50] if c.isalnum() or c in (' ', '-', '_')]).strip()
            clean_track = "".join([c for c in track_name[:80] if c.isalnum() or c in (' ', '-', '_')]).strip()
            
            if clean_artist and clean_track:
                zip_entry_name = f"{clean_artist} - {clean_track}.mp3"
            elif clean_track:
                zip_entry_name = f"{clean_track}.mp3"
            else:
                zip_entry_name = f"Track_{track_index}.mp3"

            # THREAD-SAFE: Lock protects ZIP file from concurrent writes
            with lock:
                with zipfile.ZipFile(zip_path, 'a', zipfile.ZIP_DEFLATED, compresslevel=1) as z:
                    z.write(file_to_zip, zip_entry_name)
            
            # Update job status (thread-safe)
            job['completed'] += 1
            job['completed_tracks'].append(f"{artist_name} - {track_name}")
            job['last_update'] = time.time()
            return True
        else:
            raise Exception("Download failed")

    except Exception as e:
        logger.error(f"Track {track_index} failed: {e}")
        job['skipped'] += 1
        job['skipped_tracks'].append(f"{artist_name} - {track_name}")
        job['last_update'] = time.time()
        return False
        
    finally:
        # CRITICAL: Cleanup isolated directory immediately
        # This frees memory before next batch starts
        try:
            shutil.rmtree(track_temp_dir, ignore_errors=True)
        except Exception as e:
            logger.warning(f"Failed to cleanup temp dir {track_temp_dir}: {e}")

def background_conversion(session_id, url, entries):
    """
    OPTION 4: Background conversion with SAFE 2-track batching.
    
    Uses gevent for cooperative multitasking (not OS threads).
    Gevent is lighter on memory than threading.Thread.
    """
    job = conversion_jobs[session_id]
    session_dir = os.path.join(DOWNLOAD_FOLDER, session_id)
    os.makedirs(session_dir, exist_ok=True)
    zip_path = os.path.join(session_dir, "playlist_backup.zip")
    
    # Pre-create empty ZIP file
    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED, compresslevel=1) as z:
        pass
    
    # Thread-safe lock for ZIP operations
    zip_locks[session_id] = BoundedSemaphore(1)
    
    ffmpeg_exe = 'ffmpeg'
    if os.path.exists('ffmpeg_bin/ffmpeg'):
        ffmpeg_exe = 'ffmpeg_bin/ffmpeg'

    try:
        job['status'] = 'processing'
        
        # OPTION 4: Controlled batching with gevent Pool
        # Pool limits concurrent greenlets to BATCH_SIZE
        pool = Pool(BATCH_SIZE)
        
        # Process in batches
        for i in range(0, len(entries), BATCH_SIZE):
            if job.get('cancelled'):
                break
            
            batch = entries[i:i+BATCH_SIZE]
            greenlets = []
            
            # Spawn greenlets for batch (cooperative multitasking)
            for idx, t_url, t_title, t_artist in batch:
                g = pool.spawn(
                    process_track_isolated,
                    t_url, session_id, idx, ffmpeg_exe,
                    zip_path, zip_locks[session_id], t_title, t_artist
                )
                greenlets.append(g)
            
            # CRITICAL: Wait for ALL tracks in batch to complete
            # before starting next batch (prevents memory overflow)
            gevent.joinall(greenlets)
            
            # Memory cleanup after every batch
            if i % 4 == 0:  # Every 2 batches (4 tracks)
                cleanup_memory()

        # Mark as complete
        if not job.get('cancelled'):
            job['status'] = 'completed'
            job['zip_ready'] = True
            job['zip_path'] = f"/download/{session_id}/playlist_backup.zip"
        else:
            job['status'] = 'cancelled'
            
        job['last_update'] = time.time()

    except Exception as e:
        logger.error(f"Background conversion error: {e}")
        job['status'] = 'error'
        job['error'] = str(e)
        job['last_update'] = time.time()
    
    finally:
        if session_id in zip_locks:
            del zip_locks[session_id]
        cleanup_memory()

@app.route('/start_conversion', methods=['POST'])
def start_conversion():
    """Start conversion in background, return immediately"""
    cleanup_old_sessions()
    
    data = request.json
    url = data.get('url', '').strip()
    session_id = data.get('session_id', str(uuid.uuid4()))
    
    if not url:
        return jsonify({"error": "No URL provided"}), 400
    
    try:
        # Quick metadata extraction
        with YoutubeDL({
            'extract_flat': True,
            'quiet': True,
            'no_warnings': True,
        }) as ydl:
            info = ydl.extract_info(url, download=False)
            entries = info.get('entries', [info]) if info else []
            
            valid_entries = []
            for i, e in enumerate(entries[:MAX_SONGS]):
                if e:
                    track_url = e.get('url') or e.get('webpage_url') or e.get('id', '')
                    if not track_url.startswith('http'):
                        track_url = f"https://soundcloud.com/track/{e.get('id', i)}"
                    
                    # Get artist and title from playlist metadata
                    title = e.get('title') or f"Track {i+1}"
                    artist = e.get('uploader') or e.get('creator') or 'Unknown'
                    valid_entries.append((i+1, track_url, title, artist))
            
            total_tracks = len(valid_entries)
        
        if total_tracks == 0:
            return jsonify({"error": "No tracks found"}), 400
        
        # Initialize job
        conversion_jobs[session_id] = {
            'status': 'starting',
            'total': total_tracks,
            'completed': 0,
            'skipped': 0,
            'current_track': 0,
            'current_status': 'Starting...',
            'completed_tracks': [],
            'skipped_tracks': [],
            'cancelled': False,
            'zip_ready': False,
            'last_update': time.time()
        }
        
        # Start background thread
        thread = Thread(target=background_conversion, args=(session_id, url, valid_entries))
        thread.daemon = True
        thread.start()
        
        return jsonify({
            "session_id": session_id,
            "total_tracks": total_tracks,
            "status": "started"
        }), 200
        
    except Exception as e:
        logger.error(f"Start conversion error: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/status/<session_id>', methods=['GET'])
def get_status(session_id):
    """Poll for conversion status"""
    job = conversion_jobs.get(session_id)
    
    if not job:
        return jsonify({"error": "Session not found"}), 404
    
    return jsonify({
        "status": job['status'],
        "total": job['total'],
        "completed": job['completed'],
        "skipped": job['skipped'],
        "current_track": job['current_track'],
        "current_status": job['current_status'],
        "zip_ready": job.get('zip_ready', False),
        "zip_path": job.get('zip_path', ''),
        "skipped_tracks": job['skipped_tracks']
    }), 200

@app.route('/cancel', methods=['POST'])
def cancel_conversion():
    data = request.json
    session_id = data.get('session_id')
    
    if session_id and session_id in conversion_jobs:
        conversion_jobs[session_id]['cancelled'] = True
        conversion_jobs[session_id]['status'] = 'cancelled'
        return jsonify({"status": "cancelling"}), 200
    
    return jsonify({"status": "not_found"}), 404

@app.route('/download/<session_id>/<filename>')
def download_file(session_id, filename):
    file_path = os.path.join(DOWNLOAD_FOLDER, session_id, filename)
    if os.path.exists(file_path):
        return send_file(file_path, as_attachment=True)
    return "File not found", 404

@app.route('/health')
def health():
    return jsonify({
        "status": "ok",
        "active_jobs": len(conversion_jobs),
        "batch_size": BATCH_SIZE,
        "message": "Server is running - OPTION 4: Safe Batching"
    }), 200

@app.route('/')
def index():
    return jsonify({
        "message": "SoundCloud Converter API - OPTION 4: Safe 2-Track Batching",
        "endpoints": ["/start_conversion", "/status/<id>", "/cancel", "/download/<id>/<file>", "/health"],
        "optimizations": [
            f"✅ {BATCH_SIZE}-track parallel processing (gevent)",
            "✅ Isolated temp directories (prevents conflicts)",
            "✅ Thread-safe ZIP operations (BoundedSemaphore)",
            "✅ Batch synchronization (completes batch before next)",
            "✅ VBR quality 2 (~190kbps)",
            "✅ Memory cleanup every 2 batches",
            "✅ Cooperative multitasking (lighter than threads)"
        ],
        "stability_features": [
            "🔒 Each track has isolated workspace (temp_1/, temp_2/, etc.)",
            "🔒 No file naming conflicts possible",
            "🔒 Semaphore lock prevents concurrent ZIP writes",
            "🔒 Batch-by-batch processing prevents memory spikes",
            "🔒 Immediate cleanup of temp directories",
            "🔒 Gevent cooperative scheduling (not OS threads)"
        ],
        "expected_performance": {
            "per_track": "6-10 seconds (2 tracks in parallel)",
            "100_tracks": "10-16 minutes (vs 25-33 min single-threaded)",
            "500_tracks": "50-85 minutes (vs 2-2.7 hours single-threaded)"
        },
        "tuning": {
            "current_batch_size": BATCH_SIZE,
            "recommendation": "Start with 2, increase to 3 if stable after testing"
        }
    }), 200

if __name__ == '__main__':
    app.run(debug=False, port=5000, threaded=True)