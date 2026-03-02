import gevent.monkey
gevent.monkey.patch_all()

import os, uuid, logging, glob, zipfile, certifi, gc, shutil, time, subprocess, math, tempfile
from flask import Flask, request, send_file, jsonify
from flask_cors import CORS
import requests 
import json

from gevent.pool import Pool
from gevent.lock import BoundedSemaphore
from threading import Thread
from collections import deque

import resend
from openai import OpenAI

# PDF Generation Imports
from reportlab.lib.pagesizes import letter
from reportlab.platypus import SimpleDocTemplate, Paragraph
from reportlab.lib.styles import getSampleStyleSheet
from xhtml2pdf import pisa

os.environ['SSL_CERT_FILE'] = certifi.where()
os.environ['REQUESTS_CA_BUNDLE'] = certifi.where()
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

# Initialize OpenAI Client
try:
    client = OpenAI()
except Exception as e:
    logger.warning(f"OpenAI client could not be initialized. Check OPENAI_API_KEY: {e}")
    client = None

# CONFIGURATION
MAX_SONGS = 50
AVG_TIME_PER_TRACK = 45  
PUBLIC_URL = os.environ.get('PUBLIC_URL', 'https://mp3aud.io')

# --- RAPIDAPI CONFIGURATION ---
# The .strip() prevents hidden spaces or line breaks from crashing the headers
RAPIDAPI_KEY = os.environ.get('RAPIDAPI_KEY', '').strip() 
RAPIDAPI_HOST = os.environ.get('RAPIDAPI_HOST', 'youtube138.p.rapidapi.com').strip() 

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

def notify_user_complete(session_id, user_email, track_count, html_summaries=""):
    if not user_email: return
    
    base_url = os.environ.get('PUBLIC_URL', "https://mp3aud.io").rstrip('/')
    download_link = f"{base_url}/download/{session_id}/playlist_backup.zip"
    
    manuals_section = ""
    if html_summaries:
        manuals_section = f"""
        <div style="margin-top: 30px; padding: 20px; background-color: #f8fafc; border-radius: 8px; border: 1px solid #e2e8f0; color: #333333; line-height: 1.6;">
            {html_summaries}
        </div>
        """

    html = f"""
    <div style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto; padding: 20px; border: 1px solid #e2e8f0; border-radius: 8px; background-color: #ffffff;">
        <h2 style="color: #2980b9; margin-top: 0;">Your Files Are Ready</h2>
        <p style="color: #333333; font-size: 16px;">Your conversion of <strong>{track_count} media file(s)</strong> has finished processing.</p>
        
        {manuals_section}
        
        <div style="margin: 30px 0; text-align: center;">
            <a href="{download_link}" target="_blank" style="background-color: #ea580c; color: #ffffff; padding: 14px 28px; text-decoration: none; border-radius: 6px; font-weight: bold; font-size: 16px; display: inline-block;">
                Download ZIP Archive
            </a>
        </div>
    </div>
    """
    send_email_notification(user_email, "Your Conversion is Ready 📦", html)

# --- AI Transcription Pipeline ---
def transcribe_audio_file(mp3_file_path, job=None):
    if not client: return None, None
    try:
        temp_dir = tempfile.mkdtemp()
        chunk_pattern = os.path.join(temp_dir, "chunk_%03d.mp3")
        ffmpeg_exe = 'ffmpeg_bin/ffmpeg' if os.path.exists('ffmpeg_bin/ffmpeg') else 'ffmpeg'
        
        if job:
            job['current_status'] = 'Slicing audio for AI analysis...'
            job['sub_progress'] = 0

        cmd = [
            ffmpeg_exe, '-y', '-i', mp3_file_path,
            '-f', 'segment', '-segment_time', '900',
            '-c', 'copy', chunk_pattern
        ]
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        
        chunks = sorted(glob.glob(os.path.join(temp_dir, "chunk_*.mp3")))
        total_chunks = len(chunks)
        full_transcript = ""
        
        for i, chunk_path in enumerate(chunks):
            if job:
                job['current_status'] = f'Transcribing audio (Part {i+1} of {total_chunks})...'
                job['sub_progress'] = int((i / total_chunks) * 100)
                
            try:
                with open(chunk_path, "rb") as audio_file:
                    transcript = client.audio.transcriptions.create(
                        model="whisper-1", file=audio_file
                    )
                full_transcript += transcript.text + " "
            except Exception as e:
                logger.error(f"Failed to transcribe chunk: {e}")
                full_transcript += f"\n[Warning: AI transcription failed for this segment.]\n"
        
        if job: job['sub_progress'] = 100
        shutil.rmtree(temp_dir, ignore_errors=True)
                
        text_file_path = mp3_file_path.replace('.mp3', '.txt')
        with open(text_file_path, "w", encoding="utf-8") as text_file:
            text_file.write(full_transcript.strip()) 
            
        pdf_file_path = mp3_file_path.replace('.mp3', '.pdf')
        try:
            doc = SimpleDocTemplate(pdf_file_path, pagesize=letter)
            styles = getSampleStyleSheet()
            formatted_text = full_transcript.strip().replace('\n', '<br/>')
            doc.build([Paragraph(formatted_text, styles["Normal"])])
        except Exception as e:
            pdf_file_path = None
            
        return text_file_path, pdf_file_path
    except Exception as e:
        return None, None

def generate_diy_manual(transcript_text_path, job=None):
    if not client: return None, None, None
    try:
        if job:
            job['current_status'] = 'Formatting AI summary...'
            job['sub_progress'] = 0

        with open(transcript_text_path, "r", encoding="utf-8") as file:
            transcript = file.read()[:100000] 
        
        system_prompt = """
        You are an expert technical writer. Take this transcript and format it in clean basic HTML (no markdown).
        If it's DIY: 1. Overview 2. Tools 3. Steps 4. Warnings.
        If it's a Meeting: 1. Overview 2. Discussion Points 3. Decisions 4. Action Items.
        Use <h3>, <p>, <ul>/<li>, <ol>/<li>.
        """

        response = client.chat.completions.create(
            model="gpt-4o", 
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": f"Here is the raw transcript:\n\n{transcript}"}
            ],
            temperature=0.2 
        )
        
        if job: job['sub_progress'] = 100

        manual_html = response.choices[0].message.content
        manual_path = transcript_text_path.replace('.txt', '_summary.html')
        pdf_path = transcript_text_path.replace('.txt', '_summary.pdf')
        
        with open(manual_path, "w", encoding="utf-8") as f: f.write(manual_html)
            
        try:
            with open(pdf_path, "w+b") as result_file:
                pisa.CreatePDF(manual_html, dest=result_file)
        except Exception:
            pdf_path = None
            
        return manual_path, pdf_path, manual_html
    except Exception as e:
        return None, None, None

# --- NEW RAPIDAPI HELPER FUNCTION ---
def fetch_media_from_api(url):
    """Sends the ID to the youtube138 API service to get direct media links."""
    if not RAPIDAPI_KEY:
        raise Exception("RAPIDAPI_KEY environment variable is missing.")
        
    api_endpoint = f"https://{RAPIDAPI_HOST}/video/details/" 
    
    headers = {
        "X-RapidAPI-Key": RAPIDAPI_KEY,
        "X-RapidAPI-Host": RAPIDAPI_HOST
    }
    querystring = {"id": url} # Uses 'id' parameter as expected by the youtube138 API
    
    try:
        response = requests.get(api_endpoint, headers=headers, params=querystring, timeout=15)
        response.raise_for_status()
        return response.json()
    except Exception as e:
        logger.error(f"API Fetch failed: {e}")
        return None

# ---------------------------------

def process_track(direct_url, session_dir, track_index, ffmpeg_exe, session_id, zip_path, lock, track_name, artist_name, thumbnail, start_time, end_time, transcribe_audio):
    job = conversion_jobs.get(session_id)
    if not job or job.get('cancelled'): return False

    temp_filename_base = f"track_{track_index}"
    raw_file_path = os.path.join(session_dir, f"{temp_filename_base}_raw.tmp")
    final_mp3_path = os.path.join(session_dir, f"{temp_filename_base}.mp3")

    try:
        job['current_track'] = track_index
        job['last_update'] = time.time()
        job['current_status'] = f'Downloading direct media...'
        job['sub_progress'] = 0
        job['current_thumbnail'] = thumbnail 
        
        # 1. Download the raw file directly via Requests
        with requests.get(direct_url, stream=True, timeout=30) as r:
            r.raise_for_status()
            total_size = int(r.headers.get('content-length', 0))
            downloaded = 0
            
            with open(raw_file_path, 'wb') as f:
                for chunk in r.iter_content(chunk_size=8192):
                    if job.get('cancelled'): raise Exception("CancelledByUser")
                    if chunk:
                        f.write(chunk)
                        downloaded += len(chunk)
                        if total_size > 0:
                            job['sub_progress'] = int((downloaded / total_size) * 100)

        job['current_status'] = 'Extracting and formatting audio...'
        
        # 2. Convert to standardized MP3 using FFmpeg
        ffmpeg_args = [ffmpeg_exe, '-y', '-i', raw_file_path]
        
        if start_time: ffmpeg_args.extend(['-ss', str(start_time)])
        if end_time: ffmpeg_args.extend(['-to', str(end_time)])
            
        ffmpeg_args.extend([
            '-map_metadata', '-1',
            '-metadata', f'title={track_name}',
            '-metadata', f'artist={artist_name}',
            '-vn', # Ensure no video is kept
            '-c:a', 'libmp3lame', '-b:a', '128k',
            final_mp3_path
        ])
        
        subprocess.run(ffmpeg_args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        
        clean_name = "".join([c for c in f"{artist_name} - {track_name}"[:100] if c.isalnum() or c in (' ', '-', '_')]).strip() or f"Track_{track_index}"
        
        # 3. Zip the file
        with lock:
            with zipfile.ZipFile(zip_path, 'a', zipfile.ZIP_STORED) as z:
                z.write(final_mp3_path, f"{clean_name}.mp3")
        
        # 4. Transcribe (if requested)
        if transcribe_audio:
            raw_txt_path, raw_pdf_path = transcribe_audio_file(final_mp3_path, job)
            if raw_txt_path:
                html_path, summary_pdf_path, manual_html = generate_diy_manual(raw_txt_path, job)
                with lock:
                    with zipfile.ZipFile(zip_path, 'a', zipfile.ZIP_STORED) as z:
                        if raw_pdf_path and os.path.exists(raw_pdf_path): z.write(raw_pdf_path, f"{clean_name}_raw_transcript.pdf")
                        else: z.write(raw_txt_path, f"{clean_name}_raw_transcript.txt")
                            
                        if summary_pdf_path and os.path.exists(summary_pdf_path): z.write(summary_pdf_path, f"{clean_name}_summary.pdf")
                        elif html_path and os.path.exists(html_path): z.write(html_path, f"{clean_name}_summary.html")

                if manual_html: job['email_summaries'] += f"<hr><h2>{clean_name}</h2>" + manual_html
                else: job['email_summaries'] += f"<hr><h2>{clean_name}</h2><p><em>Notice: AI summarization failed.</em></p>"
            else:
                job['email_summaries'] += f"<hr><h2>{clean_name}</h2><p><em>Notice: Audio transcription failed.</em></p>"

        job['completed'] += 1
        job['sub_progress'] = 100
        job['completed_tracks'].append(clean_name)
        return True
        
    except Exception as e:
        logger.error(f"Process track error: {e}")
        if not job.get('cancelled'): job['skipped'] += 1
        return False
    finally:
        try:
            for f in glob.glob(os.path.join(session_dir, f"{temp_filename_base}*")):
                try: os.remove(f)
                except: pass
        except: pass
        cleanup_memory()

def run_conversion_task(session_id, url, entries, user_email=None, start_time=None, end_time=None, transcribe_audio=False):
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
        for idx, t_url, t_title, t_artist, t_thumb in entries:
            if job.get('cancelled'): break
            process_track(t_url, session_dir, idx, ffmpeg_exe, session_id, zip_path, zip_locks[session_id], t_title, t_artist, t_thumb, start_time, end_time, transcribe_audio)
            if idx % 5 == 0: cleanup_memory()

        if not job.get('cancelled'):
            job['status'] = 'completed'
            job['zip_ready'] = True
            job['zip_path'] = f"/download/{session_id}/playlist_backup.zip"
            if user_email: notify_user_complete(session_id, user_email, job['completed'], job.get('email_summaries', ''))
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
                run_conversion_task(sid, task_data['url'], task_data['entries'], task_data.get('email'), task_data.get('start_time'), task_data.get('end_time'), task_data.get('transcribe_audio'))
            else:
                time.sleep(1)
        except Exception as e:
            time.sleep(1)

queue_worker = Thread(target=worker_loop, daemon=True)
queue_worker.start()

@app.route('/start_conversion', methods=['POST'])
def start_conversion():
    cleanup_old_sessions()
    data = request.json
    url = data.get('url', '').strip()
    session_id = data.get('session_id', str(uuid.uuid4()))
    user_email = data.get('email', '').strip() 
    start_time = data.get('start_time', '').strip()
    end_time = data.get('end_time', '').strip()
    transcribe_audio = data.get('transcribe_audio', False) 
    
    if not url: return jsonify({"error": "No URL provided"}), 400
    
    try:
        # Fetch metadata and download links from RapidAPI
        api_data = fetch_media_from_api(url)
        
        if not api_data:
            return jsonify({"error": "Failed to extract media. Please check the URL."}), 400
            
        # Parse the JSON response. 
        # *NOTE*: We will likely need to adjust these keys once you get a successful response back from youtube138
        title = api_data.get('title', 'Extracted Track')
        artist = api_data.get('author', 'YouTube Video')
        thumbnail = api_data.get('thumbnail', '')
        
        direct_media_url = None
        if 'url' in api_data and api_data['url'].startswith('http'):
            direct_media_url = api_data['url']
        elif 'links' in api_data and len(api_data['links']) > 0:
            direct_media_url = api_data['links'][0].get('link')

        if not direct_media_url:
            return jsonify({"error": "API did not return a valid download link."}), 400

        valid_entries = [(1, direct_media_url, title, artist, thumbnail)]
        total_tracks = 1 

        conversion_jobs[session_id] = {
            'status': 'queued', 'total': total_tracks, 'completed': 0,
            'skipped': 0, 'current_track': 0, 'completed_tracks': [],
            'skipped_tracks': [], 'cancelled': False, 'zip_ready': False,
            'current_thumbnail': thumbnail,
            'last_update': time.time(),
            'email_summaries': '',
            'sub_progress': 0 
        }
        
        conversion_queue.append({
            'session_id': session_id, 'url': url, 'entries': valid_entries,
            'email': user_email, 'start_time': start_time if start_time else None,
            'end_time': end_time if end_time else None, 'transcribe_audio': transcribe_audio
        })
        
        return jsonify({"session_id": session_id, "total_tracks": total_tracks, "status": "queued", "queue_position": len(conversion_queue)}), 200
        
    except Exception as e:
        logger.error(f"Extraction failed: {str(e)}") 
        return jsonify({"error": "An error occurred during extraction."}), 400

@app.route('/status/<session_id>', methods=['GET'])
def get_status(session_id):
    job = conversion_jobs.get(session_id)
    if not job: return jsonify({"error": "Session not found"}), 404
    
    queue_pos = 0
    wait_minutes = 0
    if job['status'] == 'queued':
        for idx, item in enumerate(conversion_queue):
            if item['session_id'] == session_id:
                queue_pos = idx + 1
                break
    return jsonify({
        "status": job['status'], "total": job['total'], "completed": job['completed'],
        "skipped": job['skipped'], "current_track": job['current_track'],
        "current_status": job.get('current_status', ''), "current_thumbnail": job.get('current_thumbnail', ''),
        "zip_ready": job.get('zip_ready', False), "zip_path": job.get('zip_path', ''), 
        "skipped_tracks": job['skipped_tracks'], "sub_progress": job.get('sub_progress', 0),
        "queue_position": queue_pos, "estimated_wait": wait_minutes
    }), 200

@app.route('/cancel', methods=['POST'])
def cancel_conversion():
    session_id = request.json.get('session_id')
    if session_id in conversion_jobs:
        conversion_jobs[session_id]['cancelled'] = True
        return jsonify({"status": "cancelling"}), 200
    return jsonify({"status": "not_found"}), 404

@app.route('/download/<session_id>/<filename>')
def download_file(session_id, filename):
    file_path = os.path.join(DOWNLOAD_FOLDER, session_id, filename)
    if os.path.exists(file_path): return send_file(file_path, as_attachment=True)
    return "File not found", 404

@app.route('/health')
def health():
    return jsonify({"status": "ok", "active_jobs": len(conversion_jobs), "queue_length": len(conversion_queue)}), 200

@app.route('/')
def index():
    return jsonify({"message": "Audio Processor API", "status": "active"}), 200

if __name__ == '__main__':
    app.run(debug=False, port=5000, threaded=True)