#!/usr/bin/env bash
# Exit on error
set -o errexit

# Install Python requirements
pip install -r requirements.txt

# Create folder for ffmpeg
mkdir -p ffmpeg_bin
cd ffmpeg_bin

# Download and extract FFmpeg if not already present
if [ ! -f "ffmpeg" ]; then
    echo "Downloading FFmpeg..."
    wget -q https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz
    tar xf ffmpeg-release-amd64-static.tar.xz --strip-components=1
    
    # CRITICAL: Give execution permissions to the binaries
    chmod +x ffmpeg ffprobe
    
    # Clean up
    rm ffmpeg-release-amd64-static.tar.xz
fi

cd ..
echo "Build successful! Binaries are in $(pwd)/ffmpeg_bin"