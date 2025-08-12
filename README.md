# Youtube-to-mp3

## Quick start (macOS/Windows/Linux):

1. **Install Python 3.8+** (if you don't have it).

2. **Install the tools:**
   - `pip install yt-dlp`
   - **FFmpeg:**
     - **macOS:** `brew install ffmpeg`
     - **Windows (one of):** `winget install Gyan.FFmpeg` or `choco install ffmpeg` or `scoop install ffmpeg`
     - **Ubuntu/Debian:** `sudo apt-get update && sudo apt-get install ffmpeg`

## Features

This tool converts a YouTube video passed as parameter or bulk convert using download.txt (one video link per line) and saves into downloads folder.

## Usage

- **Single video:** `python run.py <YouTube_URL>`
- **Bulk conversion:** Create a `download.txt` file with one YouTube URL per line, then run `python run.py`
