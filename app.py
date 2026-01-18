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

# OPTIMIZED FOR RENDER FREE TIER
CONCURRENT_WORKERS = 1
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

def process_track(url, session_dir, track_index, ffmpeg_exe, session_id, zip_path, lock, track_name, artist_name):
    """Process a single track with clear status updates"""
    job = conversion_jobs.get(session_id)
    if not job or job.get('cancelled'):
        return False

    temp_filename_base = f"track_{track_index}"
    
    ydl_opts = {
        'format': 'bestaudio[abr<=128]/bestaudio/best',
        'outtmpl': os.path.join(session_dir, f"{temp_filename_base}.%(ext)s"),
        'ffmpeg_location': ffmpeg_exe,
        'quiet': True,
        'no_warnings': True,
        'nocheckcertificate': True,
        'socket_timeout': 20,
        'retries': 1,
        'http_chunk_size': 1048576,
        'postprocessors': [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '128',
        }],
    }

    try:
        # Update track number first
        job['current_track'] = track_index
        job['last_update'] = time.time()
        
        # CLEAR STATUS: Initial downloading message (generic until we get real metadata)
        job['current_status'] = f'⬇️ Downloading track {track_index}...'
        job['last_update'] = time.time()
        
        # DEBUG: Log the metadata we're starting with
        logger.warning(f"Processing track {track_index}: Initial Artist='{artist_name}', Title='{track_name}'")
        
        with YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            # Get better metadata from actual download if available
            actual_title = info.get('title', '')
            actual_artist = info.get('uploader') or info.get('artist') or info.get('creator') or ''
            
            # DEBUG: Log what download gave us
            logger.warning(f"Download gave us - Title: '{actual_title}', Artist: '{actual_artist}'")
            
            # AGGRESSIVE PARSING from downloaded track title
            if actual_title and ' - ' in actual_title:
                parts = actual_title.split(' - ', 1)
                if len(parts) == 2:
                    potential_artist = parts[0].strip()
                    potential_title = parts[1].strip()
                    
                    # Use parsed values if current artist is generic or matches uploader
                    if (not actual_artist or 
                        actual_artist in ['Unknown', 'Unknown Artist', 'NA', ''] or
                        actual_artist.startswith('user-') or
                        actual_artist == actual_title):
                        actual_artist = potential_artist
                        actual_title = potential_title
                        logger.warning(f"PARSED from download - Artist: '{actual_artist}', Title: '{actual_title}'")
            
            # Update our working variables with better info
            if actual_title and actual_title not in ['NA', '']:
                track_name = actual_title
            if actual_artist and actual_artist not in ['Unknown', 'Unknown Artist', 'NA', ''] and not actual_artist.startswith('user-'):
                artist_name = actual_artist
            
            # Final check: if artist is STILL generic after all parsing attempts
            if artist_name in ['Unknown Artist', 'Unknown', ''] or artist_name.startswith('user-'):
                # Last ditch effort: parse from current track_name
                if ' - ' in track_name:
                    parts = track_name.split(' - ', 1)
                    if len(parts) == 2:
                        artist_name = parts[0].strip()
                        track_name = parts[1].strip()
                        logger.warning(f"FINAL PARSE - Artist: '{artist_name}', Title: '{track_name}'")
            
            del info
        
        # FINAL METADATA: Now we have the real artist and title
        logger.warning(f"FINAL metadata for track {track_index}: Artist='{artist_name}', Title='{track_name}'")
        
        # Update status with ACTUAL artist/song name
        job['current_status'] = f'⬇️ Downloaded: {artist_name} - {track_name}'
        job['last_update'] = time.time()

        if job.get('cancelled'):
            job['current_status'] = '⛔ Conversion cancelled by user'
            return False

        mp3_files = glob.glob(os.path.join(session_dir, f"{temp_filename_base}*.mp3"))
        
        if mp3_files:
            # CLEAR STATUS: Adding metadata stage
            job['current_status'] = f'🏷️ Adding metadata: {artist_name} - {track_name}'
            job['last_update'] = time.time()
            
            file_to_zip = mp3_files[0]
            
            import subprocess
            try:
                subprocess.run([
                    ffmpeg_exe, '-i', file_to_zip,
                    '-metadata', f'title={track_name}',
                    '-metadata', f'artist={artist_name}',
                    '-c', 'copy', '-y',
                    file_to_zip + '.tmp'
                ], check=True, capture_output=True, timeout=10, stderr=subprocess.DEVNULL)
                os.replace(file_to_zip + '.tmp', file_to_zip)
            except:
                if os.path.exists(file_to_zip + '.tmp'):
                    os.remove(file_to_zip + '.tmp')
            
            clean_artist = "".join([c for c in artist_name[:50] if c.isalnum() or c in (' ', '-', '_')]).strip()
            clean_track = "".join([c for c in track_name[:80] if c.isalnum() or c in (' ', '-', '_')]).strip()
            
            if clean_artist and clean_track:
                zip_entry_name = f"{clean_artist} - {clean_track}.mp3"
            elif clean_track:
                zip_entry_name = f"{clean_track}.mp3"
            else:
                zip_entry_name = f"Track_{track_index}.mp3"

            # CLEAR STATUS: Adding to ZIP stage
            job['current_status'] = f'📦 Adding to ZIP: {artist_name} - {track_name}'
            job['last_update'] = time.time()

            with lock:
                with zipfile.ZipFile(zip_path, 'a', zipfile.ZIP_STORED) as z:
                    z.write(file_to_zip, zip_entry_name)
            
            # CLEAR STATUS: Track completed
            job['current_status'] = f'✅ Completed: {artist_name} - {track_name}'
            job['completed'] += 1
            job['completed_tracks'].append(f"{artist_name} - {track_name}")
            job['last_update'] = time.time()
            return True
        else:
            raise Exception("Download failed")

    except Exception as e:
        # CLEAR ERROR STATUS
        logger.error(f"Track {track_index} failed: {e}")
        job['current_status'] = f'❌ Failed: {artist_name} - {track_name}'
        job['skipped'] += 1
        job['skipped_tracks'].append(f"{artist_name} - {track_name}")
        job['last_update'] = time.time()
        return False
        
    finally:
        try:
            for f in glob.glob(os.path.join(session_dir, f"{temp_filename_base}*")):
                try:
                    os.remove(f)
                except:
                    pass
        except:
            pass
        cleanup_memory()

def background_conversion(session_id, url, entries):
    """Background thread for conversion with progress updates"""
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
        
        # CLEAR STATUS: Starting conversion
        job['current_status'] = f'🚀 Starting conversion of {total_tracks} tracks...'
        job['last_update'] = time.time()
        
        for idx, t_url, t_title, t_artist in entries:
            if job.get('cancelled'):
                job['current_status'] = '⛔ Conversion stopped by user'
                break
            
            # DON'T overwrite status here - let process_track handle it
            # Just update the current track number for progress tracking
            job['current_track'] = idx
            
            success = process_track(
                t_url, session_dir, idx, ffmpeg_exe, session_id, 
                zip_path, zip_locks[session_id], t_title, t_artist
            )
            
            # Clean memory every 5 tracks
            if idx % 5 == 0:
                cleanup_memory()
                cleanup_memory()

        # Mark as complete
        if not job.get('cancelled'):
            job['status'] = 'completed'
            job['zip_ready'] = True
            job['zip_path'] = f"/download/{session_id}/playlist_backup.zip"
            job['current_status'] = f'🎉 All done! {job["completed"]} tracks converted successfully'
            if job['skipped'] > 0:
                job['current_status'] += f' ({job["skipped"]} unavailable)'
        else:
            job['status'] = 'cancelled'
            job['current_status'] = f'⛔ Conversion stopped. {job["completed"]} tracks completed before cancellation'
            
        job['last_update'] = time.time()

    except Exception as e:
        logger.error(f"Background conversion error: {e}")
        job['status'] = 'error'
        job['error'] = str(e)
        job['current_status'] = f'❌ Error occurred: {str(e)[:100]}'
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
                    
                    # Get raw metadata from playlist
                    raw_title = e.get('title') or e.get('track') or f"Track {i+1}"
                    raw_artist = e.get('uploader') or e.get('artist') or e.get('creator') or e.get('channel') or ''
                    
                    # DEBUG: Log EVERYTHING we're getting from SoundCloud
                    logger.warning(f"Track {i+1} RAW from SoundCloud:")
                    logger.warning(f"  title: '{e.get('title')}'")
                    logger.warning(f"  track: '{e.get('track')}'")
                    logger.warning(f"  uploader: '{e.get('uploader')}'")
                    logger.warning(f"  artist: '{e.get('artist')}'")
                    logger.warning(f"  creator: '{e.get('creator')}'")
                    logger.warning(f"  Raw Title used: '{raw_title}'")
                    logger.warning(f"  Raw Artist used: '{raw_artist}'")
                    
                    # Initialize with raw values
                    title = raw_title
                    artist = raw_artist
                    
                    # AGGRESSIVE PARSING: Always parse if title contains " - "
                    # This handles cases where uploader name is generic like "user-841173538"
                    if ' - ' in title:
                        parts = title.split(' - ', 1)
                        if len(parts) == 2:
                            potential_artist = parts[0].strip()
                            potential_title = parts[1].strip()
                            
                            logger.warning(f"  Found ' - ' separator!")
                            logger.warning(f"  Potential Artist: '{potential_artist}'")
                            logger.warning(f"  Potential Title: '{potential_title}'")
                            
                            # Use parsed artist if:
                            # 1. We have no artist, OR
                            # 2. Artist is generic (Unknown, user-XXXXX pattern), OR
                            # 3. Artist is same as title (SoundCloud bug)
                            if (not artist or 
                                artist in ['Unknown Artist', 'Unknown', ''] or
                                artist.startswith('user-') or
                                artist == raw_title):
                                artist = potential_artist
                                title = potential_title
                                logger.warning(f"  ✅ USING PARSED - Artist: '{artist}', Title: '{title}'")
                            else:
                                logger.warning(f"  ❌ NOT using parsed, keeping original artist: '{artist}'")
                    else:
                        logger.warning(f"  No ' - ' separator found in title")
                    
                    # Final cleanup: if artist is still generic, mark as Unknown
                    if not artist or artist.strip() == '' or artist.startswith('user-'):
                        artist = 'Unknown Artist'
                        logger.warning(f"  Final Artist is generic, set to 'Unknown Artist'")
                    
                    logger.warning(f"Track {i+1} FINAL - Artist: '{artist}', Title: '{title}'")
                    logger.warning(f"---")
                    
                    valid_entries.append((i+1, track_url, title, artist))
            
            total_tracks = len(valid_entries)
        
        if total_tracks == 0:
            return jsonify({"error": "No tracks found"}), 400
        
        # Initialize job with clear starting message
        conversion_jobs[session_id] = {
            'status': 'starting',
            'total': total_tracks,
            'completed': 0,
            'skipped': 0,
            'current_track': 0,
            'current_status': f'🔍 Found {total_tracks} tracks. Preparing to convert...',
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
        conversion_jobs[session_id]['current_status'] = '⛔ Cancelling conversion...'
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
        "message": "Server is running"
    }), 200

@app.route('/debug/<session_id>')
def debug_session(session_id):
    """Debug endpoint to see what metadata was extracted"""
    job = conversion_jobs.get(session_id)
    if not job:
        return jsonify({"error": "Session not found"}), 404
    
    return jsonify({
        "session_id": session_id,
        "status": job['status'],
        "total": job['total'],
        "completed_tracks": job.get('completed_tracks', []),
        "skipped_tracks": job.get('skipped_tracks', []),
        "current_status": job.get('current_status', ''),
    }), 200

@app.route('/')
def index():
    return jsonify({
        "message": "SoundCloud Converter API - Enhanced User Messaging",
        "endpoints": ["/start_conversion", "/status/<id>", "/cancel", "/download/<id>/<file>", "/health"],
        "status_emojis": {
            "🔍": "Scanning playlist",
            "🚀": "Starting conversion",
            "⏳": "Overall progress",
            "⬇️": "Downloading from SoundCloud",
            "🏷️": "Adding metadata tags",
            "📦": "Adding to ZIP file",
            "✅": "Track completed",
            "❌": "Track failed",
            "🎉": "All tracks done",
            "⛔": "Cancelled by user"
        }
    }), 200

if __name__ == '__main__':
    app.run(debug=False, port=5000, threaded=True)