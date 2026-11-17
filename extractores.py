import os
import re
import html
import uuid
import time
import asyncio
import aiohttp
import yt_dlp
from config import DOWNLOAD_DIR, MAX_CONCURRENT_DOWNLOADS

download_semaphore = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)

URL_REGEX = re.compile(r"https?://[^\s]+")
SPOTIFY_TRACK_REGEX = re.compile(r"https?://open\.spotify\.com/(intl-[a-z]{2}/)?track/([a-zA-Z0-9]+)")


def detect_platform(url: str) -> str:
    u = url.lower()
    if "youtube.com" in u or "youtu.be" in u:
        return "YouTube Music" if "music.youtube" in u else "YouTube"
    if "spotify.com" in u:
        return "Spotify"
    if "soundcloud.com" in u:
        return "SoundCloud"
    if "tiktok.com" in u:
        return "TikTok"
    if "instagram.com" in u:
        return "Instagram"
    if "facebook.com" in u or "fb.watch" in u:
        return "Facebook"
    if "twitter.com" in u or "x.com" in u:
        return "X / Twitter"
    if "bandcamp.com" in u:
        return "Bandcamp"
    return "Web Audio"


async def resolve_spotify_to_query(spotify_url: str) -> str:
    """Extrae título y artista de una página pública de Spotify para buscar su audio oficial."""
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    async with aiohttp.ClientSession(headers=headers) as session:
        async with session.get(spotify_url, timeout=10) as resp:
            page = await resp.text()

    title_match = re.search(r'<meta property="og:title" content="([^"]+)"', page)
    desc_match = re.search(r'<meta property="og:description" content="([^"]+)"', page)

    if not title_match:
        raise ValueError("No se pudo leer la pista de Spotify. Verifica que el enlace sea público.")

    track_title = html.unescape(title_match.group(1))
    artist = ""
    if desc_match:
        # Formato habitual de Spotify: "Artista · Canción · Año" o "Listen to X on Spotify. Artist · Song"
        desc = html.unescape(desc_match.group(1))
        parts = [p.strip() for p in desc.split("·")]
        if parts:
            artist = parts[0].replace("Listen to ", "").strip()

    query = f"{artist} - {track_title} official audio".strip()
    return f"ytsearch1:{query}"


def _extract_info_sync(target: str, platform_override: str = None) -> dict:
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        "extract_flat": False,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(target, download=False)
        if "entries" in info and info["entries"]:
            info = info["entries"][0]

        extractor = info.get("extractor_key", "Generic")
        vid_id = info.get("id") or str(uuid.uuid4())[:12]
        platform = platform_override or detect_platform(info.get("webpage_url", target))

        return {
            "uid": f"{extractor}_{vid_id}",
            "platform": platform,
            "title": info.get("track") or info.get("title") or "Pista de Audio",
            "artist": info.get("artist") or info.get("uploader") or info.get("creator") or "Artista Desconocido",
            "duration": int(info.get("duration") or 0),
            "webpage_url": info.get("webpage_url") or target,
            "thumbnail": info.get("thumbnail"),
        }


async def get_media_metadata(input_text: str) -> dict:
    """Acepta cualquier URL soportada, enlace de Spotify o consulta de búsqueda."""
    url_match = URL_REGEX.search(input_text)
    if url_match:
        url = url_match.group(0)
        if SPOTIFY_TRACK_REGEX.search(url):
            search_target = await resolve_spotify_to_query(url)
            return await asyncio.to_thread(_extract_info_sync, search_target, "Spotify")
        platform = detect_platform(url)
        return await asyncio.to_thread(_extract_info_sync, url, platform)
    else:
        # Búsqueda directa por texto si el usuario escribe el nombre de una canción
        return await asyncio.to_thread(_extract_info_sync, f"ytsearch1:{input_text}", "YouTube Search")


def _search_inline_sync(query: str, limit: int = 6) -> list[dict]:
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
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


def _download_sync(url: str, codec: str, bitrate: str, unique_id: str) -> str:
    output_template = os.path.join(DOWNLOAD_DIR, f"{unique_id}.%(ext)s")
    postprocessors = [
        {
            "key": "FFmpegExtractAudio",
            "preferredcodec": codec,
            "preferredquality": bitrate,
        },
        {"key": "EmbedThumbnail"},
        {"key": "FFmpegMetadata", "add_metadata": True},
    ]

    ydl_opts = {
        "format": "bestaudio/best",
        "outtmpl": output_template,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "writethumbnail": True,
        "postprocessors": postprocessors,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])
    return os.path.join(DOWNLOAD_DIR, f"{unique_id}.{codec}")


async def download_media_audio(url: str, codec: str = "mp3", bitrate: str = "128") -> str:
    unique_id = str(uuid.uuid4())
    async with download_semaphore:
        return await asyncio.to_thread(_download_sync, url, codec, bitrate, unique_id)


def cleanup_files(file_path: str):
    base = os.path.splitext(file_path)[0]
    for ext in [".mp3", ".m4a", ".opus", ".webp", ".jpg", ".png", ".part", ".ytdl"]:
        target = base + ext
        if os.path.exists(target):
            try:
                os.remove(target)
            except OSError:
                pass


async def janitor_cleanup_loop():
    """Tarea en segundo plano que elimina archivos residuales con más de 15 minutos."""
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
