import gevent.monkey
gevent.monkey.patch_all()

import os, uuid, logging, glob, zipfile, certifi, gc, shutil, time, subprocess
from flask import Flask, request, send_file, jsonify
from flask_cors import CORS
from yt_dlp import YoutubeDL
import json
from collections import deque
from datetime import datetime

from gevent.pool import Pool
from gevent.lock import BoundedSemaphore
from threading import Thread, Lock

import resend

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

MAX_SONGS = 100

# QUEUE SYSTEM
conversion_queue = deque()  # Queue of (session_id, url, entries, timestamp)
queue_lock = Lock()
active_conversion = None  # Currently processing session_id
conversion_jobs = {}
zip_locks = {}

def cleanup_memory():
    gc.collect()
    gc.collect()

def cleanup_old_sessions():
    try:
        current_time = time.time()
        for session in list(conversion_jobs.keys()):
            job = conversion_jobs[session]
            if current_time - job.get('last_update', 0) > 3600:
                session_dir = os.path.join(DOWNLOAD_FOLDER, session)
                if os.path.exists(session_dir):
                    shutil.rmtree(session_dir, ignore_errors=True)
                del conversion_jobs[session]
                if session in zip_locks:
                    del zip_locks[session]
    except:
        pass

def send_developer_alert(subject, html_content):
    """Sends an email notification to the developer via Resend API"""
    try:
        resend.api_key = os.environ.get('RESEND_API_KEY')
        from_email = os.environ.get('FROM_EMAIL') 
        dev_email = os.environ.get('DEV_EMAIL')   
        
        if not resend.api_key or not from_email or not dev_email:
            logger.warning("Missing Resend env vars. Developer alert skipped.")
            return

        params = {
            "from": f"Converter Alert <{from_email}>",
            "to": [dev_email],
            "subject": f"[App Update] {subject}",
            "html": html_content,
        }
        resend.Emails.send(params)
    except Exception as e:
        logger.error(f"Failed to send Resend alert: {e}")

def process_track(url, session_dir, track_index, ffmpeg_exe, session_id, zip_path, lock, track_name, artist_name):
    """Process a single track with granular cancellation checks"""
    job = conversion_jobs.get(session_id)
    if not job or job.get('cancelled'):
        return False

    temp_filename_base = f"track_{track_index}"
    
    def cancel_hook(d):
        if job.get('cancelled'):
            raise Exception("CancelledByUser")

    ydl_opts = {
        'format': 'bestaudio[abr<=128]/bestaudio/best',
        'outtmpl': os.path.join(session_dir, f"{temp_filename_base}.%(ext)s"),
        'ffmpeg_location': ffmpeg_exe,
        'quiet': True,
        'no_warnings': True,
        'nocheckcertificate': True,
        'socket_timeout': 10,
        'retries': 2,
        'progress_hooks': [cancel_hook],
        'postprocessors': [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '128',
        }],
    }

    try:
        job['current_track'] = track_index
        job['last_update'] = time.time()
        
        job['current_status'] = f'🔍 Getting info for track {track_index}...'
        if job.get('cancelled'): return False

        try:
            with YoutubeDL({'quiet': True, 'no_warnings': True, 'socket_timeout': 5}) as ydl:
                info = ydl.extract_info(url, download=False)
                preview_title = info.get('title', track_name)
                preview_artist = info.get('uploader') or info.get('artist') or artist_name
                
                if preview_title: track_name = preview_title
                if preview_artist and not preview_artist.startswith('user-'):
                    artist_name = preview_artist
                
                if (not artist_name or artist_name.startswith('user-') or artist_name in ['Unknown Artist', 'Unknown', '']):
                    if ' - ' in track_name:
                        parts = track_name.split(' - ', 1)
                        if len(parts) == 2:
                            artist_name = parts[0].strip()
                            track_name = parts[1].strip()
        except:
            pass
        
        if job.get('cancelled'): return False
        
        job['current_status'] = f'⬇️ Downloading: {artist_name} - {track_name}'
        job['last_update'] = time.time()
        
        with YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
        
        if job.get('cancelled'):
            job['current_status'] = '⛔ Conversion cancelled'
            return False

        mp3_files = glob.glob(os.path.join(session_dir, f"{temp_filename_base}*.mp3"))
        
        if mp3_files:
            job['current_status'] = f'🏷️ Adding metadata: {artist_name} - {track_name}'
            job['last_update'] = time.time()
            
            file_to_zip = mp3_files[0]
            
            try:
                if job.get('cancelled'): raise Exception("CancelledByUser")
                
                cmd = [
                    ffmpeg_exe, '-i', file_to_zip,
                    '-metadata', f'title={track_name}',
                    '-metadata', f'artist={artist_name}',
                    '-c', 'copy', '-y',
                    file_to_zip + '.tmp'
                ]
                
                proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                
                while proc.poll() is None:
                    if job.get('cancelled'):
                        proc.terminate()
                        raise Exception("CancelledByUser")
                    time.sleep(0.1)
                
                if proc.returncode == 0:
                    os.replace(file_to_zip + '.tmp', file_to_zip)
                else:
                    if os.path.exists(file_to_zip + '.tmp'): os.remove(file_to_zip + '.tmp')

            except Exception as e:
                if "CancelledByUser" in str(e): raise e
                if os.path.exists(file_to_zip + '.tmp'): os.remove(file_to_zip + '.tmp')
            
            clean_artist = "".join([c for c in artist_name[:50] if c.isalnum() or c in (' ', '-', '_')]).strip()
            clean_track = "".join([c for c in track_name[:80] if c.isalnum() or c in (' ', '-', '_')]).strip()
            
            if clean_artist and clean_track:
                zip_entry_name = f"{clean_artist} - {clean_track}.mp3"
            elif clean_track:
                zip_entry_name = f"{clean_track}.mp3"
            else:
                zip_entry_name = f"Track_{track_index}.mp3"

            job['current_status'] = f'📦 Adding to ZIP: {artist_name} - {track_name}'
            job['last_update'] = time.time()

            if job.get('cancelled'): raise Exception("CancelledByUser")

            with lock:
                with zipfile.ZipFile(zip_path, 'a', zipfile.ZIP_STORED) as z:
                    z.write(file_to_zip, zip_entry_name)
            
            job['current_status'] = f'✅ Completed: {artist_name} - {track_name}'
            job['completed'] += 1
            job['completed_tracks'].append(f"{artist_name} - {track_name}")
            job['last_update'] = time.time()
            return True
        else:
            raise Exception("Download failed")

    except Exception as e:
        if "CancelledByUser" in str(e) or job.get('cancelled'):
            job['current_status'] = '⛔ Conversion cancelled'
            return False
            
        logger.error(f"Track {track_index} failed: {e}")
        job['current_status'] = f'❌ Failed: {artist_name} - {track_name}'
        job['skipped'] += 1
        job['skipped_tracks'].append(f"{artist_name} - {track_name}")
        job['last_update'] = time.time()
        return False
        
    finally:
        try:
            for f in glob.glob(os.path.join(session_dir, f"{temp_filename_base}*")):
                try: os.remove(f)
                except: pass
        except: pass
        cleanup_memory()

def background_conversion(session_id, url, entries):
    """Background thread for conversion"""
    global active_conversion
    
    job = conversion_jobs[session_id]
    session_dir = os.path.join(DOWNLOAD_FOLDER, session_id)
    os.makedirs(session_dir, exist_ok=True)
    zip_path = os.path.join(session_dir, "playlist_backup.zip")
    
    zip_locks[session_id] = BoundedSemaphore(1)
    
    ffmpeg_exe = 'ffmpeg'
    if os.path.exists('ffmpeg_bin/ffmpeg'):
        ffmpeg_exe = 'ffmpeg_bin/ffmpeg'

    try:
        job['status'] = 'processing'
        total_tracks = len(entries)
        job['current_status'] = f'🚀 Starting conversion of {total_tracks} tracks...'
        
        for idx, t_url, t_title, t_artist in entries:
            if job.get('cancelled'):
                break
            
            process_track(
                t_url, session_dir, idx, ffmpeg_exe, session_id, 
                zip_path, zip_locks[session_id], t_title, t_artist
            )
            
            if idx % 5 == 0:
                cleanup_memory()

        if not job.get('cancelled'):
            job['status'] = 'completed'
            job['zip_ready'] = True
            job['zip_path'] = f"/download/{session_id}/playlist_backup.zip"
            
            status_msg = f'🎉 All done! {job["completed"]} tracks converted.'
            job['current_status'] = status_msg
            
            send_developer_alert(
                "Conversion Success ✅", 
                f"<p>Playlist conversion finished!</p><p>URL: {url}</p><p>Tracks: {job['completed']}/{total_tracks}</p>"
            )
        else:
            job['status'] = 'cancelled'
            job['current_status'] = '⛔ Conversion cancelled by user'

    except Exception as e:
        logger.error(f"Background conversion error: {e}")
        job['status'] = 'error'
        job['error'] = str(e)
        
        send_developer_alert(
            "Conversion Error ❌", 
            f"<p>Error converting playlist: {url}</p><p>Details: {str(e)}</p>"
        )
    
    finally:
        if session_id in zip_locks:
            del zip_locks[session_id]
        cleanup_memory()
        
        # Process next in queue
        with queue_lock:
            active_conversion = None
            process_next_in_queue()

def process_next_in_queue():
    """Process the next conversion in queue (must be called with queue_lock held)"""
    global active_conversion
    
    if active_conversion is not None:
        return  # Already processing
    
    if not conversion_queue:
        return  # Queue is empty
    
    # Get next job
    session_id, url, entries = conversion_queue.popleft()
    active_conversion = session_id
    
    # Start processing
    thread = Thread(target=background_conversion, args=(session_id, url, entries))
    thread.daemon = True
    thread.start()

@app.route('/start_conversion', methods=['POST'])
def start_conversion():
    cleanup_old_sessions()
    data = request.json
    url = data.get('url', '').strip()
    session_id = data.get('session_id', str(uuid.uuid4()))
    
    if not url:
        return jsonify({"error": "No URL provided"}), 400
    
    try:
        with YoutubeDL({'extract_flat': 'in_playlist', 'quiet': True}) as ydl:
            info = ydl.extract_info(url, download=False)
            entries = info.get('entries', [info]) if info else []
            
            valid_entries = []
            for i, e in enumerate(entries[:MAX_SONGS]):
                if e:
                    track_url = e.get('url') or e.get('webpage_url') or e.get('id', '')
                    if not track_url.startswith('http'):
                        track_url = f"https://soundcloud.com/track/{e.get('id', i)}"
                    
                    title = e.get('title') or f"Track {i+1}"
                    artist = e.get('uploader') or 'Unknown Artist'
                    valid_entries.append((i+1, track_url, title, artist))
            
            total_tracks = len(valid_entries)
        
        if total_tracks == 0:
            return jsonify({"error": "No tracks found"}), 400
        
        # Initialize job
        conversion_jobs[session_id] = {
            'status': 'queued',
            'total': total_tracks,
            'completed': 0,
            'skipped': 0,
            'current_track': 0,
            'completed_tracks': [],
            'skipped_tracks': [],
            'cancelled': False,
            'zip_ready': False,
            'last_update': time.time(),
            'queued_at': datetime.now().isoformat()
        }
        
        # Add to queue
        with queue_lock:
            conversion_queue.append((session_id, url, valid_entries))
            queue_position = len(conversion_queue)
            
            # Start processing if nothing is active
            if active_conversion is None:
                process_next_in_queue()
                queue_position = 0  # Started immediately
        
        return jsonify({
            "session_id": session_id,
            "total_tracks": total_tracks,
            "status": "queued" if queue_position > 0 else "started",
            "queue_position": queue_position
        }), 200
        
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/status/<session_id>', methods=['GET'])
def get_status(session_id):
    job = conversion_jobs.get(session_id)
    if not job:
        return jsonify({"error": "Session not found"}), 404
    
    # Calculate queue position
    queue_position = 0
    with queue_lock:
        if job['status'] == 'queued':
            for i, (sid, _, _) in enumerate(conversion_queue):
                if sid == session_id:
                    queue_position = i + 1
                    break
    
    return jsonify({
        "status": job['status'],
        "total": job['total'],
        "completed": job['completed'],
        "skipped": job['skipped'],
        "current_track": job['current_track'],
        "current_status": job.get('current_status', ''),
        "zip_ready": job.get('zip_ready', False),
        "zip_path": job.get('zip_path', ''),
        "skipped_tracks": job['skipped_tracks'],
        "queue_position": queue_position
    }), 200

@app.route('/cancel', methods=['POST'])
def cancel_conversion():
    data = request.json
    session_id = data.get('session_id')
    
    if session_id not in conversion_jobs:
        return jsonify({"status": "not_found"}), 404
    
    job = conversion_jobs[session_id]
    
    # If queued, remove from queue
    if job['status'] == 'queued':
        with queue_lock:
            conversion_queue_list = list(conversion_queue)
            conversion_queue.clear()
            for sid, url, entries in conversion_queue_list:
                if sid != session_id:
                    conversion_queue.append((sid, url, entries))
        
        job['status'] = 'cancelled'
        job['current_status'] = '⛔ Removed from queue'
    else:
        # Mark as cancelled (will stop during processing)
        job['cancelled'] = True
    
    return jsonify({"status": "cancelling"}), 200

@app.route('/queue', methods=['GET'])
def get_queue_info():
    """Get current queue information"""
    with queue_lock:
        queue_info = []
        for i, (sid, url, entries) in enumerate(conversion_queue):
            job = conversion_jobs.get(sid, {})
            queue_info.append({
                "session_id": sid,
                "position": i + 1,
                "total_tracks": job.get('total', len(entries)),
                "queued_at": job.get('queued_at', '')
            })
        
        return jsonify({
            "active_conversion": active_conversion,
            "queue_length": len(conversion_queue),
            "queue": queue_info
        }), 200

@app.route('/download/<session_id>/<filename>')
def download_file(session_id, filename):
    file_path = os.path.join(DOWNLOAD_FOLDER, session_id, filename)
    if os.path.exists(file_path):
        return send_file(file_path, as_attachment=True)
    return "File not found", 404

@app.route('/health')
def health():
    with queue_lock:
        queue_len = len(conversion_queue)
    return jsonify({
        "status": "ok",
        "active_jobs": len(conversion_jobs),
        "queue_length": queue_len,
        "active_conversion": active_conversion is not None
    }), 200

@app.route('/')
def index():
    return jsonify({"message": "SoundCloud Converter API", "status": "active"}), 200

if __name__ == '__main__':
    app.run(debug=False, port=5000, threaded=True)