import gevent.monkey
gevent.monkey.patch_all()

import os, uuid, logging, glob, zipfile, certifi, gc, shutil, time, subprocess
from flask import Flask, request, send_file, jsonify
from flask_cors import CORS
from yt_dlp import YoutubeDL
import json

from gevent.pool import Pool
from gevent.lock import BoundedSemaphore
from threading import Thread

# NEW: Resend Import
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

# OPTIMIZED FOR RENDER FREE TIER
# Keep at 1 to prevent OOM kills, but we optimized the single worker speed
CONCURRENT_WORKERS = 1 
MAX_SONGS = 500

# GLOBAL STATE
conversion_jobs = {} 
zip_locks = {}

def cleanup_memory():
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

def send_developer_alert(subject, html_content):
    """Sends an email notification to the developer via Resend API"""
    try:
        resend.api_key = os.environ.get('RESEND_API_KEY')
        from_email = os.environ.get('FROM_EMAIL') 
        dev_email = os.environ.get('DEV_EMAIL')   
        
        if not resend.api_key or not from_email or not dev_email:
            # logger.warning("Missing Resend env vars. Developer alert skipped.")
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
    """Process a single track with optimized single-pass conversion"""
    job = conversion_jobs.get(session_id)
    if not job or job.get('cancelled'):
        return False

    temp_filename_base = f"track_{track_index}"
    final_mp3_path = os.path.join(session_dir, f"{temp_filename_base}.mp3")
    
    # 1. Hook to interrupt download loop
    def cancel_hook(d):
        if job.get('cancelled'):
            raise Exception("CancelledByUser")

    # OPTIMIZATION: Merge Metadata injection into the download/convert pass
    # This prevents running FFmpeg twice (saving CPU/RAM)
    ydl_opts = {
        'format': 'bestaudio/best',
        'outtmpl': os.path.join(session_dir, f"{temp_filename_base}.%(ext)s"),
        'ffmpeg_location': ffmpeg_exe,
        'quiet': True,
        'no_warnings': True,
        'nocheckcertificate': True,
        'writethumbnail': False, # Save bandwidth
        'socket_timeout': 10,
        'retries': 2,
        'progress_hooks': [cancel_hook],
        'postprocessors': [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '128',
        }],
        # Inject metadata directly during conversion
        'postprocessor_args': [
            '-metadata', f'title={track_name}',
            '-metadata', f'artist={artist_name}',
            '-metadata', 'album=SoundCloud Backup'
        ]
    }

    try:
        job['current_track'] = track_index
        job['last_update'] = time.time()
        
        # Combined Status: Removed separate 'Getting info' step for speed
        job['current_status'] = f'⬇️ Processing: {artist_name} - {track_name}'
        
        if job.get('cancelled'): return False

        # --- PHASE 1: DOWNLOAD & CONVERT & TAG (All in one) ---
        with YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
        
        # --- PHASE 2: ZIPPING ---
        if job.get('cancelled'):
            job['current_status'] = '⛔ Conversion cancelled'
            return False

        if os.path.exists(final_mp3_path):
            # Clean filename for the ZIP entry
            clean_artist = "".join([c for c in artist_name[:50] if c.isalnum() or c in (' ', '-', '_')]).strip()
            clean_track = "".join([c for c in track_name[:80] if c.isalnum() or c in (' ', '-', '_')]).strip()
            
            if clean_artist and clean_track:
                zip_entry_name = f"{clean_artist} - {clean_track}.mp3"
            elif clean_track:
                zip_entry_name = f"{clean_track}.mp3"
            else:
                zip_entry_name = f"Track_{track_index}.mp3"

            job['current_status'] = f'📦 Adding to ZIP: {zip_entry_name}'
            job['last_update'] = time.time()

            with lock:
                with zipfile.ZipFile(zip_path, 'a', zipfile.ZIP_STORED) as z:
                    z.write(final_mp3_path, zip_entry_name)
            
            job['current_status'] = f'✅ Completed: {artist_name} - {track_name}'
            job['completed'] += 1
            job['completed_tracks'].append(f"{artist_name} - {track_name}")
            job['last_update'] = time.time()
            
            # Clean up the individual MP3 immediately to save disk space
            try: os.remove(final_mp3_path)
            except: pass
            
            return True
        else:
            raise Exception("Download failed - File not created")

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
        cleanup_memory()

def background_conversion(session_id, url, entries):
    """Background thread for conversion with Resend notifications"""
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
            # Check cancel at start of loop
            if job.get('cancelled'):
                break
            
            process_track(
                t_url, session_dir, idx, ffmpeg_exe, session_id, 
                zip_path, zip_locks[session_id], t_title, t_artist
            )
            
            # Frequent GC for low RAM environments
            if idx % 3 == 0:
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

@app.route('/start_conversion', methods=['POST'])
def start_conversion():
    cleanup_old_sessions()
    data = request.json
    url = data.get('url', '').strip()
    session_id = data.get('session_id', str(uuid.uuid4()))
    
    if not url:
        return jsonify({"error": "No URL provided"}), 400
    
    try:
        # Fast extraction (flat=in_playlist)
        with YoutubeDL({'extract_flat': 'in_playlist', 'quiet': True}) as ydl:
            info = ydl.extract_info(url, download=False)
            entries = info.get('entries', [info]) if info else []
            
            valid_entries = []
            for i, e in enumerate(entries[:MAX_SONGS]):
                if e:
                    track_url = e.get('url') or e.get('webpage_url') or e.get('id', '')
                    if not track_url.startswith('http'):
                        track_url = f"https://soundcloud.com/track/{e.get('id', i)}"
                    
                    # Pre-clean metadata here so we don't need to fetch it again later
                    title = e.get('title') or f"Track {i+1}"
                    artist = e.get('uploader') or 'Unknown Artist'
                    
                    # Basic cleanup for artist/title
                    if (not artist or artist.startswith('user-') or artist in ['Unknown Artist', 'Unknown', '']):
                        if ' - ' in title:
                            parts = title.split(' - ', 1)
                            if len(parts) == 2:
                                artist = parts[0].strip()
                                title = parts[1].strip()

                    valid_entries.append((i+1, track_url, title, artist))
            
            total_tracks = len(valid_entries)
        
        if total_tracks == 0:
            return jsonify({"error": "No tracks found"}), 400
        
        conversion_jobs[session_id] = {
            'status': 'starting', 'total': total_tracks, 'completed': 0,
            'skipped': 0, 'current_track': 0, 'completed_tracks': [],
            'skipped_tracks': [], 'cancelled': False, 'zip_ready': False,
            'last_update': time.time()
        }
        
        thread = Thread(target=background_conversion, args=(session_id, url, valid_entries))
        thread.daemon = True
        thread.start()
        
        return jsonify({"session_id": session_id, "total_tracks": total_tracks, "status": "started"}), 200
        
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/status/<session_id>', methods=['GET'])
def get_status(session_id):
    job = conversion_jobs.get(session_id)
    if not job: return jsonify({"error": "Session not found"}), 404
    return jsonify({
        "status": job['status'], "total": job['total'], "completed": job['completed'],
        "skipped": job['skipped'], "current_track": job['current_track'],
        "current_status": job.get('current_status', ''), "zip_ready": job.get('zip_ready', False),
        "zip_path": job.get('zip_path', ''), "skipped_tracks": job['skipped_tracks']
    }), 200

@app.route('/cancel', methods=['POST'])
def cancel_conversion():
    data = request.json
    session_id = data.get('session_id')
    if session_id in conversion_jobs:
        conversion_jobs[session_id]['cancelled'] = True
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
    return jsonify({"status": "ok", "active_jobs": len(conversion_jobs)}), 200

@app.route('/')
def index():
    return jsonify({"message": "SoundCloud Converter API", "status": "active"}), 200

if __name__ == '__main__':
    app.run(debug=False, port=5000, threaded=True)