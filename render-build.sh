#!/usr/bin/env bash

# Install Python requirements
pip install -r requirements.txt

# Create folder for ffmpeg
mkdir -p ffmpeg_bin
cd ffmpeg_bin

# Download and extract FFmpeg
wget -q https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz
tar xf ffmpeg-release-amd64-static.tar.xz --strip-components=1

# --- CRITICAL ADDITION FOR PERMISSIONS ---
chmod +x ffmpeg ffprobe
# -----------------------------------------

# Clean up the compressed file to save space
rm ffmpeg-release-amd64-static.tar.xz

echo "Build successful!"