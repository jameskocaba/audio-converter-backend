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
MAX_SONGS = 50
AVG_TIME_PER_TRACK = 45  
PUBLIC_URL = os.environ.get('PUBLIC_URL', 'https://mp3aud.io')

# GLOBAL STATE
conversion_jobs = {} 
zip_locks = {}
conversion_queue = deque() 
current_processing_session = None 
popular_tracks = {}

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

            clean_name =