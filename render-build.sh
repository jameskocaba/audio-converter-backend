#!/bin/bash

# Exit immediately if a command fails
set -e

# Install Python requirements
pip install -r requirements.txt

# Create folder for ffmpeg
mkdir -p ffmpeg_bin
cd ffmpeg_bin

# Download and extract FFmpeg
# We use -O to ensure the filename is consistent
wget https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz -O ffmpeg.tar.xz
tar xf ffmpeg.tar.xz --strip-components=1

# CRITICAL: Permissions so the app can actually run the file
chmod +x ffmpeg ffprobe

# Clean up
rm ffmpeg.tar.xz

echo "Build successful!"