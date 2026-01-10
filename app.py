def process_single_track(url, session_dir, track_index, ffmpeg_exe, session_id):
    """Process a single track and return success status"""
    try:
        ydl_opts = {
            'format': 'bestaudio/best',
            'writethumbnail': True,
            'postprocessors': [
                # FIXED ORDER: Convert thumbnail to JPG FIRST, before audio extraction
                {
                    'key': 'FFmpegThumbnailsConvertor',
                    'format': 'jpg',
                },
                # Then extract audio to MP3
                {
                    'key': 'FFmpegExtractAudio',
                    'preferredcodec': 'mp3',
                    'preferredquality': '128',
                },
                # CRITICAL: Embed thumbnail AFTER audio extraction
                {
                    'key': 'EmbedThumbnail',
                    'already_have_thumbnail': False,
                },
                # Add metadata last
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
        
        # Clean up temporary files (thumbnails will be embedded, so we can remove them)
        for ext in ['*.webp', '*.jpg', '*.jpeg', '*.png', '*.part', '*.ytdl', '*.tmp']:
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