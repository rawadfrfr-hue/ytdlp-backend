import os
import re
import urllib.parse
from typing import Optional, List
from fastapi import FastAPI, HTTPException, Header, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import yt_dlp

# --- CONFIGURATION ---
API_KEY = os.getenv("API_KEY", "my_secret_ytdlp_key_2026")
COOKIE_FILE = os.getenv("COOKIE_FILE", "cookies.txt")

app = FastAPI(
    title="VPS yt-dlp Backend Service",
    description="High-performance media extractor and direct streaming engine for YouTube, Instagram, TikTok & Facebook",
    version="1.0.0"
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

def verify_api_key(x_api_key: Optional[str] = Header(None)):
    # If API_KEY is custom configured, enforce it. Otherwise allow default key or open access.
    if API_KEY and API_KEY != "my_secret_ytdlp_key_2026" and x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid X-API-KEY header")

def get_ydl_options(extract_flat: bool = False):
    cookie_path = COOKIE_FILE if os.path.exists(COOKIE_FILE) else None
    return {
        'quiet': True,
        'no_warnings': True,
        'cookiefile': cookie_path,
        'noplaylist': True,
        'geo_bypass': True,
        'extract_flat': extract_flat,
        'socket_timeout': 15,
        'nocheckcertificate': True,
    }

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

            # Process video and audio formats
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
                is_audio = (acodec != 'none' and not is_video)

                if is_video:
                    res_label = f"{height}p" if height else (f.get('resolution') or f.get('format_note') or 'HD')
                    quality_desc = f"{res_label} · {ext.upper()}"
                    
                    key = f"v-{res_label}"
                    if key in seen_res:
                        continue
                    seen_res.add(key)

                    # Build stream proxy URL for seamless direct downloading
                    base_url = get_base_url(request)
                    stream_url = f"{base_url}/api/stream?url={urllib.parse.quote(raw_url)}&format_id={format_id}&title={urllib.parse.quote(info.get('title', 'video')[:50])}"

                    formats.append({
                        "id": str(format_id),
                        "label": f"{res_label} Video ({ext.upper()})",
                        "resolution": f"{width}x{height}" if width and height else res_label,
                        "format": ext.upper(),
                        "type": "video",
                        "fileSize": f"{(f.get('filesize') or f.get('filesize_approx', 0)) / (1024*1024):.1f} MB" if (f.get('filesize') or f.get('filesize_approx')) else "Original HD",
                        "quality": quality_desc,
                        "hasAudio": acodec != 'none',
                        "downloadUrl": stream_url,
                        "directUrl": f_url,
                        "recommended": height >= 1080 or ('1080' in res_label)
                    })

            # Add Best MP3 / Audio Option
            best_audio = next((f for f in raw_formats if f.get('acodec') != 'none' and f.get('vcodec') == 'none'), None)
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
                "directUrl": best_audio.get('url') if best_audio else None,
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
    # Support api key query parameter or header for direct link downloads
    if API_KEY and x_api_key and x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Unauthorized")

    clean_media_url = clean_url(url)
    opts = get_ydl_options(extract_flat=False)

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(clean_media_url, download=False)
            if not info:
                raise HTTPException(status_code=404, detail="Media not found")

            # Find matching format URL
            raw_formats = info.get('formats', [])
            target_format = None

            if format_id == "bestaudio" or type == "audio":
                target_format = next((f for f in raw_formats if f.get('acodec') != 'none' and f.get('vcodec') == 'none'), None)
            else:
                target_format = next((f for f in raw_formats if str(f.get('format_id')) == str(format_id)), None)

            if not target_format:
                target_format = raw_formats[-1] if raw_formats else None

            if not target_format or not target_format.get('url'):
                raise HTTPException(status_code=404, detail="Format stream URL unavailable")

            download_url = target_format.get('url')
            ext = 'mp3' if type == 'audio' else (target_format.get('ext') or 'mp4')
            safe_title = re.sub(r'[^\w\s-]', '', title or info.get('title') or 'video').strip().replace(' ', '_')
            filename = f"{safe_title[:40]}.{ext}"

            # Stream using urllib request to client
            import urllib.request
            req = urllib.request.Request(
                download_url,
                headers={
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                    'Accept': '*/*'
                }
            )
            response = urllib.request.urlopen(req, timeout=30)
            
            content_type = 'audio/mpeg' if ext == 'mp3' else 'video/mp4'

            def iterfile():
                while True:
                    chunk = response.read(64 * 1024)
                    if not chunk:
                        break
                    yield chunk

            headers = {
                "Content-Disposition": f'attachment; filename="{filename}"',
                "Content-Type": content_type,
            }
            if response.headers.get('Content-Length'):
                headers["Content-Length"] = response.headers.get('Content-Length')

            return StreamingResponse(iterfile(), headers=headers)

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Streaming error: {str(e)[:250]}")

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)
