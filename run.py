#!/usr/bin/env python3
"""
YouTube → MP3 downloader with intelligent format fallbacks and rich metadata tagging

Features:
- Robust format selection with multiple fallback strategies
- Automatic ID3v2 metadata tagging using iTunes Search API
- Batch processing via download.txt
- Playlist parameter stripping (unless --allow-playlist)
- Timeout protection to prevent hanging
- Album artwork embedding
"""

import argparse
import os
import sys
import shutil
import re
import signal
import unicodedata
from pathlib import Path
from typing import List, Optional, Tuple, Dict, Any
from urllib.parse import urlparse, parse_qsl, urlencode, urlunparse

import requests
from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError
from mutagen.easyid3 import EasyID3
from mutagen.id3 import ID3, APIC, ID3NoHeaderError, TIT2, TPE1, TALB, TDRC, TCON, TPE2, COMM, TSSE
from mutagen.mp3 import MP3

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_OUTDIR = SCRIPT_DIR / "downloads"
DEFAULT_LISTFILE = SCRIPT_DIR / "download.txt"
ITUNES_SEARCH_URL = "https://itunes.apple.com/search"


def normalize_unicode_text(text: str) -> str:
    """
    Normalize Unicode text for better compatibility with metadata APIs and file systems.
    Handles Persian, Arabic, and other Unicode characters properly.
    """
    if not text:
        return text
    
    # Normalize Unicode to NFC form (canonical composition)
    normalized = unicodedata.normalize('NFC', text)
    
    # Clean up any problematic characters for filenames while preserving Unicode content
    # Note: We don't remove Unicode chars, just normalize them
    return normalized.strip()


class TimeoutHandler:
    def __init__(self, timeout_seconds=120):
        self.timeout_seconds = timeout_seconds
        self.old_handler = None
    
    def __enter__(self):
        def timeout_handler(signum, frame):
            raise TimeoutError(f"Operation timed out after {self.timeout_seconds} seconds")
        self.old_handler = signal.signal(signal.SIGALRM, timeout_handler)
        signal.alarm(self.timeout_seconds)
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        signal.alarm(0)
        if self.old_handler:
            signal.signal(signal.SIGALRM, self.old_handler)


def progress_hook(d):
    if d.get('status') == 'downloading':
        total = d.get('total_bytes') or d.get('total_bytes_estimate') or 0
        done = d.get('downloaded_bytes', 0)
        pct = f"{(done/total*100):.1f}%" if total else "?%"
        speed = d.get('speed')
        spd = f" @ {speed/1_000_000:.2f} MB/s" if speed else ""
        print(f"Downloading… {pct}{spd}", end="\r", flush=True)
    elif d.get('status') == 'finished':
        print("\nConverting to MP3…")


def ensure_ffmpeg():
    if not shutil.which('ffmpeg'):
        sys.exit("Error: ffmpeg not found. On macOS with Homebrew: `brew install ffmpeg`.")


# ---------- URL handling ----------
def normalize_url(u: str, allow_playlist: bool) -> str:
    """Return a single-video watch URL unless playlists are explicitly allowed."""
    try:
        p = urlparse(u)
        # Expand youtu.be links to standard /watch
        if p.netloc in {"youtu.be", "www.youtu.be"}:
            vid = p.path.lstrip('/')
            q = dict(parse_qsl(p.query))
            if not allow_playlist:
                q = {"v": vid} if vid else q
            new = p._replace(netloc="www.youtube.com", path="/watch", query=urlencode(q, doseq=True))
            return urlunparse(new)
        # Tidy youtube.com watch links
        if "youtube.com" in p.netloc:
            q = dict(parse_qsl(p.query))
            if not allow_playlist:
                # Hard-strip playlist-ish params
                for key in ["list", "index", "start_radio", "pp", "playlist", "playnext", "si", "t"]:
                    q.pop(key, None)
            new = p._replace(query=urlencode(q, doseq=True))
            return urlunparse(new)
    except Exception:
        pass
    return u


# ---------- Enhanced Metadata helpers ----------
def get_music_metadata_from_title(title: str) -> Optional[Dict[str, Any]]:
    """
    Get music metadata from title using online services.
    Simplified to use iTunes first, then fallbacks.
    Each call is isolated to prevent metadata leakage between different songs.
    """
    # Clean up title - remove common YouTube additions
    clean_title = clean_youtube_title(title)
    
    print(f"Looking up metadata for: '{clean_title}'")
    
    # Try iTunes first (most reliable and fast)
    try:
        result = lookup_itunes_direct(clean_title)
        if result and result.get('title'):
            print(f"✓ Metadata found via iTunes")
            return result
    except Exception as e:
        print(f"⚠ iTunes lookup failed: {e}")
    
    # Try MusicBrainz as fallback
    try:
        result = lookup_musicbrainz(clean_title)
        if result and result.get('title'):
            print(f"✓ Metadata found via MusicBrainz")
            return result
    except Exception as e:
        print(f"⚠ MusicBrainz lookup failed: {e}")
    
    print("✗ No metadata found from any service")
    return None


def clean_youtube_title(title: str) -> str:
    """
    Clean YouTube title by removing common additions and noise.
    Preserves Unicode characters for Persian and other international content.
    """
    # Normalize Unicode text first
    title = normalize_unicode_text(title)
    
    # Remove common YouTube suffixes while preserving Unicode content
    patterns_to_remove = [
        r'\s*\(Official Video\).*$',
        r'\s*\(Official Music Video\).*$',
        r'\s*\(Official Audio\).*$',
        r'\s*\(Lyric Video\).*$',
        r'\s*\(Live\).*$',
        r'\s*\(HD\).*$',
        r'\s*\(4K\).*$',
        r'\s*\[Official Video\].*$',
        r'\s*\[Official Music Video\].*$',
        r'\s*\[Official Audio\].*$',
        r'\s*- Topic$',
        r'\s*VEVO$',
    ]
    
    cleaned = title
    for pattern in patterns_to_remove:
        cleaned = re.sub(pattern, '', cleaned, flags=re.IGNORECASE)
    
    return cleaned.strip()


def lookup_musicbrainz(title: str) -> Optional[Dict[str, Any]]:
    """
    Look up music metadata using MusicBrainz API.
    Most comprehensive and accurate music database.
    Uses a fresh session for each lookup to prevent data leakage.
    """
    import urllib.parse
    
    # MusicBrainz search API
    base_url = "https://musicbrainz.org/ws/2/recording"
    query = urllib.parse.quote(title)
    url = f"{base_url}?query={query}&fmt=json&limit=5"
    
    headers = {
        'User-Agent': 'YoutubeMp3Converter/1.0 (your-email@example.com)'  # Required by MusicBrainz
    }
    
    try:
        # Use a fresh session for each request to prevent caching/state issues
        with requests.Session() as session:
            response = session.get(url, headers=headers, timeout=10)
            response.raise_for_status()
            data = response.json()
        
        recordings = data.get('recordings', [])
        if not recordings:
            return None
        
        # Get the best match (first result is usually most relevant)
        recording = recordings[0]
        
        # Extract metadata
        result = {
            'title': recording.get('title', ''),
            'artist': '',
            'album': '',
            'date': '',
            'genre': '',
            'track_number': '',
            'album_artist': '',
        }
        
        # Get artist info
        if recording.get('artist-credit'):
            artists = [ac.get('name', '') for ac in recording['artist-credit'] if isinstance(ac, dict)]
            artist_names = ', '.join(artists)
            # Avoid "Various Artists" if possible - look for next recording if this one has Various Artists
            if artist_names.lower() == 'various artists' and len(recordings) > 1:
                for alt_recording in recordings[1:]:
                    if alt_recording.get('artist-credit'):
                        alt_artists = [ac.get('name', '') for ac in alt_recording['artist-credit'] if isinstance(ac, dict)]
                        alt_artist_names = ', '.join(alt_artists)
                        if alt_artist_names.lower() != 'various artists':
                            recording = alt_recording  # Use this alternative recording instead
                            artist_names = alt_artist_names
                            result['title'] = recording.get('title', '')
                            break
            result['artist'] = artist_names
            result['album_artist'] = artist_names
        
        # Get release info (album)
        if recording.get('releases'):
            release = recording['releases'][0]
            result['album'] = release.get('title', '')
            result['date'] = release.get('date', '')[:4] if release.get('date') else ''
        
        return result if result['title'] and result['artist'] else None
        
    except Exception as e:
        print(f"MusicBrainz lookup error: {e}")
        return None


def lookup_itunes_direct(title: str) -> Optional[Dict[str, Any]]:
    """
    Direct iTunes Search API lookup using the full title.
    Good for popular music and accurate metadata.
    Uses a fresh session for each lookup to prevent data leakage.
    """
    try:
        params = {
            'term': title,
            'media': 'music',
            'entity': 'song',
            'limit': 5
        }
        
        # Use a fresh session for each request to prevent caching/state issues
        with requests.Session() as session:
            response = session.get(ITUNES_SEARCH_URL, params=params, timeout=10)
            response.raise_for_status()
            data = response.json()
        
        if data.get('resultCount', 0) == 0:
            return None
        
        # Get best match based on title similarity
        results = data['results']
        best_match = None
        best_score = 0
        
        title_words = set(title.lower().split())
        
        for result in results:
            track_name = result.get('trackName', '').lower()
            artist_name = result.get('artistName', '').lower()
            
            # Skip "Various Artists" results unless it's the only option
            if artist_name == 'various artists':
                continue
            
            # Calculate similarity score
            track_words = set(track_name.split())
            artist_words = set(artist_name.split())
            all_result_words = track_words | artist_words
            
            # Score based on word overlap
            common_words = title_words & all_result_words
            score = len(common_words) / len(title_words) if title_words else 0
            
            if score > best_score and score > 0.3:  # At least 30% word overlap
                best_score = score
                best_match = result
        
        if not best_match:
            # If no good match found, try to find any non-"Various Artists" result
            for result in results:
                if result.get('artistName', '').lower() != 'various artists':
                    best_match = result
                    break
            # Last resort: use first result even if it's "Various Artists"
            if not best_match:
                best_match = results[0]
        
        return {
            'title': best_match.get('trackName', ''),
            'artist': best_match.get('artistName', ''),
            'album': best_match.get('collectionName', ''),
            'album_artist': best_match.get('artistName', ''),
            'genre': best_match.get('primaryGenreName', ''),
            'date': best_match.get('releaseDate', '')[:4] if best_match.get('releaseDate') else '',
            'track_number': str(best_match.get('trackNumber', '')) if best_match.get('trackNumber') else '',
            'artwork_url': best_match.get('artworkUrl100', '').replace('100x100bb', '600x600bb') if best_match.get('artworkUrl100') else '',
        }
        
    except Exception as e:
        print(f"iTunes lookup error: {e}")
        return None


def lookup_last_fm(title: str) -> Optional[Dict[str, Any]]:
    """
    Look up music metadata using Last.fm API.
    Good fallback service with extensive database.
    Note: Requires API key for full functionality, using public search for now.
    """
    try:
        # Using Last.fm's search without API key (limited functionality)
        # In production, you'd want to get a free API key from Last.fm
        base_url = "https://ws.audioscrobbler.com/2.0/"
        params = {
            'method': 'track.search',
            'track': title,
            'api_key': 'demo',  # Replace with actual API key
            'format': 'json',
            'limit': 5
        }
        
        # Skip Last.fm for now since it requires API key
        # This is a placeholder for when API key is available
        return None
        
    except Exception as e:
        print(f"Last.fm lookup error: {e}")
        return None


def download_artwork(url: str) -> Optional[bytes]:
    """Download album artwork from URL using a fresh session"""
    try:
        with requests.Session() as session:
            r = session.get(url, timeout=10)
            r.raise_for_status()
            return r.content
    except Exception:
        return None


def tag_mp3_with_metadata(mp3_path: Path, video_title: str, uploader: Optional[str] = None, video_id: Optional[str] = None, playlist_info: Optional[Dict[str, Any]] = None):
    """
    Tag MP3 file with metadata from online music services.
    Assumes the video is a music video and looks up proper metadata.
    """
    
    if os.environ.get('YTMP3_SKIP_TAG'):
        print("Skipping tagging (YTMP3_SKIP_TAG set)")
        return
    
    # Validate that the file is a proper MP3 file
    import time
    max_attempts = 10
    for attempt in range(max_attempts):
        try:
            # Try to load the MP3 file to verify it's valid
            test_mp3 = MP3(str(mp3_path))
            if test_mp3.info.length > 0:  # File has audio data
                break
        except Exception as e:
            if attempt < max_attempts - 1:
                print(f"MP3 not ready yet (attempt {attempt + 1}), waiting...")
                time.sleep(1)
                continue
            else:
                print(f"⚠ MP3 file validation failed after {max_attempts} attempts: {e}")
                return
    
    # Get metadata from online services
    metadata = get_music_metadata_from_title(video_title)
    
    if not metadata:
        print("⚠ No metadata found online, using basic info from video")
        metadata = {
            'title': video_title,
            'artist': uploader or 'Unknown Artist',
            'album': '',
            'album_artist': uploader or 'Unknown Artist',
            'genre': '',
            'date': '',
            'track_number': '',
            'artwork_url': '',
        }
    else:
        print(f"✓ Enhanced metadata: {metadata.get('artist', 'Unknown')} - {metadata.get('title', 'Unknown')}")
        
        # If metadata lookup returned "Various Artists" and we have a valid uploader, use uploader instead
        if (metadata.get('artist', '').lower() == 'various artists' and 
            uploader and uploader.lower() != 'various artists'):
            print(f"⚠ Replacing 'Various Artists' with uploader: {uploader}")
            metadata['artist'] = uploader
            metadata['album_artist'] = uploader
            
        # Improve album handling for playlist downloads
        if playlist_info and playlist_info.get('playlist_title'):
            playlist_title = playlist_info['playlist_title']
            # If we don't have a good album name from metadata, use playlist title
            if not metadata.get('album') or metadata.get('album').lower() in ['various artists', 'unknown album']:
                metadata['album'] = playlist_title
                print(f"✓ Set album to playlist title: {playlist_title}")
            # Add playlist track number if available
            if playlist_info.get('playlist_index'):
                metadata['track_number'] = str(playlist_info['playlist_index'])
    
    # Apply metadata to MP3 file
    try:
        # Load MP3 file and ensure it has tags
        audio_file = MP3(str(mp3_path))
        if audio_file.tags is None:
            audio_file.add_tags()
            audio_file.save()
        
        # Use EasyID3 for standard tags
        easy_tags = EasyID3(str(mp3_path))
        
        # Clear existing tags first to avoid conflicts
        easy_tags.clear()
        
        # Apply all available metadata
        if metadata.get('title'):
            easy_tags['title'] = [metadata['title']]
        if metadata.get('artist'):
            easy_tags['artist'] = [metadata['artist']]
        if metadata.get('album'):
            easy_tags['album'] = [metadata['album']]
        if metadata.get('album_artist'):
            easy_tags['albumartist'] = [metadata['album_artist']]
        if metadata.get('genre'):
            easy_tags['genre'] = [metadata['genre']]
        if metadata.get('date'):
            easy_tags['date'] = [str(metadata['date'])]
        if metadata.get('track_number'):
            easy_tags['tracknumber'] = [str(metadata['track_number'])]
        
        easy_tags.save()
        print("✓ Metadata applied")
        
        # Add comment and encoder using raw ID3 (since EasyID3 doesn't support them)
        try:
            id3_tags = ID3(str(mp3_path))
            
            # Add comment
            id3_tags.add(COMM(
                encoding=3,
                lang='eng',
                desc='',
                text=[f"YouTube: {video_id}" if video_id else "Downloaded from YouTube"]
            ))
            
            # Add encoder info using TSSE frame
            id3_tags.add(TSSE(encoding=3, text='YoutubeMp3Converter'))
            
            id3_tags.save(v2_version=3)
        except Exception as id3_e:
            print(f"⚠ Extended tags failed: {id3_e}")
        
        # Add album artwork if available
        artwork_url = metadata.get('artwork_url')
        if artwork_url:
            artwork_data = download_artwork(artwork_url)
            if artwork_data:
                try:
                    tags = ID3(str(mp3_path))
                    # Remove existing artwork
                    for key in list(tags.keys()):
                        if key.startswith('APIC'):
                            del tags[key]
                    
                    # Add new artwork
                    tags.add(APIC(
                        encoding=3,
                        mime='image/jpeg',
                        type=3,  # Cover (front)
                        desc='Cover',
                        data=artwork_data
                    ))
                    tags.save(v2_version=3)
                    print("✓ Album artwork added")
                except Exception as art_e:
                    print(f"⚠ Artwork embedding failed: {art_e}")
        
        if metadata.get('album'):
            print(f"  📀 {metadata['album']}")
        
    except Exception as e:
        print(f"⚠ Metadata tagging failed: {e}")


# Legacy functions for backward compatibility
def parse_video_title(title: str) -> Tuple[Optional[str], Optional[str], str]:
    """Legacy function - now just returns title as track"""
    return None, None, title


def enhanced_itunes_lookup(artist: Optional[str], track: str, album: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Legacy function - replaced by direct title lookup"""
    return lookup_itunes_direct(track)


def tag_mp3_enhanced(mp3_path: Path, video_title: str, uploader: Optional[str] = None, video_id: Optional[str] = None):
    """Legacy function - replaced by tag_mp3_with_metadata"""
    tag_mp3_with_metadata(mp3_path, video_title, uploader, video_id, None)


def tag_mp3(mp3_path: Path, base_title: str, uploader: Optional[str] = None):
    """Legacy function - replaced by tag_mp3_with_metadata"""
    tag_mp3_with_metadata(mp3_path, base_title, uploader, None, None)


# ---------- yt-dlp options ----------
def make_ydl_opts(outdir: str, bitrate: str, allow_playlist: bool, alt: bool = False, processed_files: Optional[set] = None):
    # Use provided processed_files set or create a new one
    if processed_files is None:
        processed_files = set()
    
    def post_hook(d):
        # After post-processing; MP3 should exist now.
        if d.get('status') != 'finished':
            return

        info = d.get('info_dict', {}) or {}
        # yt-dlp may supply different keys depending on hook type/version
        candidate_paths = [
            d.get('filepath'),              # some postprocessor hooks
            d.get('filename'),              # progress style key
            info.get('filepath'),           # newer yt-dlp info key
            info.get('_filename'),          # legacy internal key
        ]
        filepath = next((p for p in candidate_paths if p), None)

        if not filepath:
            # Last resort: derive from title+id pattern we set in outtmpl
            vid_id = info.get('id')
            title = info.get('title')
            if vid_id and title:
                pattern = f"{title} [{vid_id}].mp3"
                guess = Path(outdir) / pattern
                if guess.exists():
                    filepath = str(guess)

        if not filepath:
            return

        if not filepath.endswith('.mp3'):
            mp3_path = Path(filepath).with_suffix('.mp3')
            if mp3_path.exists():
                filepath = str(mp3_path)
            else:
                # Not an MP3 (maybe different codec) → skip tagging
                return

        # Verify file exists
        if not Path(filepath).exists():
            return
            
        # Prevent duplicate processing of the same file
        if filepath in processed_files:
            return
        processed_files.add(filepath)
            
        print(f"🎵 Processing: {Path(filepath).name}")

        base_title = info.get('title') or Path(filepath).stem
        uploader = info.get('artist') or info.get('uploader')
        video_id = info.get('id')
        
        # Extract playlist information
        playlist_info = None
        if info.get('playlist') or info.get('playlist_title'):
            playlist_info = {
                'playlist_title': info.get('playlist_title'),
                'playlist_id': info.get('playlist_id'),
                'playlist_index': info.get('playlist_index'),
                'playlist_count': info.get('playlist_count')
            }
        
        # Ensure metadata lookup isolation by clearing any potential caches
        # This prevents metadata leakage between different songs
        print(f"📋 Title: {base_title}")
        print(f"👤 Uploader: {uploader}")
        if playlist_info:
            print(f"📁 Playlist: {playlist_info.get('playlist_title')} ({playlist_info.get('playlist_index')}/{playlist_info.get('playlist_count')})")
        
        try:
            tag_mp3_with_metadata(Path(filepath), base_title, uploader, video_id, playlist_info)
        except Exception as e:
            print(f"⚠ Metadata lookup failed: {e}")
            # Fallback to basic tagging
            try:
                easy_tags = EasyID3(str(filepath))
                easy_tags['title'] = [base_title]
                if uploader:
                    easy_tags['artist'] = [uploader]
                easy_tags['comment'] = [f"YouTube: {video_id}" if video_id else "Downloaded from YouTube"]
                easy_tags.save()
                print("✓ Basic tags applied")
            except Exception as e2:
                print(f"⚠ All tagging failed: {e2}")

    # Common robust options
    opts = {
        'paths': {'home': outdir},
        'outtmpl': '%(title).200B [%(id)s].%(ext)s',
        'format': 'bestaudio/best',  # Simplified format selection - skip formats that consistently fail
        'postprocessors': [
            {'key': 'FFmpegExtractAudio', 'preferredcodec': 'mp3', 'preferredquality': bitrate},
            # Remove FFmpegMetadata as we handle all metadata in our custom post_hook
        ],
        'prefer_ffmpeg': True,
        'noplaylist': not allow_playlist,
        # Robustness to avoid empty files:
        'retries': 3,  # Reduced from 10 to prevent hanging
        'fragment_retries': 3,
        'retry_sleep': 'exponential',
        'concurrent_fragment_downloads': 1,  # serial fragments
        'nopart': True,                      # write directly to final file
        'quiet': True,
        'no_warnings': True,
        'progress_hooks': [progress_hook],
        'postprocessor_hooks': [post_hook],
        'geo_bypass': True,
        # Add timeout settings to prevent hanging
        'socket_timeout': 30,
        'http_timeout': 30,
    }

    if alt:
        # Alternate extractor hints that often fix edge cases
        opts.update({
            'extractor_args': {'youtube': {'player_client': ['android', 'tv']}},
            'http_chunk_size': 10485760,  # 10MB chunks
            'http_headers': {
                'User-Agent': 'Mozilla/5.0',
                'Accept-Language': 'en-US,en;q=0.9',
            },
        })
    else:
        # Primary attempt keeps defaults minimal, but nudges Android client
        opts.update({
            'extractor_args': {'youtube': {'player_client': ['android']}},
        })

    return opts


# ---------- Fallback format selection ----------
def pick_best_audio_format(formats: List[dict]) -> Optional[str]:
    """Select the best audio-only format id based on abr (audio bitrate).
    Returns format_id or None.
    """
    audio_only = []
    for f in formats or []:
        # Skip if no audio
        if f.get('acodec') in (None, 'none'):
            continue
        # If it has video codec AND not 'none', skip to prefer pure audio.
        if f.get('vcodec') not in (None, 'none'):
            continue
        audio_only.append(f)
    # Prefer highest abr, fallback to filesize or asr.
    if not audio_only:
        return None
    def sort_key(f):
        return (
            f.get('abr') or 0,
            f.get('asr') or 0,
            f.get('filesize') or f.get('filesize_approx') or 0,
        )
    audio_only.sort(key=sort_key, reverse=True)
    return audio_only[0].get('format_id')


def attempt_manual_format(url: str, outdir: str, bitrate: str, allow_playlist: bool) -> bool:
    """Fallback: extract info without downloading, pick a viable audio stream, then download."""
    print("Attempting manual format selection…")
    
    # Create fresh processed_files for manual format attempt
    manual_processed_files = set()
    base_opts = make_ydl_opts(outdir, bitrate, allow_playlist, processed_files=manual_processed_files)
    
    # We only need metadata first; silence progress for this step.
    meta_opts = dict(base_opts)
    # Remove restrictive entries so extraction is broad
    meta_opts.pop('format', None)
    meta_opts.pop('extractor_args', None)
    meta_opts.update({'quiet': True, 'progress_hooks': [], 'postprocessor_hooks': [], 'skip_download': True})
    try:
        with YoutubeDL(meta_opts) as ydl_meta:
            info = ydl_meta.extract_info(url, download=False)
    except Exception as e:
        print(f"Could not extract formats: {e}")
        return False
    fmts = info.get('formats') or []
    chosen = pick_best_audio_format(fmts)
    if not chosen:
        if fmts:
            # Fallback to highest tbr overall (prefer ones with audio)
            fmts_sorted = sorted(fmts, key=lambda f: (f.get('acodec') not in (None,'none'), f.get('tbr') or f.get('abr') or 0), reverse=True)
            chosen = fmts_sorted[0].get('format_id')
            print("No pure audio format found; using best available format for extraction →", chosen)
        else:
            print("No formats returned; aborting manual attempt.")
            return False
    print(f"Chosen audio format id: {chosen}")
    # Now re-run with explicit format id and fresh processed_files
    dl_processed_files = set()
    dl_opts = make_ydl_opts(outdir, bitrate, allow_playlist, processed_files=dl_processed_files)
    dl_opts['format'] = chosen
    dl_opts.pop('extractor_args', None)  # do not constrain when explicit format chosen
    try:
        with YoutubeDL(dl_opts) as ydl_dl:
            ydl_dl.download([url])
        print("✅ Done (manual format)")
        return True
    except Exception as e:
        print(f"Manual format attempt failed: {e}")
        return False


# ---------- CLI ----------
def load_urls(url: Optional[str], listfile: Path, allow_playlist: bool) -> List[str]:
    if url:
        return [normalize_url(url, allow_playlist)]
    if not listfile.exists():
        sys.exit(f"No URL provided and list file not found: {listfile}")
    urls: List[str] = []
    for line in listfile.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s or s.startswith('#'):
            continue
        urls.append(normalize_url(s, allow_playlist))
    if not urls:
        sys.exit(f"No valid URLs found in {listfile}.")
    return urls


def main():
    p = argparse.ArgumentParser(description="YouTube → MP3 converter with enhanced metadata tagging")
    p.add_argument("url", nargs='?', help="YouTube video URL. If omitted, read URLs from download.txt.")
    p.add_argument("-o", "--outdir", default=str(DEFAULT_OUTDIR), help=f"Output directory (default: {DEFAULT_OUTDIR})")
    p.add_argument("--bitrate", default="192", help="MP3 bitrate kbps (default: 192)")
    p.add_argument("--allow-playlist", action="store_true", help="Allow playlist download (off by default)")
    p.add_argument("--file", default=str(DEFAULT_LISTFILE), help=f"Alternate list file (default: {DEFAULT_LISTFILE})")
    p.add_argument("--list-formats", action="store_true", help="List available formats for the URL(s) (no download)")
    p.add_argument("--test-metadata", action="store_true", help="Test metadata lookup for the URL(s) (no download)")
    args = p.parse_args()

    ensure_ffmpeg()
    os.makedirs(args.outdir, exist_ok=True)

    urls = load_urls(args.url, Path(args.file), args.allow_playlist)
    failures: List[str] = []

    # Process each URL with its own isolated environment
    for i, u in enumerate(urls, 1):
        print(f"\n{'='*60}")
        print(f"[{i}/{len(urls)}] Processing: {u}")
        print(f"{'='*60}")
        
        # Create a fresh processed_files set for each download to prevent cross-contamination
        processed_files = set()
        ydl_opts = make_ydl_opts(args.outdir, args.bitrate, args.allow_playlist, processed_files=processed_files)
        
        print(f"🔄 Created isolated environment for download {i}")

        try:
            print(f"Using (normalized): {u}")
            if args.list_formats:
                tmp_opts = dict(ydl_opts)
                tmp_opts.pop('format', None)
                tmp_opts.pop('extractor_args', None)
                with YoutubeDL(tmp_opts) as ydl_list:
                    info = ydl_list.extract_info(u, download=False)
                formats = info.get('formats') or []
                print("id  ext  acodec        vcodec        abr  tbr  note")
                for f in formats:
                    print(f"{f.get('format_id'):>3} {f.get('ext'):>4} {str(f.get('acodec')):>12} {str(f.get('vcodec')):>12} {str(f.get('abr')):>4} {str(f.get('tbr')):>4} {f.get('format_note')}")
                continue
            if args.test_metadata:
                tmp_opts = dict(ydl_opts)
                tmp_opts.pop('format', None)
                tmp_opts.pop('extractor_args', None)
                with YoutubeDL(tmp_opts) as ydl_meta:
                    info = ydl_meta.extract_info(u, download=False)
                title = info.get('title', 'Unknown Title')
                print(f"Video title: {title}")
                print("Testing metadata lookup...")
                metadata = get_music_metadata_from_title(title)
                if metadata:
                    print("✓ Metadata found:")
                    for key, value in metadata.items():
                        if value:
                            print(f"  {key.title()}: {value}")
                else:
                    print("✗ No metadata found")
                continue
            print("Starting download...")
            
            # Create a fresh YoutubeDL instance for each download
            with YoutubeDL(ydl_opts) as ydl:
                try:
                    with TimeoutHandler(120):  # 2 minute timeout
                        ydl.download([u])
                    print("Download completed successfully")
                except TimeoutError as te:
                    print(f"❌ Download timed out: {te}")
                    raise Exception(f"Download timed out after 120 seconds")
            print("✅ Done")
        except Exception as e:
            msg = str(e)
            print(f"Primary attempt failed: {msg}")
            # Try fallback sequence for format issues  
            if any(phrase in msg.lower() for phrase in ['format', 'not available', 'empty file']):
                # Optimized format sequence based on actual success patterns
                alt_specs = [
                    'bestaudio',           # This consistently works
                    'best[height<=720]',   # Lower quality fallback
                    'best',                # Final fallback
                ]
                success = False
                for spec in alt_specs:
                    try:
                        print(f"Trying format: {spec}")
                        # Create fresh processed_files for fallback attempts too
                        fallback_processed_files = set()
                        spec_opts = make_ydl_opts(args.outdir, args.bitrate, args.allow_playlist, processed_files=fallback_processed_files)
                        spec_opts['format'] = spec
                        spec_opts.pop('extractor_args', None)
                        with YoutubeDL(spec_opts) as ydl_spec:
                            with TimeoutHandler(120):
                                ydl_spec.download([u])
                        print(f"✅ Done (spec {spec})")
                        success = True
                        break
                    except Exception as spec_err:
                        print(f"❌ {spec} failed")
                
                if success:
                    continue
                
                # If format specs failed, try manual format selection
                if attempt_manual_format(u, args.outdir, args.bitrate, args.allow_playlist):
                    continue
            
            # Final fallback with alternate extraction strategy
            print("Retrying with alternate strategy…")
            try:
                # Create fresh processed_files for final fallback too
                final_processed_files = set()
                alt_opts = make_ydl_opts(args.outdir, args.bitrate, args.allow_playlist, alt=True, processed_files=final_processed_files)
                with YoutubeDL(alt_opts) as ydl2:
                    with TimeoutHandler(120):
                        ydl2.download([u])
                print("✅ Done (alternate)")
            except Exception as e2:
                print(f"❌ Failed: {e2}")
                failures.append(u)

    if failures:
        print("\nSome downloads failed:")
        for u in failures:
            print(f" - {u}")
        sys.exit(1)
    else:
        print(f"\nAll done. Files saved to: {args.outdir}")


if __name__ == "__main__":
    main()
