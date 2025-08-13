# Youtube-to-mp3

A robust YouTube to MP3 converter with intelligent format fallbacks and enhanced metadata tagging.

## Features

- **Smart Format Selection**: Multiple fallback strategies for maximum compatibility
- **Enhanced Metadata**: Automatic ID3v2 tagging with artist, album, genre, year, and album artwork
- **Intelligent Title Parsing**: Extracts artist and song information from video titles
- **iTunes Integration**: Looks up metadata using iTunes Search API for accurate tagging
- **Batch Processing**: Convert multiple videos using download.txt
- **Timeout Protection**: Prevents hanging with configurable timeouts
- **Playlist Filtering**: Strips playlist parameters by default (unless --allow-playlist)

## Quick start (macOS/Windows/Linux):

1. **Install Python 3.8+** (if you don't have it).

2. **Install the tools:**
   ```bash
   pip install yt-dlp mutagen requests
   ```
   - **FFmpeg:**
     - **macOS:** `brew install ffmpeg`
     - **Windows (one of):** `winget install Gyan.FFmpeg` or `choco install ffmpeg` or `scoop install ffmpeg`
     - **Ubuntu/Debian:** `sudo apt-get update && sudo apt-get install ffmpeg`
    
3. **Install Env:**
   ```bash
   # Create venv in a hidden folder .venv
   python3 -m venv .venv
   
   # Activate it
   source .venv/bin/activate
   ```

## Usage

- **Single video:** `python run.py <YouTube_URL>`
- **Bulk conversion:** Create a `download.txt` file with one YouTube URL per line, then run `python run.py`
- **List formats:** `python run.py --list-formats <YouTube_URL>` to see available quality options
- **Custom output:** `python run.py -o /path/to/output <YouTube_URL>`
- **Skip tagging:** Set `YTMP3_SKIP_TAG=1` environment variable to disable metadata tagging

## Metadata Features

The script assumes all YouTube videos are music videos and automatically:
- Cleans video titles by removing YouTube-specific additions (Official Video, HD, VEVO, etc.)
- Looks up accurate metadata using multiple online music databases:
  - **MusicBrainz**: Comprehensive open music database (primary source)
  - **iTunes Search API**: Commercial music database with artwork
  - **Last.fm**: Community-driven music database (fallback)
- Embeds high-quality album artwork when available
- Applies comprehensive ID3v2 tags including:
  - Title, Artist, Album, Album Artist
  - Genre, Release Year, Track Numbers  
  - Album artwork (600x600px when available)
  - YouTube video ID in comments for reference

**No title parsing** - The script sends the cleaned video title directly to music databases for accurate metadata lookup, avoiding parsing errors.

## Examples

```bash
# Download single video with enhanced metadata
python run.py "https://www.youtube.com/watch?v=dQw4w9WgXcQ"

# Batch download from file
echo "https://www.youtube.com/watch?v=dQw4w9WgXcQ" > download.txt
python run.py

# Check available formats first
python run.py --list-formats "https://www.youtube.com/watch?v=dQw4w9WgXcQ"

# Custom bitrate and output directory
python run.py --bitrate 320 -o ~/Music "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
```
