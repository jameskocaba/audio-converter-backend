import gevent.monkey
gevent.monkey.patch_all()

import os, uuid, logging, glob, zipfile, certifi, gc, shutil, time, subprocess, math
from flask import Flask, request, send_file, jsonify
from flask_cors import CORS
from yt_dlp import YoutubeDL
import json

from gevent.pool import Pool
from gevent.lock import BoundedSemaphore
from threading import Thread
from collections import deque

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

# CONFIGURATION
MAX_SONGS = 50
AVG_TIME_PER_TRACK = 45  
PUBLIC_URL = os.environ.get('PUBLIC_URL', 'https://mp3aud.io') # Fallback if env var is missing

# GLOBAL STATE
conversion_jobs = {} 
zip_locks = {}
conversion_queue = deque() 
current_processing_session = None 

def cleanup_memory():
    gc.collect()
    gc.collect()

def cleanup_old_sessions():
    try:
        current_time = time.time()
        for session in list(conversion_jobs.keys()):
            job = conversion_jobs[session]
            if job['status'] in ['processing', 'queued']:
                continue
            if current_time - job.get('last_update', 0) > 3600:
                session_dir = os.path.join(DOWNLOAD_FOLDER, session)
                if os.path.exists(session_dir):
                    shutil.rmtree(session_dir, ignore_errors=True)
                del conversion_jobs[session]
                if session in zip_locks:
                    del zip_locks[session]
    except:
        pass

def send_email_notification(recipient, subject, html_content):
    """Sends email via Resend"""
    try:
        resend.api_key = os.environ.get('RESEND_API_KEY')
        from_email = os.environ.get('FROM_EMAIL') 
        
        if not resend.api_key or not from_email:
            logger.warning("Resend keys missing. Email not sent.")
            return

        params = {
            "from": f"MP3 Audio Tools <{from_email}>",
            "to": [recipient],
            "subject": subject,
            "html": html_content,
        }
        resend.Emails.send(params)
    except Exception as e:
        logger.error(f"Failed to send email: {e}")

def notify_user_complete(session_id, user_email, track_count):
    """Generates the email content with a robust fallback link"""
    if not user_email: return
    
    # 1. Ensure valid base URL
    base_url = os.environ.get('PUBLIC_URL')
    if not base_url:
        base_url = "https://mp3aud.io" # Hardcoded fallback
    
    base_url = base_url.rstrip('/')
    download_link = f"{base_url}/download/{session_id}/playlist_backup.zip"
    
    # 2. Log for debugging
    logger.warning(f"EMAIL DEBUG: Sending to {user_email} | Link: {download_link}")

    # 3. Email HTML with Button + Plain Text Fallback
    html = f"""
    <div style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto; padding: 20px; border: 1px solid #e2e8f0; border-radius: 8px; background-color: #ffffff;">
        <h2 style="color: #2980b9; margin-top: 0;">Your Files Are Ready</h2>
        <p style="color: #333333; font-size: 16px;">Your conversion of <strong>{track_count} media </strong> has finished processing.</p>
        
        <div style="margin: 30px 0; text-align: center;">
            <a href="{download_link}" target="_blank" style="background-color: #ea580c; color: #ffffff; padding: 14px 28px; text-decoration: none; border-radius: 6px; font-weight: bold; font-size: 16px; display: inline-block;">
                Download ZIP Archive
            </a>
        </div>

        <p style="color: #666666; font-size: 14px; margin-top: 20px;">
            If the button above doesn't work, copy and paste this link into your browser:<br>
            <a href="{download_link}" style="color: #2980b9; word-break: break-all;">{download_link}</a>
        </p>
        
        <hr style="border: 0; border-top: 1px solid #eeeeee; margin: 20px 0;">
        <p style="color: #94a3b8; font-size: 12px; text-align: center;">This link expires in 1 hour.</p>
    </div>
    """
    send_email_notification(user_email, "Your Conversion is Ready 📦", html)

def process_track(url, session_dir, track_index, ffmpeg_exe, session_id, zip_path, lock, track_name, artist_name):
    job = conversion_jobs.get(session_id)
    if not job or job.get('cancelled'): return False

    temp_filename_base = f"track_{track_index}"
    
    def cancel_hook(d):
        if job.get('cancelled'): raise Exception("CancelledByUser")

    ydl_opts = {
        'format': 'bestaudio[abr<=128]/bestaudio/best',
        'outtmpl': os.path.join(session_dir, f"{temp_filename_base}.%(ext)s"),
        'ffmpeg_location': ffmpeg_exe,
        'quiet': True, 'no_warnings': True, 'nocheckcertificate': True,
        'socket_timeout': 15, 'retries': 3,
        'progress_hooks': [cancel_hook], 'cookiefile': None,
        'http_headers': {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        },
        'postprocessors': [{'key': 'FFmpegExtractAudio','preferredcodec': 'mp3','preferredquality': '128'}],
    }

    try:
        job['current_track'] = track_index
        job['last_update'] = time.time()
        job['current_status'] = f'Processing track {track_index}...'
        
        if job.get('cancelled'): return False

        try:
            with YoutubeDL({'quiet':True, 'no_warnings':True, 'socket_timeout':10}) as ydl:
                info = ydl.extract_info(url, download=False)
                if info.get('title'): track_name = info['title']
                if info.get('uploader'): artist_name = info['uploader']
        except: pass
        
        job['current_status'] = f'Processing: {artist_name} - {track_name}'
        
        with YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
        
        mp3_files = glob.glob(os.path.join(session_dir, f"{temp_filename_base}*.mp3"))
        if mp3_files:
            file_to_zip = mp3_files[0]
            try:
                cmd = [ffmpeg_exe, '-i', file_to_zip, '-metadata', f'title={track_name}', '-metadata', f'artist={artist_name}', '-c', 'copy', '-y', file_to_zip + '.tmp']
                subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
                if os.path.exists(file_to_zip + '.tmp'): os.replace(file_to_zip + '.tmp', file_to_zip)
            except: pass

            clean_name = "".join([c for c in f"{artist_name} - {track_name}"[:100] if c.isalnum() or c in (' ', '-', '_')]).strip() or f"Track_{track_index}"
            
            with lock:
                with zipfile.ZipFile(zip_path, 'a', zipfile.ZIP_STORED) as z:
                    z.write(file_to_zip, f"{clean_name}.mp3")
            
            job['completed'] += 1
            job['completed_tracks'].append(clean_name)
            return True
    except Exception as e:
        if not job.get('cancelled'): job['skipped'] += 1
        return False
    finally:
        try:
            for f in glob.glob(os.path.join(session_dir, f"{temp_filename_base}*")):
                try: os.remove(f)
                except: pass
        except: pass
        cleanup_memory()

def run_conversion_task(session_id, url, entries, user_email=None):
    global current_processing_session
    current_processing_session = session_id
    
    job = conversion_jobs[session_id]
    session_dir = os.path.join(DOWNLOAD_FOLDER, session_id)
    os.makedirs(session_dir, exist_ok=True)
    zip_path = os.path.join(session_dir, "playlist_backup.zip")
    
    zip_locks[session_id] = BoundedSemaphore(1)
    ffmpeg_exe = 'ffmpeg_bin/ffmpeg' if os.path.exists('ffmpeg_bin/ffmpeg') else 'ffmpeg'

    try:
        job['status'] = 'processing'
        for idx, t_url, t_title, t_artist in entries:
            if job.get('cancelled'): break
            process_track(t_url, session_dir, idx, ffmpeg_exe, session_id, zip_path, zip_locks[session_id], t_title, t_artist)
            if idx % 5 == 0: cleanup_memory()

        if not job.get('cancelled'):
            job['status'] = 'completed'
            job['zip_ready'] = True
            job['zip_path'] = f"/download/{session_id}/playlist_backup.zip"
            
            # --- SEND USER NOTIFICATION ---
            if user_email:
                notify_user_complete(session_id, user_email, job['completed'])
                
            # Developer Alert (Optional)
            dev_email = os.environ.get('DEV_EMAIL')
            if dev_email:
                send_email_notification(dev_email, "Conversion Finished", f"<p>URL: {url}</p>")
                
        else:
            job['status'] = 'cancelled'

    except Exception as e:
        job['status'] = 'error'
        job['error'] = str(e)
    
    finally:
        if session_id in zip_locks: del zip_locks[session_id]
        current_processing_session = None
        cleanup_memory()

def worker_loop():
    logger.warning("Worker thread started...")
    while True:
        try:
            if conversion_queue:
                task_data = conversion_queue.popleft()
                sid = task_data['session_id']
                
                if conversion_jobs.get(sid, {}).get('cancelled'):
                    conversion_jobs[sid]['status'] = 'cancelled'
                    continue
                    
                # Pass email from queue to task runner
                run_conversion_task(sid, task_data['url'], task_data['entries'], task_data.get('email'))
            else:
                time.sleep(1)
        except Exception as e:
            logger.error(f"Worker error: {e}")
            time.sleep(1)

queue_worker = Thread(target=worker_loop, daemon=True)
queue_worker.start()

# --- ROUTES ---

@app.route('/start_conversion', methods=['POST'])
def start_conversion():
    cleanup_old_sessions()
    data = request.json
    url = data.get('url', '').strip()
    session_id = data.get('session_id', str(uuid.uuid4()))
    user_email = data.get('email', '').strip() # <--- CAPTURE EMAIL
    
    if not url: return jsonify({"error": "No URL provided"}), 400
    
    try:
        with YoutubeDL({'extract_flat': 'in_playlist', 'quiet': True}) as ydl:
            info = ydl.extract_info(url, download=False)
            entries = info.get('entries', [info]) if info else []
            
            valid_entries = []
            for i, e in enumerate(entries[:MAX_SONGS]):
                if e:
                    track_url = e.get('url') or e.get('webpage_url') or e.get('id', '')
                    if not track_url.startswith('http'): track_url = f"https://soundcloud.com/track/{e.get('id', i)}"
                    valid_entries.append((i+1, track_url, e.get('title', f"Track {i}"), e.get('uploader', 'Artist')))
            
            total_tracks = len(valid_entries)

        if total_tracks == 0: return jsonify({"error": "No tracks found."}), 400
        
        conversion_jobs[session_id] = {
            'status': 'queued', 'total': total_tracks, 'completed': 0,
            'skipped': 0, 'current_track': 0, 'completed_tracks': [],
            'skipped_tracks': [], 'cancelled': False, 'zip_ready': False,
            'last_update': time.time()
        }
        
        conversion_queue.append({
            'session_id': session_id,
            'url': url,
            'entries': valid_entries,
            'email': user_email # <--- ADD TO QUEUE
        })
        
        position = len(conversion_queue)
        
        return jsonify({
            "session_id": session_id, 
            "total_tracks": total_tracks, 
            "status": "queued",
            "queue_position": position
        }), 200
        
    except Exception as e:
        return jsonify({"error": f"Extraction failed: {str(e)}"}), 500

@app.route('/status/<session_id>', methods=['GET'])
def get_status(session_id):
    job = conversion_jobs.get(session_id)
    if not job: return jsonify({"error": "Session not found"}), 404
    
    queue_pos = 0
    wait_minutes = 0
    
    if job['status'] == 'queued':
        if current_processing_session and current_processing_session != session_id:
            curr_job = conversion_jobs.get(current_processing_session)
            if curr_job and curr_job['status'] == 'processing':
                remaining = max(0, curr_job['total'] - curr_job['completed'])
                wait_minutes += (remaining * AVG_TIME_PER_TRACK)

        for idx, item in enumerate(conversion_queue):
            if item['session_id'] == session_id:
                queue_pos = idx + 1
                break
            wait_minutes += (len(item['entries']) * AVG_TIME_PER_TRACK)
            
        wait_minutes = math.ceil(wait_minutes / 60)

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
        "queue_position": queue_pos,
        "estimated_wait": wait_minutes
    }), 200

@app.route('/cancel', methods=['POST'])
def cancel_conversion():
    data = request.json
    session_id = data.get('session_id')
    if session_id in conversion_jobs:
        job = conversion_jobs[session_id]
        job['cancelled'] = True
        if job['status'] == 'queued': job['status'] = 'cancelled'
        
        try:
            for item in list(conversion_queue):
                if item['session_id'] == session_id:
                    conversion_queue.remove(item)
                    break
        except: pass
            
        return jsonify({"status": "cancelling"}), 200
    return jsonify({"status": "not_found"}), 404

@app.route('/download/<session_id>/<filename>')
def download_file(session_id, filename):
    file_path = os.path.join(DOWNLOAD_FOLDER, session_id, filename)
    if os.path.exists(file_path):
        return send_file(file_path, as_attachment=True)
    return "File not found", 404

# --- TOP 5 CHART ROUTE ---
@app.route('/top-5')
def top_chart():
    # Placeholder for the chart page
    return """
    <div style="font-family: sans-serif; text-align: center; padding: 40px;">
        <h1>Top 5 Downloads</h1>
        <p>Chart data is accumulating...</p>
        <p><a href="/">Back to Converter</a></p>
    </div>
    """, 200

@app.route('/health')
def health():
    return jsonify({
        "status": "ok", 
        "active_jobs": len(conversion_jobs), 
        "queue_length": len(conversion_queue)
    }), 200

@app.route('/')
def index():
    return jsonify({"message": "Audio Processor API", "status": "active"}), 200

if __name__ == '__main__':
    app.run(debug=False, port=5000, threaded=True)