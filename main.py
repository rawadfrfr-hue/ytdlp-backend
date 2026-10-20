import os
import re
import urllib.parse
import uuid
import glob
from typing import Optional, List
from fastapi import FastAPI, HTTPException, Header, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from starlette.background import BackgroundTask
from pydantic import BaseModel
import yt_dlp

# --- CONFIGURATION ---
API_KEY = os.getenv("API_KEY", "my_secret_ytdlp_key_2026")
COOKIE_FILE = os.getenv("COOKIE_FILE", "cookies.txt")
TEMP_DIR = "/tmp/ytdlp_downloads"

os.makedirs(TEMP_DIR, exist_ok=True)

app = FastAPI(
    title="Railway yt-dlp Backend Service",
    description="High-performance media extractor and direct streaming engine for YouTube, Instagram, TikTok & Facebook",
    version="1.1.0"
)

# Allow CORS for Next.js app
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class ExtractRequest(BaseModel):
    url: str

def cleanup_file(filepath: str):
    """Deletes temporary downloaded file after user finishes downloading."""
    try:
        if os.path.exists(filepath):
            os.remove(filepath)
    except Exception as e:
        print(f"Cleanup error: {e}")

def verify_api_key(x_api_key: Optional[str] = Header(None)):
    # If custom API_KEY is set in environment, check it. Otherwise allow default.
    if API_KEY and API_KEY != "my_secret_ytdlp_key_2026" and x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid X-API-KEY header")

PROXIES_FILE = os.getenv("PROXIES_FILE", "proxies.txt")

def load_proxies() -> List[str]:
    proxies = []
    if os.path.exists(PROXIES_FILE):
        try:
            with open(PROXIES_FILE, "r") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    parts = line.split(":")
                    if len(parts) == 4:
                        host, port, user, password = parts
                        proxies.append(f"http://{urllib.parse.quote(user)}:{urllib.parse.quote(password)}@{host}:{port}")
                    elif len(parts) == 2:
                        proxies.append(f"http://{parts[0]}:{parts[1]}")
                    elif line.startswith("http://") or line.startswith("https://") or line.startswith("socks5://"):
                        proxies.append(line)
        except Exception as e:
            print(f"Error loading proxies: {e}")
    return proxies

LOADED_PROXIES = load_proxies()
proxy_cursor = 0

def get_next_proxy() -> Optional[str]:
    global proxy_cursor
    env_proxy = os.getenv("PROXY_URL") or os.getenv("HTTP_PROXY") or os.getenv("HTTPS_PROXY")
    if env_proxy:
        return env_proxy
    if not LOADED_PROXIES:
        return None
    p = LOADED_PROXIES[proxy_cursor % len(LOADED_PROXIES)]
    proxy_cursor = (proxy_cursor + 1) % len(LOADED_PROXIES)
    return p

def get_ydl_options(extract_flat: bool = False, proxy: Optional[str] = None):
    cookie_path = COOKIE_FILE if os.path.exists(COOKIE_FILE) else None
    proxy_to_use = proxy if proxy is not None else get_next_proxy()
    opts = {
        'quiet': True,
        'no_warnings': True,
        'cookiefile': cookie_path,
        'noplaylist': True,
        'geo_bypass': True,
        'extract_flat': extract_flat,
        'socket_timeout': 25,
        'nocheckcertificate': True,
        'extractor_args': {
            'youtube': {
                'player_client': ['android', 'ios', 'web_creator']
            }
        }
    }
    if proxy_to_use:
        opts['proxy'] = proxy_to_use
    return opts

def clean_url(url: str) -> str:
    match = re.search(r'https?://[^\s<>"]+', url)
    return match.group(0) if match else url.strip()

def get_base_url(request: Request) -> str:
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    host = request.headers.get("host") or request.url.netloc
    return f"{proto}://{host}".rstrip('/')

@app.get("/")
def home_root():
    return {
        "status": "online",
        "service": "Railway yt-dlp Backend",
        "message": "Railway yt-dlp backend is running successfully! 🚀",
        "yt_dlp_version": yt_dlp.version.__version__,
        "endpoints": ["/health", "/api/extract", "/api/stream"]
    }

@app.get("/health")
def health_check():
    cookie_present = os.path.exists(COOKIE_FILE)
    return {
        "status": "online",
        "service": "yt-dlp-vps-backend",
        "yt_dlp_version": yt_dlp.version.__version__,
        "cookie_file_configured": cookie_present
    }

@app.post("/api/extract")
def extract_media(req: ExtractRequest, x_api_key: Optional[str] = Header(None), request: Request = None):
    verify_api_key(x_api_key)
    
    raw_url = clean_url(req.url)
    if not raw_url:
        raise HTTPException(status_code=400, detail="Invalid URL provided")

    opts = get_ydl_options(extract_flat=False)
    
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(raw_url, download=False)
            if not info:
                raise HTTPException(status_code=404, detail="Could not extract media info")
            
            raw_formats = info.get('formats', [])
            formats = []
            seen_res = set()

            # Process video formats
            for f in raw_formats:
                f_url = f.get('url')
                if not f_url or f.get('ext') == 'mhtml':
                    continue

                vcodec = f.get('vcodec', 'none')
                acodec = f.get('acodec', 'none')
                ext = f.get('ext', 'mp4')
                format_id = f.get('format_id')
                height = f.get('height') or 0
                width = f.get('width') or 0

                is_video = (vcodec != 'none')

                if is_video and height >= 144:
                    res_label = f"{height}p"
                    key = f"v-{res_label}"
                    if key in seen_res:
                        continue
                    seen_res.add(key)

                    base_url = get_base_url(request)
                    stream_url = f"{base_url}/api/stream?url={urllib.parse.quote(raw_url)}&format_id={format_id}&title={urllib.parse.quote(info.get('title', 'video')[:50])}"

                    formats.append({
                        "id": str(format_id),
                        "label": f"{res_label} Video (MP4)",
                        "resolution": f"{width}x{height}" if width and height else res_label,
                        "format": "MP4",
                        "type": "video",
                        "fileSize": f"{(f.get('filesize') or f.get('filesize_approx', 0)) / (1024*1024):.1f} MB" if (f.get('filesize') or f.get('filesize_approx')) else "Original HD",
                        "quality": f"{res_label} · MP4 Clean Video",
                        "hasAudio": True,
                        "downloadUrl": stream_url,
                        "recommended": height >= 1080 or height == 720
                    })

            # Add Best MP3 Audio Option
            base_url = get_base_url(request)
            audio_stream_url = f"{base_url}/api/stream?url={urllib.parse.quote(raw_url)}&format_id=bestaudio&type=audio&title={urllib.parse.quote(info.get('title', 'audio')[:50])}"
            
            formats.append({
                "id": "best-audio",
                "label": "Original MP3 Audio",
                "resolution": "Audio",
                "format": "MP3",
                "type": "audio",
                "fileSize": "Studio Audio",
                "quality": "Extracted High Bitrate Audio",
                "hasAudio": True,
                "downloadUrl": audio_stream_url,
                "recommended": False
            })

            # Sort so highest resolution is first
            formats.sort(key=lambda x: (x["type"] == "video", x["recommended"]), reverse=True)

            return {
                "success": True,
                "id": str(info.get("id")),
                "title": info.get("title") or "Extracted Media",
                "author": info.get("uploader") or info.get("channel") or "Media Creator",
                "authorHandle": f"@{info.get('uploader_id')}" if info.get('uploader_id') else None,
                "platform": info.get("extractor_key", "generic").lower(),
                "duration": info.get("duration_string") or (f"{info.get('duration')}s" if info.get('duration') else "Original"),
                "durationSeconds": info.get("duration", 0),
                "thumbnail": info.get("thumbnail") or (info.get("thumbnails")[-1]["url"] if info.get("thumbnails") else None),
                "views": f"{info.get('view_count'):,} views" if info.get('view_count') else None,
                "sourceUrl": raw_url,
                "formats": formats[:10]
            }

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"yt-dlp error: {str(e)[:250]}")

@app.get("/api/stream")
def stream_media(
    url: str = Query(..., description="Target media URL"),
    format_id: str = Query("best", description="Target format ID or 'best'/'bestaudio'"),
    type: str = Query("video", description="'video' or 'audio'"),
    title: Optional[str] = Query("download", description="Desired filename base"),
    x_api_key: Optional[str] = Query(None, description="Optional API key via query or header")
):
    if API_KEY and API_KEY != "my_secret_ytdlp_key_2026" and x_api_key and x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Unauthorized")

    clean_media_url = clean_url(url)
    if not clean_media_url:
        raise HTTPException(status_code=400, detail="Invalid media URL")

    # Safe filename sanitization
    safe_title = re.sub(r'[^\w\s-]', '', title or 'video').strip().replace(' ', '_')[:40] or "video"
    unique_id = uuid.uuid4().hex[:8]
    is_audio = (type == "audio" or format_id == "bestaudio")

    os.makedirs(TEMP_DIR, exist_ok=True)
    cookie_path = COOKIE_FILE if os.path.exists(COOKIE_FILE) else None

    if is_audio:
        ydl_opts = {
            'format': 'bestaudio/best',
            'outtmpl': f"{TEMP_DIR}/{unique_id}_{safe_title}.%(ext)s",
            'postprocessors': [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': '192',
            }],
            'cookiefile': cookie_path,
            'quiet': True,
            'no_warnings': True,
            'geo_bypass': True,
            'noplaylist': True,
            'socket_timeout': 30,
            'extractor_args': {
                'youtube': {
                    'player_client': ['android', 'ios', 'web_creator']
                }
            }
        }
        content_type = "audio/mpeg"
    else:
        # Merge requested video format + best audio with FFmpeg into an authoritative MP4
        fmt = f"{format_id}+bestaudio/bestvideo+bestaudio/best" if format_id and format_id not in ["best", "default"] else "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best"
        ydl_opts = {
            'format': fmt,
            'outtmpl': f"{TEMP_DIR}/{unique_id}_{safe_title}.%(ext)s",
            'merge_output_format': 'mp4',
            'cookiefile': cookie_path,
            'quiet': True,
            'no_warnings': True,
            'geo_bypass': True,
            'noplaylist': True,
            'socket_timeout': 30,
            'extractor_args': {
                'youtube': {
                    'player_client': ['android', 'ios', 'web_creator']
                }
            }
        }
        content_type = "video/mp4"

    proxy_to_use = get_next_proxy()
    if proxy_to_use:
        ydl_opts['proxy'] = proxy_to_use

    try:
        # Download and remux using yt-dlp + FFmpeg
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([clean_media_url])

        matched_files = glob.glob(f"{TEMP_DIR}/{unique_id}_*")
        if not matched_files:
            raise HTTPException(status_code=404, detail="Media file could not be prepared")

        target_file = matched_files[0]
        ext = target_file.split('.')[-1]
        final_filename = f"{safe_title}.{ext}"

        # Send genuine binary file with Content-Length, Range support and automatic background deletion
        return FileResponse(
            path=target_file,
            filename=final_filename,
            media_type=content_type,
            background=BackgroundTask(cleanup_file, target_file)
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Streaming error: {str(e)[:250]}")

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)
