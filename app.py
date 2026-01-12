import gevent.monkey
gevent.monkey.patch_all()

import os, uuid, logging, glob, zipfile, certifi, shutil, gc, time, random
from flask import Flask, request, send_file, jsonify, Response, stream_with_context
from flask_cors import CORS
from yt_dlp import YoutubeDL
import json
from concurrent.futures import ThreadPoolExecutor, as_completed

# SSL & Logging
os.environ['SSL_CERT_FILE'] = certifi.where()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}})

DOWNLOAD_FOLDER = os.path.join(os.getcwd(), 'downloads')
os.makedirs(DOWNLOAD_FOLDER, exist_ok=True)

# Limit concurrent workers to minimize RAM usage on free tier
MAX_WORKERS = 2 
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
    """Process a single track and return (success_boolean, track_index)."""
    try:
        if active_tasks.get(session_id) is True:
            return False, track_index

        ydl_opts = {
            'format': 'bestaudio/best',
            'writethumbnail': True,
            'postprocessors': [
                {
                    'key': 'FFmpegExtractAudio',
                    'preferredcodec': 'mp3',
                    'preferredquality': '192', # Slightly better quality
                },
                {
                    'key': 'FFmpegThumbnailsConvertor',
                    'format': 'jpg',
                },
                {
                    'key': 'EmbedThumbnail', # Embeds art into MP3
                },
                {
                    'key': 'FFmpegMetadata',
                    'add_metadata': True,
                }
            ],
            # Use specific filenames to avoid collision
            'outtmpl': os.path.join(session_dir, f'%(title)s.%(ext)s'),
            'noplaylist': False,
            'playlist_items': str(track_index),
            'ffmpeg_location': ffmpeg_exe,
            'ignoreerrors': True,
            'quiet': True,
            'no_warnings': True,
            # CRITICAL: Disable cache to save RAM/Disk
            'cachedir': False, 
            'cookiefile': 'cookies.txt' if os.path.exists('cookies.txt') else None,
            'progress_hooks': [lambda d: progress_hook(d, session_id)],
            'keepvideo': False,
            'nocheckcertificate': True,
            'socket_timeout': 30,
        }
        
        with YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
        
        # Cleanup temp files immediately
        for ext in ['*.webp', '*.part', '*.ytdl', '*.tmp']:
            for file in glob.glob(os.path.join(session_dir, ext)):
                try:
                    os.remove(file)
                except:
                    pass
        
        return True, track_index
        
    except Exception as e:
        logger.error(f"Error processing track {track_index}: {e}")
        return False, track_index

def generate_conversion_stream(url, session_id):
    """Generator function that yields progress updates via SSE"""
    
    session_dir = os.path.join(DOWNLOAD_FOLDER, session_id)
    os.makedirs(session_dir, exist_ok=True)
    
    # Initialize Zip File immediately
    zip_name = "soundcloud_bundle_with_art.zip"
    zip_path = os.path.join(session_dir, zip_name)
    
    ffmpeg_exe = 'ffmpeg'
    local_ffmpeg = os.path.join(os.getcwd(), 'ffmpeg_bin/ffmpeg')
    if os.path.exists(local_ffmpeg):
        ffmpeg_exe = local_ffmpeg
        os.chmod(ffmpeg_exe, 0o755)

    try:
        yield f"data: {json.dumps({'type': 'status', 'message': 'Connecting to server...'})}\n\n"
        
        # 1. Metadata Phase
        info_opts = {
            'extract_flat': 'in_playlist',
            'quiet': True, 
            'no_warnings': True, 
            'nocheckcertificate': True,
            'cachedir': False 
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

        successful_track_indices = set()
        failed_track_indices = set()
        processed_count = 0

        # 2. Batch Processing Phase
        # We process in chunks to keep memory usage flat
        batch_size = MAX_WORKERS
        track_indices = list(range(1, total_tracks + 1))
        
        # Initialize empty zip file
        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as z:
            pass

        # Loop through chunks
        for i in range(0, len(track_indices), batch_size):
            if active_tasks.get(session_id) is True:
                raise Exception("USER_CANCELLED")

            batch = track_indices[i : i + batch_size]
            
            # Update Status
            current_track_names = []
            for idx in batch:
                t_name = expected_titles[idx-1] if (idx-1) < len(expected_titles) else f"Track {idx}"
                current_track_names.append(t_name)
            
            yield f"data: {json.dumps({'type': 'status', 'message': f'Processing: {', '.join(current_track_names)}'})}\n\n"

            # Execute Batch in Parallel
            with ThreadPoolExecutor(max_workers=batch_size) as executor:
                futures = {
                    executor.submit(process_single_track, url, session_dir, idx, ffmpeg_exe, session_id): idx 
                    for idx in batch
                }
                
                for future in as_completed(futures):
                    success, idx = future.result()
                    track_name = expected_titles[idx-1] if (idx-1) < len(expected_titles) else f"Track {idx}"
                    
                    if success:
                        successful_track_indices.add(idx)
                        processed_count += 1
                        yield f"data: {json.dumps({'type': 'progress', 'current': processed_count, 'total': total_tracks, 'track': track_name})}\n\n"
                    else:
                        failed_track_indices.add(idx)
                        yield f"data: {json.dumps({'type': 'failed', 'track': track_name})}\n\n"

            # 3. Incremental Zip & Clean Phase
            # Find all files excluding the zip file itself
            files_to_zip = [
                f for f in glob.glob(os.path.join(session_dir, '*')) 
                if not f.endswith('.zip') and os.path.isfile(f)
            ]

            if files_to_zip:
                with zipfile.ZipFile(zip_path, 'a', zipfile.ZIP_DEFLATED, compresslevel=1) as z:
                    for f in files_to_zip:
                        try:
                            z.write(f, os.path.basename(f))
                        except Exception as e:
                            logger.error(f"Zip error: {e}")

                # DELETE IMMEDIATELY to free disk space/inodes
                for f in files_to_zip:
                    try:
                        os.remove(f)
                    except Exception as e:
                        logger.error(f"Delete error: {e}")

            # Aggressive GC to prevent OOM on free tier
            cleanup_memory()
            time.sleep(0.5) # Brief cool-down for CPU

        # 4. Finalizing
        skipped = [expected_titles[idx-1] for idx in failed_track_indices if (idx-1) < len(expected_titles)]
        
        # Prepare list for frontend. 
        # Note: We provide names but NO individual links because files were deleted to save space.
        tracks_metadata = []
        for idx in sorted(list(successful_track_indices)):
            t_name = expected_titles[idx-1] if (idx-1) < len(expected_titles) else f"Track {idx}"
            # Logic: If it's a single track, we might have kept it? 
            # Actually, for consistency in this batch mode, we zipped everything.
            # We will return the zip link as the primary download.
            tracks_metadata.append({
                "name": t_name, 
                "downloadLink": f"#" # Dummy link as file is in zip
            })

        zip_link = f"/download/{session_id}/{zip_name}" if os.path.exists(zip_path) else None

        result = {
            "type": "done",
            "status": "success", 
            "tracks": tracks_metadata, 
            "zipLink": zip_link, 
            "skipped": skipped, 
            "session_id": session_id,
            "total_processed": len(successful_track_indices),
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
    
    finally:
        active_tasks.pop(session_id, None)
        cleanup_memory()

# Remaining routes (/convert, /download)
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
    # Threaded=True is essential for the parallel processing to work alongside Flask
    app.run(debug=True, port=5000, threaded=True)