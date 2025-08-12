#!/usr/bin/env python3
"""
YouTube → MP3 downloader (no GUI, Tk-free)

Behavior (unchanged):
- Pass a URL → downloads that one.
- Pass no URL → reads URLs from download.txt (one per line, '#' comments ok).

Fixes:
- Strips playlist params unless --allow-playlist, and prints the normalized URL.
- Robust retries to avoid "downloaded file is empty".
- If the primary attempt fails, an alternate strategy is tried automatically.

Enhancement:
- After successful download, MP3 is tagged (title/artist/album/genre/year + cover) using iTunes Search.
"""

import argparse
import os
import sys
import shutil
import re
from pathlib import Path
from typing import List, Optional, Tuple
from urllib.parse import urlparse, parse_qsl, urlencode, urlunparse

import requests
from yt_dlp import YoutubeDL
from mutagen.easyid3 import EasyID3
from mutagen.id3 import ID3, APIC, ID3NoHeaderError
from mutagen.mp3 import MP3

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_OUTDIR = SCRIPT_DIR / "downloads"
DEFAULT_LISTFILE = SCRIPT_DIR / "download.txt"
ITUNES_SEARCH_URL = "https://itunes.apple.com/search"


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


# ---------- Metadata helpers ----------
def split_artist_title(title: str) -> Tuple[Optional[str], str]:
    m = re.match(r"^(.+?)\s+[-–]\s+(.+)$", title)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return None, title.strip()


def itunes_lookup(query: str) -> Optional[dict]:
    params = {"term": query, "media": "music", "entity": "song", "limit": 1}
    try:
        r = requests.get(ITUNES_SEARCH_URL, params=params, timeout=10)
        r.raise_for_status()
        data = r.json()
        if data.get("resultCount"):
            return data["results"][0]
    except Exception:
        return None
    return None


def download_artwork(url: str) -> Optional[bytes]:
    try:
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        return r.content
    except Exception:
        return None


def tag_mp3(mp3_path: Path, base_title: str, uploader: Optional[str] = None):
    artist_guess, track_guess = split_artist_title(base_title)
    if not artist_guess and uploader:
        artist_guess = uploader

    query = f"{artist_guess} {track_guess}".strip() if artist_guess else track_guess
    it = itunes_lookup(query)

    title = (it.get("trackName") if it else track_guess) or track_guess
    artist = (it.get("artistName") if it else (artist_guess or uploader or "")).strip()
    album = (it.get("collectionName") if it else "").strip()
    genre = (it.get("primaryGenreName") if it else "").strip()
    year = (it.get("releaseDate", "")[:4] if it and it.get("releaseDate") else "").strip()

    try:
        audio = EasyID3(str(mp3_path))
    except ID3NoHeaderError:
        audio = MP3(str(mp3_path))
        audio.add_tags()
        audio = EasyID3(str(mp3_path))

    if title:  audio['title']  = title
    if artist: audio['artist'] = artist
    if album:  audio['album']  = album
    if genre:  audio['genre']  = genre
    if year:   audio['date']   = year
    audio.save()

    cover_url = None
    if it and it.get("artworkUrl100"):
        cover_url = it["artworkUrl100"].replace("100x100bb", "600x600bb")
    if cover_url:
        art = download_artwork(cover_url)
        if art:
            tags = ID3(str(mp3_path))
            # remove existing APIC frames to avoid duplicates
            for k in list(tags.keys()):
                if k.startswith('APIC'):
                    del tags[k]
            tags.add(APIC(encoding=3, mime='image/jpeg', type=3, desc='Cover', data=art))
            tags.save(v2_version=3)


# ---------- yt-dlp options ----------
def make_ydl_opts(outdir: str, bitrate: str, allow_playlist: bool, alt: bool = False):
    def post_hook(d):
        # After post-processing; MP3 should exist now.
        if d.get('status') == 'finished':
            filepath = d.get('filepath')
            info = d.get('info_dict', {})
            if filepath and filepath.endswith('.mp3'):
                base_title = info.get('title') or Path(filepath).stem
                uploader = info.get('artist') or info.get('uploader')
                print("Tagging metadata…", flush=True)
                try:
                    tag_mp3(Path(filepath), base_title=base_title, uploader=uploader)
                    print("Tags updated (title/artist/album/genre/year + cover art)")
                except Exception as e:
                    print(f"Tagging skipped: {e}")

    # Common robust options
    opts = {
        'paths': {'home': outdir},
        'outtmpl': '%(title).200B [%(id)s].%(ext)s',
        'format': 'bestaudio/best',
        'postprocessors': [
            {'key': 'FFmpegExtractAudio', 'preferredcodec': 'mp3', 'preferredquality': bitrate},
            {'key': 'FFmpegMetadata'},
        ],
        'prefer_ffmpeg': True,
        'noplaylist': not allow_playlist,
        # Robustness to avoid empty files:
        'retries': 10,
        'fragment_retries': 10,
        'retry_sleep': 'exponential',
        'concurrent_fragment_downloads': 1,  # serial fragments
        'nopart': True,                      # write directly to final file
        'quiet': True,
        'no_warnings': True,
        'progress_hooks': [progress_hook],
        'postprocessor_hooks': [post_hook],
        'geo_bypass': True,
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
    p = argparse.ArgumentParser(description="YouTube → MP3 (batch via download.txt, robust + auto-tagging)")
    p.add_argument("url", nargs='?', help="YouTube video URL. If omitted, read URLs from download.txt.")
    p.add_argument("-o", "--outdir", default=str(DEFAULT_OUTDIR), help=f"Output directory (default: {DEFAULT_OUTDIR})")
    p.add_argument("--bitrate", default="192", help="MP3 bitrate kbps (default: 192)")
    p.add_argument("--allow-playlist", action="store_true", help="Allow playlist download (off by default)")
    p.add_argument("--file", default=str(DEFAULT_LISTFILE), help=f"Alternate list file (default: {DEFAULT_LISTFILE})")
    args = p.parse_args()

    ensure_ffmpeg()
    os.makedirs(args.outdir, exist_ok=True)

    urls = load_urls(args.url, Path(args.file), args.allow_playlist)
    failures: List[str] = []

    ydl_opts = make_ydl_opts(args.outdir, args.bitrate, args.allow_playlist)

    with YoutubeDL(ydl_opts) as ydl:
        for i, u in enumerate(urls, 1):
            print(f"\n[{i}/{len(urls)}] {u}")
            try:
                print(f"Using (normalized): {u}")
                ydl.download([u])
                print("✅ Done")
            except Exception as e:
                print(f"Primary attempt failed: {e}\nRetrying with alternate strategy…")
                try:
                    alt_opts = make_ydl_opts(args.outdir, args.bitrate, args.allow_playlist, alt=True)
                    with YoutubeDL(alt_opts) as ydl2:
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