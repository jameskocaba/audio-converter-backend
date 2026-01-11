import gevent.monkey
gevent.monkey.patch_all()

import os, uuid, logging, glob, zipfile, certifi, shutil, gc, time, random
from flask import Flask, request, send_file, jsonify, Response, stream_with_context
from flask_cors import CORS
from yt_dlp import YoutubeDL
import json

# SSL & Logging
os.environ['SSL_CERT_FILE'] = certifi.where()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}})

DOWNLOAD_FOLDER = os.path.join(os.getcwd(), 'downloads')
os.makedirs(DOWNLOAD_FOLDER, exist_ok=True)
MAX_SONGS = 500

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

def process_single_track(url, session_dir, track_index, ffmpeg_exe, session_id):
    """Process a single track and return success status"""
    try:
        ydl_opts = {
            'format': 'bestaudio/best',
            'writethumbnail': True,  # Keep downloading the thumbnail
            'postprocessors': [
                {
                    'key': 'FFmpegExtractAudio',
                    'preferredcodec': 'mp3',
                    'preferredquality': '128',
                },
                {
                    # Add this to ensure thumbnails are converted to a standard format like JPG
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
            'outtmpl': os.path.join(session_dir, '%(title)s.%(ext)s'),
            'noplaylist': False,
            'playlist_items': str(track_index),
            'ffmpeg_location': ffmpeg_exe,
            'ignoreerrors': True,
            'quiet': True,
            'no_warnings': True,
            'cookiefile': 'cookies.txt' if os.path.exists('cookies.txt') else None,
            'progress_hooks': [lambda d: progress_hook(d, session_id)],
            'keepvideo': False,
            'nocheckcertificate': True,
            'socket_timeout': 15,
        }
        
        with YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
        
        # MODIFIED: Cleanup temporary files but PRESERVE .jpg and .png files
        for ext in ['*.webp', '*.part', '*.ytdl', '*.tmp']:
            for file in glob.glob(os.path.join(session_dir, ext)):
                try:
                    os.remove(file)
                except:
                    pass
        
        cleanup_memory()
        return True
        
    except Exception as e:
        logger.error(f"Error processing track {track_index}: {e}")
        cleanup_memory()
        return False

def generate_conversion_stream(url, session_id):
    """Generator function that yields progress updates via SSE"""
    
    session_dir = os.path.join(DOWNLOAD_FOLDER, session_id)
    os.makedirs(session_dir, exist_ok=True)
    
    ffmpeg_exe = 'ffmpeg'
    local_ffmpeg = os.path.join(os.getcwd(), 'ffmpeg_bin/ffmpeg')
    if os.path.exists(local_ffmpeg):
        ffmpeg_exe = local_ffmpeg
        os.chmod(ffmpeg_exe, 0o755)

    try:
        yield f"data: {json.dumps({'type': 'status', 'message': 'Connecting to server...'})}\n\n"
        yield f"data: {json.dumps({'type': 'status', 'message': 'Analyzing playlist metadata...'})}\n\n"
        
        info_opts = {
            'extract_flat': 'in_playlist',
            'quiet': True,
            'no_warnings': True,
            'ignoreerrors': True,
            'nocheckcertificate': True
        }
        
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

        cleanup_memory()
        yield f"data: {json.dumps({'type': 'total', 'total': total_tracks})}\n\n"

        if active_tasks.get(session_id) is True:
            raise Exception("USER_CANCELLED")

        successful_tracks = []
        failed_tracks = []
        
        for i in range(1, total_tracks + 1):
            if active_tasks.get(session_id) is True:
                raise Exception("USER_CANCELLED")
            
            track_name = expected_titles[i-1] if i-1 < len(expected_titles) else f"Track {i}"
            yield f"data: {json.dumps({'type': 'progress', 'current': i, 'total': total_tracks, 'track': track_name})}\n\n"
            
            success = process_single_track(url, session_dir, i, ffmpeg_exe, session_id)
            
            if success:
                successful_tracks.append(i)
                yield f"data: {json.dumps({'type': 'complete', 'track': track_name})}\n\n"
            else:
                failed_tracks.append(i)
                yield f"data: {json.dumps({'type': 'failed', 'track': track_name})}\n\n"
            
            time.sleep(random.uniform(1.5, 3.0))

        # MODIFIED: Collect both MP3 and Image files
        all_downloaded_files = glob.glob(os.path.join(session_dir, "*.mp3")) + \
                               glob.glob(os.path.join(session_dir, "*.jpg")) + \
                               glob.glob(os.path.join(session_dir, "*.png"))
        
        skipped = [expected_titles[idx] for idx in range(len(expected_titles)) if (idx + 1) in failed_tracks]

        # Generate individual links for all files (Music + Art)
        tracks = [{"name": os.path.basename(f), "downloadLink": f"/download/{session_id}/{os.path.basename(f)}"} 
                  for f in all_downloaded_files]

        zip_link = None
        if len(all_downloaded_files) > 1:
            yield f"data: {json.dumps({'type': 'status', 'message': 'Creating ZIP bundle with artwork...'})}\n\n"
            
            zip_name = "soundcloud_bundle_with_art.zip"
            zip_path = os.path.join(session_dir, zip_name)
            
            with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED, compresslevel=1) as z:
                for f in all_downloaded_files:
                    z.write(f, os.path.basename(f))
            
            zip_link = f"/download/{session_id}/{zip_name}"

        cleanup_memory()

        result = {
            "type": "done",
            "status": "success", 
            "tracks": tracks, 
            "zipLink": zip_link, 
            "skipped": skipped, 
            "session_id": session_id,
            "total_processed": len(successful_tracks),
            "total_expected": total_tracks
        }
        
        yield f"data: {json.dumps(result)}\n\n"

    except Exception as e:
        logger.exception("Conversion error")
        if str(e) == "USER_CANCELLED":
            shutil.rmtree(session_dir, ignore_errors=True)
            yield f"data: {json.dumps({'type': 'cancelled', 'message': 'Conversion stopped by user.'})}\n\n"
        else:
            yield f"data: {json.dumps({'type': 'error', 'message': f'Server Error: {str(e)}'})}\n\n"
        cleanup_memory()
    
    finally:
        active_tasks.pop(session_id, None)
        cleanup_memory()

# Remaining routes (/convert, /download) stay the same as your original file
@app.route('/convert', methods=['POST'])
def convert_audio():
    data = request.json
    url = data.get('url', '').strip()
    if not url or "soundcloud.com" not in url.lower():
        return jsonify({"status": "error", "message": "Invalid link."}), 400

    session_id = str(uuid.uuid4())
    active_tasks[session_id] = False
    return Response(
        stream_with_context(generate_conversion_stream(url, session_id)),
        mimetype='text/event-stream',
        headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no', 'Connection': 'keep-alive'}
    )

@app.route('/download/<session_id>/<filename>')
def download_file(session_id, filename):
    file_path = os.path.join(DOWNLOAD_FOLDER, session_id, filename)
    if os.path.exists(file_path):
        return send_file(file_path, as_attachment=True)
    return "File not found", 404

if __name__ == '__main__':
    app.run(debug=True, port=5000, threaded=True)