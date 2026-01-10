import gevent.monkey
gevent.monkey.patch_all()

import os, uuid, logging, glob, zipfile, certifi, shutil, gc
from flask import Flask, request, send_file, jsonify
from flask_cors import CORS
from yt_dlp import YoutubeDL

# SSL & Logging
os.environ['SSL_CERT_FILE'] = certifi.where()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}})

DOWNLOAD_FOLDER = os.path.join(os.getcwd(), 'downloads')
os.makedirs(DOWNLOAD_FOLDER, exist_ok=True)
MAX_SONGS = 200
BATCH_SIZE = 3  # Reduced from 10 to 3 for memory constraints

# Global tracker for active downloads
active_tasks = {}

def progress_hook(d, session_id):
    """Checks if the task was cancelled during download."""
    if session_id in active_tasks and active_tasks[session_id]:
        raise Exception("USER_CANCELLED")

def cleanup_memory():
    """Force garbage collection to free memory"""
    gc.collect()

@app.route('/cancel', methods=['POST'])
def cancel_conversion():
    data = request.json
    session_id = data.get('session_id')
    if session_id and session_id in active_tasks:
        active_tasks[session_id] = True  
        logger.info(f"Cancellation requested for session: {session_id}")
        return jsonify({"status": "cancelling"}), 200
    return jsonify({"status": "not_found"}), 404

@app.route('/convert', methods=['POST'])
def convert_audio():
    data = request.json
    url = data.get('url', '').strip()
    
    if not url or "soundcloud.com" not in url.lower():
        return jsonify({
            "status": "error", 
            "message": "Invalid link. This tool only supports SoundCloud shareable links."
        }), 400

    session_id = str(uuid.uuid4())
    active_tasks[session_id] = False 
    
    session_dir = os.path.join(DOWNLOAD_FOLDER, session_id)
    os.makedirs(session_dir, exist_ok=True)
    
    # FFmpeg path logic
    ffmpeg_exe = os.path.join(os.getcwd(), 'ffmpeg_bin/ffmpeg')
    for root, dirs, files in os.walk(os.path.join(os.getcwd(), 'ffmpeg_bin')):
        if 'ffmpeg' in files:
            ffmpeg_exe = os.path.join(root, 'ffmpeg')
            os.chmod(ffmpeg_exe, 0o755)
            break

    # Optimized YDL options for lower memory usage
    base_ydl_opts = {
        'format': 'bestaudio/best',
        'writethumbnail': True,
        'postprocessors': [
            {
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': '128',  # Lower quality to save memory
            },
            {
                'key': 'FFmpegThumbnailsConvertor',
                'format': 'jpg',
            },
            {
                'key': 'EmbedThumbnail',
            },
            {
                'key': 'FFmpegMetadata',
                'add_metadata': True,
            }
        ],
        'postprocessor_args': {
            'ffmpeg': [
                '-id3v2_version', '3', 
                '-metadata:s:v', 'title="Album cover"', 
                '-metadata:s:v', 'comment="Cover (Front)"'
            ]
        },
        'outtmpl': os.path.join(session_dir, '%(title)s.%(ext)s'),
        'noplaylist': False,
        'ffmpeg_location': ffmpeg_exe,
        'ignoreerrors': True, 
        'cookiefile': 'cookies.txt' if os.path.exists('cookies.txt') else None,
        'progress_hooks': [lambda d: progress_hook(d, session_id)],
        'keepvideo': False,  # Don't keep original video file
        'nocheckcertificate': True,
    }

    try:
        # First, extract info to get total track count
        info_opts = base_ydl_opts.copy()
        info_opts['extract_flat'] = True
        
        expected_titles = []
        total_tracks = 0
        
        with YoutubeDL(info_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            if 'entries' in info:
                all_entries = [e for e in info['entries'] if e]
                total_tracks = min(len(all_entries), MAX_SONGS)
                expected_titles = [e.get('title', 'Unknown Track') for e in all_entries[:total_tracks]]
            else:
                total_tracks = 1
                expected_titles = [info.get('title', 'Unknown Track')]

        cleanup_memory()  # Free memory after info extraction

        if active_tasks.get(session_id) is True:
            raise Exception("USER_CANCELLED")

        # Process in small batches
        all_mp3_files = []
        num_batches = (total_tracks + BATCH_SIZE - 1) // BATCH_SIZE
        
        for batch_num in range(num_batches):
            if active_tasks.get(session_id) is True:
                raise Exception("USER_CANCELLED")
            
            start_idx = batch_num * BATCH_SIZE + 1
            end_idx = min((batch_num + 1) * BATCH_SIZE, total_tracks)
            
            logger.info(f"Processing batch {batch_num + 1}/{num_batches}: tracks {start_idx}-{end_idx}")
            
            batch_opts = base_ydl_opts.copy()
            batch_opts['playlist_items'] = f'{start_idx}-{end_idx}'
            
            try:
                with YoutubeDL(batch_opts) as ydl:
                    ydl.download([url])
            except Exception as batch_error:
                logger.error(f"Batch {batch_num + 1} error: {batch_error}")
                # Continue to next batch even if this one fails
            
            # Force cleanup after each batch
            cleanup_memory()
            
            # Check for cancellation after each batch
            if active_tasks.get(session_id) is True:
                raise Exception("USER_CANCELLED")

        # Collect all downloaded MP3 files
        mp3_files = glob.glob(os.path.join(session_dir, "*.mp3"))
        
        # Clean up non-MP3 files to save space
        for ext in ['*.webp', '*.jpg', '*.jpeg', '*.png', '*.part', '*.ytdl', '*.tmp']:
            for file in glob.glob(os.path.join(session_dir, ext)):
                try:
                    if not file.endswith('.mp3'):
                        os.remove(file)
                except:
                    pass
        
        downloaded_names = [os.path.basename(f).lower() for f in mp3_files]
        
        # Identify skipped tracks
        skipped = []
        for title in expected_titles:
            match_found = any(title[:15].lower() in d_name for d_name in downloaded_names)
            if not match_found:
                skipped.append(title)

        tracks = [{"name": n, "downloadLink": f"/download/{session_id}/{n}"} for n in [os.path.basename(f) for f in mp3_files]]

        # Create ZIP if multiple files
        zip_link = None
        if len(mp3_files) > 1:
            zip_name = "soundcloud_bundle.zip"
            zip_path = os.path.join(session_dir, zip_name)
            
            # Create zip in chunks to save memory
            with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as z:
                for f in mp3_files:
                    z.write(f, os.path.basename(f))
            
            zip_link = f"/download/{session_id}/{zip_name}"

        cleanup_memory()  # Final cleanup

        return jsonify({
            "status": "success", 
            "tracks": tracks, 
            "zipLink": zip_link, 
            "skipped": skipped, 
            "session_id": session_id,
            "total_processed": len(mp3_files),
            "total_expected": total_tracks
        })

    except Exception as e:
        if str(e) == "USER_CANCELLED":
            logger.info(f"Cleanup session {session_id} after cancellation.")
            shutil.rmtree(session_dir, ignore_errors=True)
            return jsonify({"status": "cancelled", "message": "Conversion stopped by user."}), 200
        
        logger.exception("Conversion error")
        cleanup_memory()
        return jsonify({"status": "error", "message": str(e)}), 500
    
    finally:
        active_tasks.pop(session_id, None)
        cleanup_memory()

@app.route('/download/<session_id>/<filename>')
def download_file(session_id, filename):
    file_path = os.path.join(DOWNLOAD_FOLDER, session_id, filename)
    if os.path.exists(file_path):
        return send_file(file_path, as_attachment=True)
    return "File not found", 404

if __name__ == '__main__':
    app.run(debug=True, port=5000)