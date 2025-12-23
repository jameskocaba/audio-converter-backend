#!/usr/bin/env bash
# Exit on error
set -o errexit

# 1. Install Python dependencies
pip install -r requirements.txt

# 2. Install FFmpeg (Static Build)
# We download a version that doesn't require root access to install
if [ ! -d "ffmpeg_bin" ]; then
  echo "Downloading FFmpeg..."
  mkdir -p ffmpeg_bin
  cd ffmpeg_bin
  wget https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz
  tar xvf ffmpeg-release-amd64-static.tar.xz --strip-components=1
  cd ..
fi

# 3. Add FFmpeg to the PATH so the app can see it
export PATH=$PATH:$(pwd)/ffmpeg_bin

echo "Build complete!"