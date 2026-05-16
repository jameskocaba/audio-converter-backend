import gevent.monkey
gevent.monkey.patch_all()

import os, uuid, logging, glob, zipfile, certifi, gc, shutil, time, subprocess, math, tempfile
from flask import Flask, request, send_file, jsonify
from flask_cors import CORS
from yt_dlp import YoutubeDL
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
MAX_SONGS = 10
AVG_TIME_PER_TRACK = 45  
PUBLIC_URL = os.environ.get('PUBLIC_URL', 'https://mp3aud.io')

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
    
    base_url = os.environ.get('PUBLIC_URL')
    if not base_url:
        base_url = "https://mp3aud.io" 
    
    base_url = base_url.rstrip('/')
    download_link = f"{base_url}/download/{session_id}/playlist_backup.zip"
    
    logger.warning(f"EMAIL DEBUG: Sending to {user_email} | Link: {download_link}")

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

        <p style="color: #666666; font-size: 14px; margin-top: 20px;">
            If the button above doesn't work, copy and paste this link into your browser:<br>
            <a href="{download_link}" style="color: #2980b9; word-break: break-all;">{download_link}</a>
        </p>
        
        <hr style="border: 0; border-top: 1px solid #eeeeee; margin: 20px 0;">
        <p style="color: #94a3b8; font-size: 12px; text-align: center;">This link expires in 1 hour.</p>
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
                        model="whisper-1",
                        file=audio_file
                    )
                full_transcript += transcript.text + " "
            except Exception as e:
                logger.error(f"Failed to transcribe chunk: {e}")
                full_transcript += f"\n[Warning: AI transcription failed for this segment.]\n"
        
        if job:
            job['sub_progress'] = 100

        shutil.rmtree(temp_dir, ignore_errors=True)
                
        text_file_path = mp3_file_path.replace('.mp3', '.txt')
        with open(text_file_path, "w", encoding="utf-8") as text_file:
            text_file.write(full_transcript.strip()) 
            
        pdf_file_path = mp3_file_path.replace('.mp3', '.pdf')
        try:
            doc = SimpleDocTemplate(pdf_file_path, pagesize=letter)
            styles = getSampleStyleSheet()
            style = styles["Normal"]
            
            formatted_text = full_transcript.strip().replace('\n', '<br/>')
            story = [Paragraph(formatted_text, style)]
            doc.build(story)
        except Exception as e:
            logger.error(f"Failed to generate raw transcript PDF: {e}")
            pdf_file_path = None
            
        return text_file_path, pdf_file_path
    except Exception as e:
        logger.error(f"Transcription process failed: {e}")
        return None, None

def generate_diy_manual(transcript_text_path, job=None):
    if not client: return None, None, None
    try:
        if job:
            job['current_status'] = 'Formatting AI summary...'
            job['sub_progress'] = 0

        with open(transcript_text_path, "r", encoding="utf-8") as file:
            transcript = file.read()
            
        transcript = transcript[:100000] 
        
        system_prompt = """
        You are an expert technical writer and executive assistant. Your task is to take a raw, unstructured audio transcript and intelligently determine if it is a "DIY/Instructional Video" or a "Professional Meeting/Discussion". Based on your assessment, format the text into a highly detailed, comprehensive document using the appropriate structure below.
        
        CRUCIAL DIRECTIVE: Be EXHAUSTIVE and METICULOUS. Do not omit minor steps, nuances, arguments, or specifics. If measurements, specific tool brands, names, or exact figures are mentioned, you MUST include them. Use detailed paragraphs and sub-bullets to capture the full depth of the content.

        IF THE TRANSCRIPT IS A DIY/INSTRUCTIONAL VIDEO:
        1. Comprehensive Project Overview (Detailed description, context, estimated time, difficulty, and the ultimate end-goal)
        2. Exhaustive Tools & Materials List (Extract every single piece of hardware, tool, software, or supply mentioned, including exact specs, measurements, or brands if available)
        3. Granular Step-by-Step Instructions (Chronological, highly detailed actionable steps. Break complex actions into sub-steps. Explain the 'why' behind the actions if the speaker mentions it)
        4. Safety Warnings, Troubleshooting & Pro Tips (Highlight crucial warnings, hazards, common pitfalls to avoid, and expert advice)
        
        IF THE TRANSCRIPT IS A PROFESSIONAL MEETING/DISCUSSION:
        1. Detailed Meeting Overview (Date/Time/Participants, Main Objective, and the overall context of the meeting)
        2. In-Depth Discussion Points (Comprehensive breakdown of topics debated. Include differing viewpoints, specific data points or metrics cited, and the nuance of the conversation. Use nested sub-bullets for depth)
        3. Decisions Made & Rationale (Clear list of final conclusions, agreements, and the specific reasoning behind why those decisions were made)
        4. Action Items (Specific tasks, exact deadlines, and clear ownership)
        
        CRITICAL FORMATTING RULES FOR BOTH:
        Format your entire response in clean, basic HTML. Use <h3> for section headers, <p> for paragraphs, <ul>/<li> for unordered lists, and <ol>/<li> for numbered steps. Use strong (<b>/<strong>) tags to emphasize key terms, measurements, metrics, or names to make the document highly scannable. Do not include markdown formatting (like ```html), just the raw HTML elements. Eliminate casual filler but retain all substantive content.
        """

        response = client.chat.completions.create(
            model="gpt-4o", 
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": f"Here is the raw transcript to process:\n\n{transcript}"}
            ],
            temperature=0.3 
        )
        
        if job:
            job['sub_progress'] = 100

        manual_html = response.choices[0].message.content
        manual_path = transcript_text_path.replace('.txt', '_summary.html')
        pdf_path = transcript_text_path.replace('.txt', '_summary.pdf')
        
        with open(manual_path, "w", encoding="utf-8") as f:
            f.write(manual_html)
            
        try:
            with open(pdf_path, "w+b") as result_file:
                pisa_status = pisa.CreatePDF(manual_html, dest=result_file)
            if pisa_status.err:
                logger.error(f"Failed to generate summary PDF: {pisa_status.err}")
                pdf_path = None
        except Exception as e:
            logger.error(f"PDF creation exception: {e}")
            pdf_path = None
            
        return manual_path, pdf_path, manual_html
    except Exception as e:
        logger.error(f"Failed to generate AI summary: {e}")
        return None, None, None

# ---------------------------------

def process_track(url, session_dir, track_index, ffmpeg_exe, session_id, zip_path, lock, track_name, artist_name, thumbnail, start_time, end_time, transcribe_audio):
    job = conversion_jobs.get(session_id)
    if not job or job.get('cancelled'): return False

    temp_filename_base = f"track_{track_index}"
    
    def progress_hook(d):
        if job.get('cancelled'): 
            raise Exception("CancelledByUser")
            
        if d['status'] == 'downloading':
            total = d.get('total_bytes') or d.get('total_bytes_estimate')
            if total and d.get('downloaded_bytes'):
                job['sub_progress'] = int((d['downloaded_bytes'] / total) * 100)
            job['current_status'] = 'Downloading audio...'
        elif d['status'] == 'finished':
            job['sub_progress'] = 100
            job['current_status'] = 'Extracting audio...'

    ydl_opts = {
        'format': 'http_mp3_128/bestaudio[ext=mp3]/bestaudio/best',
        'outtmpl': os.path.join(session_dir, f"{temp_filename_base}.%(ext)s"),
        'ffmpeg_location': ffmpeg_exe,
        'quiet': True, 'no_warnings': True, 'nocheckcertificate': True,
        'socket_timeout': 30, 'retries': 5,
        'hls_prefer_native': True, 
        'writethumbnail': False,
        'progress_hooks': [progress_hook], 'cookiefile': None,
        'http_headers': {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        },
        'postprocessors': [
            {'key': 'FFmpegExtractAudio', 'preferredcodec': 'mp3', 'preferredquality': '128'},
        ],
        'postprocessor_args': {
            'ffmpeg': [
                '-map_metadata', '-1', 
                '-threads', '1',
                '-err_detect', 'ignore_err'
            ]
        },
    }

    if start_time or end_time:
        ydl_opts['external_downloader'] = ffmpeg_exe
        ffmpeg_args = ['-y']
        if start_time:
            ffmpeg_args.extend(['-ss', str(start_time)])
        if end_time:
            ffmpeg_args.extend(['-to', str(end_time)])
        ydl_opts['external_downloader_args'] = {'ffmpeg_i': ffmpeg_args}

    try:
        job['current_track'] = track_index
        job['last_update'] = time.time()
        job['current_status'] = f'Initializing track {track_index}...'
        job['sub_progress'] = 0
        job['current_thumbnail'] = thumbnail 
        
        if job.get('cancelled'): return False

        try:
            with YoutubeDL({'quiet':True, 'no_warnings':True, 'socket_timeout':10}) as ydl:
                info = ydl.extract_info(url, download=False)
                if info.get('title'): track_name = info['title']
                if info.get('uploader'): artist_name = info['uploader']
                if info.get('thumbnail'): job['current_thumbnail'] = info['thumbnail']
        except: pass
        
        with YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
        
        mp3_files = glob.glob(os.path.join(session_dir, f"{temp_filename_base}*.mp3"))
        if mp3_files:
            file_to_zip = mp3_files[0]

            try:
                cmd = [
                    ffmpeg_exe, '-y', '-i', file_to_zip, 
                    '-map_metadata', '-1', 
                    '-metadata', f'title={track_name}', 
                    '-metadata', f'artist={artist_name}', 
                    '-c', 'copy', file_to_zip + '.tmp'
                ]
                subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
                if os.path.exists(file_to_zip + '.tmp'): 
                    os.replace(file_to_zip + '.tmp', file_to_zip)
            except: pass

            clean_name = "".join([c for c in f"{artist_name} - {track_name}"[:100] if c.isalnum() or c in (' ', '-', '_')]).strip() or f"Track_{track_index}"
            
            with lock:
                with zipfile.ZipFile(zip_path, 'a', zipfile.ZIP_STORED) as z:
                    z.write(file_to_zip, f"{clean_name}.mp3")
            
            if transcribe_audio:
                raw_txt_path, raw_pdf_path = transcribe_audio_file(file_to_zip, job)
                
                if raw_txt_path:
                    html_path, summary_pdf_path, manual_html = generate_diy_manual(raw_txt_path, job)
                    
                    with lock:
                        with zipfile.ZipFile(zip_path, 'a', zipfile.ZIP_STORED) as z:
                            # Prioritize adding the PDF of the raw transcript
                            if raw_pdf_path and os.path.exists(raw_pdf_path):
                                z.write(raw_pdf_path, f"{clean_name}_raw_transcript.pdf")
                            else:
                                z.write(raw_txt_path, f"{clean_name}_raw_transcript.txt")
                                
                            # Prioritize adding the PDF of the summary
                            if summary_pdf_path and os.path.exists(summary_pdf_path):
                                z.write(summary_pdf_path, f"{clean_name}_summary.pdf")
                            elif html_path and os.path.exists(html_path):
                                z.write(html_path, f"{clean_name}_summary.html")

                    if manual_html:
                        job['email_summaries'] += f"<hr><h2>{clean_name}</h2>" + manual_html
                    else:
                        job['email_summaries'] += f"<hr><h2>{clean_name}</h2><p><em>Notice: AI summarization failed or timed out.</em></p>"
                else:
                    job['email_summaries'] += f"<hr><h2>{clean_name}</h2><p><em>Notice: Audio transcription failed due to an API error.</em></p>"

            job['completed'] += 1
            job['sub_progress'] = 100
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
            
            if user_email:
                notify_user_complete(session_id, user_email, job['completed'], job.get('email_summaries', ''))
                
            dev_email = os.environ.get('DEV_EMAIL')
            if dev_email:
                subject = f"User Conversion Finished: {job['completed']}/{job['total']}"
                user_info_html = f"<p><strong>User Email:</strong> {user_email}</p>" if user_email else "<p><strong>User Email:</strong> Not provided</p>"
                
                body = f"""
                <p><strong>Result:</strong> {job['completed']} of {job['total']} tracks converted.</p>
                <p><strong>URL:</strong> {url}</p>
                {user_info_html}
                """
                send_email_notification(dev_email, subject, body)
                
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
                    
                run_conversion_task(
                    sid, 
                    task_data['url'], 
                    task_data['entries'], 
                    task_data.get('email'),
                    task_data.get('start_time'),
                    task_data.get('end_time'),
                    task_data.get('transcribe_audio')
                )
            else:
                time.sleep(1)
        except Exception as e:
            logger.error(f"Worker error: {e}")
            time.sleep(1)

queue_worker = Thread(target=worker_loop, daemon=True)
queue_worker.start()

@app.route('/start_conversion', methods=['POST'])
def start_conversion():
    cleanup_old_sessions()
    data = request.json
    
    # MODIFICATION: Sanitize the incoming URL to strip query parameters like ?in= or ?utm_source=
    raw_url = data.get('url', '').strip()
    url = raw_url.split('?')[0] if raw_url else ''
    
    session_id = data.get('session_id', str(uuid.uuid4()))
    user_email = data.get('email', '').strip() 
    start_time = data.get('start_time', '').strip()
    end_time = data.get('end_time', '').strip()
    transcribe_audio = data.get('transcribe_audio', False) 
    
    if not url: return jsonify({"error": "No URL provided"}), 400
    
    try:
        with YoutubeDL({'extract_flat': True, 'quiet': True, 'playlistend': MAX_SONGS, 'nocheckcertificate': True}) as ydl:
            info = ydl.extract_info(url, download=False)
            entries = info.get('entries', [info]) if info else []
            
            valid_entries = []
            for i, e in enumerate(entries[:MAX_SONGS]):
                if e:
                    track_url = e.get('url') or e.get('webpage_url') or e.get('id', '')
                    if not track_url.startswith('http') and 'soundcloud' in url: 
                        track_url = "[https://soundcloud.com/track/](https://soundcloud.com/track/)" + str(e.get('id', i))
                    elif not track_url.startswith('http'):
                        continue 
                        
                    thumbnail = e.get('thumbnail', info.get('thumbnail', ''))
                    valid_entries.append((i+1, track_url, e.get('title', f"Track {i}"), e.get('uploader', 'Artist'), thumbnail))
            
            total_tracks = len(valid_entries)

        if total_tracks == 0: return jsonify({"error": "No tracks found."}), 400
        
        conversion_jobs[session_id] = {
            'status': 'queued', 'total': total_tracks, 'completed': 0,
            'skipped': 0, 'current_track': 0, 'completed_tracks': [],
            'skipped_tracks': [], 'cancelled': False, 'zip_ready': False,
            'current_thumbnail': '',
            'last_update': time.time(),
            'email_summaries': '',
            'sub_progress': 0 
        }
        
        conversion_queue.append({
            'session_id': session_id,
            'url': url,
            'entries': valid_entries,
            'email': user_email,
            'start_time': start_time if start_time else None,
            'end_time': end_time if end_time else None,
            'transcribe_audio': transcribe_audio
        })
        
        position = len(conversion_queue)
        
        return jsonify({
            "session_id": session_id, 
            "total_tracks": total_tracks, 
            "status": "queued",
            "queue_position": position
        }), 200
        
    except Exception as e:
        logger.error(f"Extraction failed for {url}: {str(e)}") 
        return jsonify({"error": "This URL may be protected and unsupported. Please try a valid public link."}), 400

@app.route('/status/<session_id>', methods=['GET'])
def get_status(session_id):
    job = conversion_jobs.get(session_id)
    if not job: return jsonify({"error": "Session not found"}), 404
    
    queue_pos = 0
    wait_seconds = 0
    
    if job['status'] == 'queued':
        # 1. Add time for the currently processing session (if it's not this user's session)
        if current_processing_session and current_processing_session != session_id:
            curr_job = conversion_jobs.get(current_processing_session)
            if curr_job and curr_job['status'] == 'processing':
                remaining_active = max(0, curr_job['total'] - curr_job['completed'])
                wait_seconds += (remaining_active * AVG_TIME_PER_TRACK)

        # 2. Add time for all jobs AHEAD of this user in the queue
        for idx, item in enumerate(conversion_queue):
            if item['session_id'] == session_id:
                queue_pos = idx + 1
                break
            wait_seconds += (len(item['entries']) * AVG_TIME_PER_TRACK)
            
    wait_minutes = math.ceil(wait_seconds / 60)

    return jsonify({
        "status": job['status'], 
        "total": job['total'], 
        "completed": job['completed'],
        "skipped": job['skipped'], 
        "current_track": job['current_track'],
        "current_status": job.get('current_status', ''), 
        "current_thumbnail": job.get('current_thumbnail', ''),
        "zip_ready": job.get('zip_ready', False),
        "zip_path": job.get('zip_path', ''), 
        "skipped_tracks": job['skipped_tracks'],
        "sub_progress": job.get('sub_progress', 0),
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