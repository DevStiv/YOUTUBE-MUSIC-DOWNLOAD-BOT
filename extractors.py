import os
import re
import html
import uuid
import time
import glob
import asyncio
import aiohttp
import yt_dlp
from config import DOWNLOAD_DIR, MAX_CONCURRENT_DOWNLOADS, COOKIES_FILE

download_semaphore = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)

URL_REGEX = re.compile(r"https?://[^\s<>\"']+")
SPOTIFY_TRACK_REGEX = re.compile(r"https?://open\.spotify\.com/(intl-[a-z]{2}/)?track/([a-zA-Z0-9]+)")
DIRECT_IMAGE_REGEX = re.compile(r"\.(jpg|jpeg|png|webp)(\?.*)?$", re.IGNORECASE)

PRIORITY_DOMAINS = (
    "youtube.com", "youtu.be", "spotify.com", "soundcloud.com",
    "tiktok.com", "instagram.com", "facebook.com", "fb.watch",
    "twitter.com", "x.com", "pinterest.com", "pin.it", "reddit.com", "bandcamp.com"
)


def get_ydl_base_opts() -> dict:
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "geo_bypass": True,
        "nocheckcertificate": True,
        "extractor_args": {
            "youtube": {
                "player_client": ["ios", "android", "tv", "web"],
            }
        },
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            )
        },
    }
    if os.path.exists(COOKIES_FILE) and os.path.getsize(COOKIES_FILE) > 10:
        opts["cookiefile"] = COOKIES_FILE
    return opts


def detect_platform(url: str) -> str:
    u = url.lower()
    if "youtube.com" in u or "youtu.be" in u:
        return "YouTube Music" if "music.youtube" in u else "YouTube"
    if "spotify.com" in u:
        return "Spotify"
    if "tiktok.com" in u:
        return "TikTok"
    if "instagram.com" in u:
        return "Instagram"
    if "facebook.com" in u or "fb.watch" in u:
        return "Facebook"
    if "twitter.com" in u or "x.com" in u:
        return "X / Twitter"
    if "soundcloud.com" in u:
        return "SoundCloud"
    if "pinterest.com" in u or "pin.it" in u:
        return "Pinterest"
    if "reddit.com" in u:
        return "Reddit"
    return "Multimedia Web"


def extract_best_url(text: str) -> str | None:
    urls = URL_REGEX.findall(text)
    if not urls:
        return None
    for u in urls:
        if any(domain in u.lower() for domain in PRIORITY_DOMAINS):
            return u
    return urls[0]


async def unshorten_or_resolve_aggregator(url: str) -> str:
    """Resuelve redirecciones y extrae enlaces de agregadores como orcd.co o linktr.ee."""
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    try:
        async with aiohttp.ClientSession(headers=headers) as session:
            async with session.get(url, allow_redirects=True, timeout=8) as resp:
                final_url = str(resp.url)
                if any(d in final_url.lower() for d in PRIORITY_DOMAINS):
                    return final_url
                body = await resp.text(errors="ignore")
                yt_match = re.search(
                    r"https?://(?:www\.|music\.)?(?:youtube\.com/watch\?v=[\w\-]{11}|youtu\.be/[\w\-]{11})",
                    body
                )
                if yt_match:
                    return yt_match.group(0)
                sp_match = SPOTIFY_TRACK_REGEX.search(body)
                if sp_match:
                    return sp_match.group(0)
                return final_url
    except Exception:
        return url


async def resolve_spotify_to_query(spotify_url: str) -> str:
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    async with aiohttp.ClientSession(headers=headers) as session:
        async with session.get(spotify_url, timeout=10) as resp:
            page = await resp.text()

    title_match = re.search(r'<meta property="og:title" content="([^"]+)"', page)
    desc_match = re.search(r'<meta property="og:description" content="([^"]+)"', page)
    if not title_match:
        raise ValueError("No se pudo leer el enlace de Spotify.")

    track_title = html.unescape(title_match.group(1))
    artist = ""
    if desc_match:
        desc = html.unescape(desc_match.group(1))
        parts = [p.strip() for p in desc.split("·")]
        if parts:
            artist = parts[0].replace("Listen to ", "").strip()

    return f"ytsearch1:{artist} - {track_title}"


def _extract_info_sync(target: str, platform_override: str = None) -> dict:
    ydl_opts = {
        **get_ydl_base_opts(),
        "skip_download": True,
        "extract_flat": False,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(target, download=False)
        if "entries" in info and info["entries"]:
            info = info["entries"][0]

        extractor = info.get("extractor_key", "Generic")
        vid_id = info.get("id") or str(uuid.uuid4())[:10]
        webpage = info.get("webpage_url") or target
        platform = platform_override or detect_platform(webpage)

        ext = (info.get("ext") or "").lower()
        vcodec = info.get("vcodec")
        is_direct_image = ext in ("jpg", "jpeg", "png", "webp") or (
            vcodec == "none" and not info.get("duration") and info.get("url", "").endswith((".jpg", ".png", ".webp"))
        )

        return {
            "uid": f"{extractor}_{vid_id}",
            "platform": platform,
            "title": info.get("track") or info.get("title") or "Contenido Multimedia",
            "artist": info.get("artist") or info.get("uploader") or info.get("creator") or platform,
            "duration": int(info.get("duration") or 0),
            "webpage_url": webpage,
            "thumbnail": info.get("thumbnail") or (info.get("url") if is_direct_image else None),
            "is_image": is_direct_image,
            "has_video": (vcodec != "none") and not is_direct_image and platform != "Spotify",
        }


async def get_media_metadata(input_text: str) -> dict:
    raw_url = extract_best_url(input_text)
    if raw_url:
        if not any(d in raw_url.lower() for d in PRIORITY_DOMAINS):
            raw_url = await unshorten_or_resolve_aggregator(raw_url)

        if DIRECT_IMAGE_REGEX.search(raw_url):
            uid = "IMG_" + str(uuid.uuid5(uuid.NAMESPACE_URL, raw_url))[:10]
            return {
                "uid": uid,
                "platform": detect_platform(raw_url),
                "title": "Imagen Directa HD",
                "artist": "Web",
                "duration": 0,
                "webpage_url": raw_url,
                "thumbnail": raw_url,
                "is_image": True,
                "has_video": False,
            }

        if SPOTIFY_TRACK_REGEX.search(raw_url):
            search_target = await resolve_spotify_to_query(raw_url)
            meta = await asyncio.to_thread(_extract_info_sync, search_target, "Spotify")
            meta["has_video"] = True
            return meta

        platform = detect_platform(raw_url)
        try:
            return await asyncio.to_thread(_extract_info_sync, raw_url, platform)
        except Exception:
            # Fallback inteligente: si un link de YouTube falla por bloqueo de IP, intenta buscar por su ID
            yt_id_match = re.search(r"(?:v=|youtu\.be/|shorts/)([\w\-]{11})", raw_url)
            if yt_id_match:
                vid_id = yt_id_match.group(1)
                return await asyncio.to_thread(_extract_info_sync, f"ytsearch1:{vid_id}", "YouTube")
            raise
    else:
        return await asyncio.to_thread(_extract_info_sync, f"ytsearch1:{input_text}", "YouTube Search")


def _search_inline_sync(query: str, limit: int = 6) -> list[dict]:
    ydl_opts = {
        **get_ydl_base_opts(),
        "skip_download": True,
        "extract_flat": True,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        res = ydl.extract_info(f"ytsearch{limit}:{query}", download=False)
        results = []
        for entry in res.get("entries", []):
            if not entry:
                continue
            results.append({
                "id": entry.get("id"),
                "title": entry.get("title", "Sin título"),
                "uploader": entry.get("uploader", "YouTube"),
                "duration": int(entry.get("duration") or 0),
                "url": f"https://www.youtube.com/watch?v={entry.get('id')}",
            })
        return results


async def search_tracks_inline(query: str, limit: int = 6) -> list[dict]:
    return await asyncio.to_thread(_search_inline_sync, query, limit)


def _download_audio_sync(url: str, codec: str, bitrate: str, unique_id: str) -> str:
    output_template = os.path.join(DOWNLOAD_DIR, f"{unique_id}.%(ext)s")
    ydl_opts = {
        **get_ydl_base_opts(),
        "format": "bestaudio/best",
        "outtmpl": output_template,
        "writethumbnail": True,
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": codec,
                "preferredquality": bitrate,
            },
            {"key": "EmbedThumbnail"},
            {"key": "FFmpegMetadata", "add_metadata": True},
        ],
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])
    return os.path.join(DOWNLOAD_DIR, f"{unique_id}.{codec}")


async def download_media_audio(url: str, codec: str = "mp3", bitrate: str = "128") -> str:
    unique_id = str(uuid.uuid4())
    async with download_semaphore:
        return await asyncio.to_thread(_download_audio_sync, url, codec, bitrate, unique_id)


def _download_video_sync(url: str, height: str, unique_id: str) -> str:
    output_template = os.path.join(DOWNLOAD_DIR, f"{unique_id}.%(ext)s")
    fmt_selector = (
        f"bestvideo[height<={height}][ext=mp4]+bestaudio[ext=m4a]/"
        f"bestvideo[height<={height}]+bestaudio/"
        f"best[height<={height}]/best"
    )
    ydl_opts = {
        **get_ydl_base_opts(),
        "format": fmt_selector,
        "outtmpl": output_template,
        "merge_output_format": "mp4",
        "max_filesize": 49 * 1024 * 1024,  # Límite seguro de Bot API estándar (50MB)
        "postprocessor_args": {
            "merger": ["-movflags", "+faststart"]
        },
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])

    mp4_path = os.path.join(DOWNLOAD_DIR, f"{unique_id}.mp4")
    if os.path.exists(mp4_path):
        return mp4_path

    matches = glob.glob(os.path.join(DOWNLOAD_DIR, f"{unique_id}.*"))
    if matches:
        return matches[0]
    raise FileNotFoundError("No se pudo generar el archivo de video.")


async def download_media_video(url: str, height: str = "480") -> str:
    unique_id = str(uuid.uuid4())
    async with download_semaphore:
        return await asyncio.to_thread(_download_video_sync, url, height, unique_id)


async def download_image_file(image_url: str) -> str:
    unique_id = str(uuid.uuid4())
    file_path = os.path.join(DOWNLOAD_DIR, f"{unique_id}.jpg")
    headers = {"User-Agent": "Mozilla/5.0"}
    async with aiohttp.ClientSession(headers=headers) as session:
        async with session.get(image_url, timeout=15) as resp:
            if resp.status != 200:
                raise ValueError("No se pudo descargar la imagen.")
            content = await resp.read()
            with open(file_path, "wb") as f:
                f.write(content)
    return file_path


def cleanup_files(file_path: str):
    if not file_path:
        return
    base = os.path.splitext(file_path)[0]
    for candidate in glob.glob(f"{base}*"):
        try:
            os.remove(candidate)
        except OSError:
            pass


async def janitor_cleanup_loop():
    while True:
        await asyncio.sleep(600)
        now = time.time()
        try:
            for fname in os.listdir(DOWNLOAD_DIR):
                fpath = os.path.join(DOWNLOAD_DIR, fname)
                if os.path.isfile(fpath) and (now - os.path.getmtime(fpath)) > 900:
                    os.remove(fpath)
        except Exception:
            pass
        
