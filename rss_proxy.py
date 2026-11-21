"""
Tiny RSS/Atom proxy for the Ambient Dashboard.

Fetches a feed server-side (no browser CORS restrictions apply here)
and re-serves it with permissive CORS headers so dashboard.html can
fetch it directly, regardless of which browser is loading the page.
Also serves dashboard.html at the server root.
Generates QR-code PNGs for RSS entry links.

Run:
    pip install fastapi uvicorn httpx "qrcode[pil]" psutil --break-system-packages
    uvicorn rss_proxy:app --host 0.0.0.0 --port 8787

Open the dashboard at:
    http://localhost:8787/

In the dashboard's RSS URL setting, enter the feed's URL directly:
    https://feeds.bbci.co.uk/news/world/rss.xml
"""

import html
import ipaddress
import mimetypes
import queue
import random
import re
import time
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse

import httpx
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response, StreamingResponse
from starlette.background import BackgroundTask
from pydantic import BaseModel

#from qr import create_qr_png
from system_usage import read_system_usage
from manga_shelf import scan_manga_shelf
from manga_share import TOKEN_TTL_SECONDS, build_archive, create_share, lan_host, lookup_share

app = FastAPI(title="RSS Proxy")
DASHBOARD_FILE = Path(__file__).with_name("dashboard.html")
MEDIA_EXTENSIONS = {
    ".avif", ".gif", ".jpeg", ".jpg", ".mp4", ".mov", ".ogg",
    ".png", ".webm", ".webp",
}

# Allow the dashboard (any origin/browser) to call this proxy.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

# Simple in-memory cache so a browser tab polling this every few
# minutes doesn't hammer the upstream feed on every refresh.
_cache: dict[str, tuple[float, bytes, str]] = {}
CACHE_SECONDS = 300

HEADERS = {
    # Some feed hosts reject requests with no/odd User-Agent.
    "User-Agent": "Mozilla/5.0 (compatible; AmbientDashboardRSSProxy/1.0)"
}


class _RSSHTMLText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str):
        self.parts.append(data)


def _plain_rss_text(markup: str) -> str:
    parser = _RSSHTMLText()
    parser.feed(html.unescape(markup))
    return re.sub(r"\s+", " ", " ".join(parser.parts)).strip()


def _rss_entry_text(feed_content: bytes) -> list[str]:
    root = ET.fromstring(feed_content)
    entries = root.findall(".//item") or root.findall(".//{*}entry")
    readings = []

    for entry in entries:
        fields: dict[str, str] = {}
        for child in entry:
            field_name = child.tag.rsplit("}", 1)[-1].lower()
            if field_name in {"title", "description", "summary", "content", "encoded"} and field_name not in fields:
                fields[field_name] = "".join(child.itertext())

        title = _plain_rss_text(fields.get("title", ""))
        detail = next(
            (_plain_rss_text(fields[name]) for name in ("summary", "description", "encoded", "content") if fields.get(name)),
            "",
        )
        reading = "。".join(part for part in (title, detail[:600]) if part)
        if reading:
            readings.append(reading)

    return readings


def _fetch_rss_content(url: str) -> bytes:
    parsed_url = urlparse(url)
    if parsed_url.scheme not in {"http", "https"} or not parsed_url.hostname or parsed_url.username or parsed_url.password:
        raise HTTPException(status_code=400, detail="RSS feed URL must be a valid HTTP or HTTPS URL")

    now = time.time()
    cached = _cache.get(url)
    if cached and now - cached[0] < CACHE_SECONDS:
        return cached[1]

    try:
        with httpx.Client(timeout=10, follow_redirects=True) as client:
            response = client.get(url, headers=HEADERS)
            response.raise_for_status()
    except httpx.TimeoutException as error:
        raise HTTPException(status_code=504, detail="RSS feed timed out") from error
    except httpx.HTTPStatusError as error:
        raise HTTPException(status_code=error.response.status_code, detail="RSS feed returned an error") from error
    except httpx.RequestError as error:
        raise HTTPException(status_code=502, detail=f"Could not reach RSS feed: {error}") from error

    _cache[url] = (now, response.content, response.headers.get("content-type", "application/xml"))
    return response.content


@app.get("/", include_in_schema=False)
@app.get("/dashboard.html", include_in_schema=False)
async def dashboard():
    return FileResponse(DASHBOARD_FILE, media_type="text/html")


@app.get("/local-media", include_in_schema=False)
async def local_media(request: Request, path: str = Query(...)):
    if request.client is None:
        raise HTTPException(status_code=403, detail="Local media is only available from this machine")

    try:
        if not ipaddress.ip_address(request.client.host).is_loopback:
            raise HTTPException(status_code=403, detail="Local media is only available from this machine")
    except ValueError:
        raise HTTPException(status_code=403, detail="Local media is only available from this machine")

    media_path = Path(path).expanduser()
    if not media_path.is_absolute():
        media_path = Path(__file__).parent / media_path

    try:
        media_path = media_path.resolve(strict=True)
    except (OSError, RuntimeError):
        raise HTTPException(status_code=404, detail="Media file not found")

    if not media_path.is_file():
        raise HTTPException(status_code=404, detail="Media file not found")
    if media_path.suffix.lower() not in MEDIA_EXTENSIONS:
        raise HTTPException(status_code=415, detail="Unsupported media file type")

    media_type = mimetypes.guess_type(media_path.name)[0] or "application/octet-stream"
    return FileResponse(media_path, media_type=media_type)


@app.get("/manga-shelf", include_in_schema=False)
def manga_shelf(request: Request, path: str = Query(..., min_length=1, max_length=1024)):
    if request.client is None:
        raise HTTPException(status_code=403, detail="Manga shelf is only available from this machine")

    try:
        if not ipaddress.ip_address(request.client.host).is_loopback:
            raise HTTPException(status_code=403, detail="Manga shelf is only available from this machine")
    except ValueError:
        raise HTTPException(status_code=403, detail="Manga shelf is only available from this machine")

    try:
        return scan_manga_shelf(path)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Series folder not found")
    except OSError as error:
        raise HTTPException(status_code=500, detail=f"Could not read the series folder: {error}") from error


def _client_ip(request: Request):
    if request.client is None:
        return None
    try:
        address = ipaddress.ip_address(request.client.host)
    except ValueError:
        return None
    if address.version == 6 and address.ipv4_mapped:
        address = address.ipv4_mapped
    return address


@app.get("/manga-share", include_in_schema=False)
def manga_share(
    request: Request,
    path: str = Query(..., min_length=1, max_length=1024),
    volume: int = Query(..., ge=0),
):
    """Create a short-lived LAN download link for one volume. Only the dashboard on this machine may ask."""
    address = _client_ip(request)
    if address is None or not address.is_loopback:
        raise HTTPException(status_code=403, detail="Download links can only be created from this machine")

    origin = request.headers.get("origin")
    if origin and origin != "null":
        origin_host = urlparse(origin).hostname
        try:
            is_local_origin = origin_host == "localhost" or (
                origin_host is not None and ipaddress.ip_address(origin_host).is_loopback
            )
        except ValueError:
            is_local_origin = False
        if not is_local_origin:
            raise HTTPException(status_code=403, detail="Download links can only be created by local pages")

    try:
        token, _ = create_share(path, volume)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Series folder not found")
    except LookupError:
        raise HTTPException(status_code=404, detail="Volume not found")
    except OSError as error:
        raise HTTPException(status_code=500, detail=f"Could not read the series folder: {error}") from error

    port = request.url.port
    host = lan_host() + (f":{port}" if port else "")
    return {"url": f"http://{host}/book/{token}", "expiresIn": TOKEN_TTL_SECONDS}


@app.get("/book/{token}", include_in_schema=False)
def download_book(token: str, request: Request):
    """Send one volume as a .cbz. Open to devices on the local network, and only with a live token."""
    address = _client_ip(request)
    if address is None or not (address.is_loopback or address.is_private):
        raise HTTPException(status_code=403, detail="Downloads are only available on the local network")

    entry = lookup_share(token)
    if entry is None:
        raise HTTPException(status_code=404, detail="This download link has expired")
    folder, download_name = entry
    if not folder.is_dir():
        raise HTTPException(status_code=404, detail="Volume folder not found")

    try:
        archive = build_archive(folder)
    except OSError as error:
        raise HTTPException(status_code=500, detail=f"Could not pack the volume: {error}") from error

    return FileResponse(
        archive,
        media_type="application/octet-stream",
        filename=download_name,
        background=BackgroundTask(archive.unlink, missing_ok=True),
    )


@app.get("/rss")
async def get_rss(url: str = Query(..., description="Feed URL to fetch")):
    import time

    now = time.time()
    cached = _cache.get(url)
    if cached and now - cached[0] < CACHE_SECONDS:
        _, body, content_type = cached
        return Response(content=body, media_type=content_type)

    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
            resp = await client.get(url, headers=HEADERS)
            resp.raise_for_status()
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="Upstream feed timed out")
    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=e.response.status_code, detail="Upstream feed returned an error")
    except httpx.RequestError as e:
        raise HTTPException(status_code=502, detail=f"Could not reach feed: {e}")

    content_type = resp.headers.get("content-type", "application/xml")
    _cache[url] = (now, resp.content, content_type)

    return Response(content=resp.content, media_type=content_type)


@app.get("/qr", include_in_schema=False)
async def get_qr(url: str = Query(..., min_length=1, max_length=2048)):
    parsed_url = urlparse(url)
    if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
        raise HTTPException(status_code=400, detail="QR codes require an HTTP or HTTPS URL")
    return Response(content=create_qr_png(url), media_type="image/png")


class TimeAnnouncementRequest(BaseModel):
    time: str
    speaker: int | None = None
    readRss: bool = False
    rssUrl: str | None = None


@app.post("/announce-time")
def announce_time(body: TimeAnnouncementRequest, request: Request):
    if request.client is None:
        raise HTTPException(status_code=403, detail="Time announcements are only available from this machine")

    try:
        if not ipaddress.ip_address(request.client.host).is_loopback:
            raise HTTPException(status_code=403, detail="Time announcements are only available from this machine")
    except ValueError:
        raise HTTPException(status_code=403, detail="Time announcements are only available from this machine")

    origin = request.headers.get("origin")
    if origin and origin != "null":
        origin_host = urlparse(origin).hostname
        try:
            is_local_origin = origin_host == "localhost" or (
                origin_host is not None and ipaddress.ip_address(origin_host).is_loopback
            )
        except ValueError:
            is_local_origin = False
        if not is_local_origin:
            raise HTTPException(status_code=403, detail="Time announcements are only available to local pages")

    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", body.time):
        raise HTTPException(status_code=400, detail="Time must use the 24-hour HH:MM format")

    from requests.exceptions import RequestException
    from voicevox_gateway import ANNOUNCEMENT_SPEAKERS, SPEAKER, navy_time_reading, speak_sequence

    speaker = SPEAKER if body.speaker is None else body.speaker
    if speaker not in ANNOUNCEMENT_SPEAKERS:
        raise HTTPException(status_code=400, detail="Unknown VOICEVOX speaker/style ID")

    rss_url = ""
    if body.readRss:
        if not body.rssUrl or len(body.rssUrl) > 2048:
            raise HTTPException(status_code=400, detail="An RSS feed URL is required")
        rss_url = body.rssUrl

    try:
        def announcement_readings():
            yield navy_time_reading(body.time)
            if body.readRss:
                try:
                    feed_entries = _rss_entry_text(_fetch_rss_content(rss_url))
                except ET.ParseError as error:
                    raise HTTPException(status_code=502, detail="RSS feed contains invalid XML") from error
                if not feed_entries:
                    raise HTTPException(status_code=502, detail="RSS feed contains no readable entries")
                yield random.choice(feed_entries)

        speak_sequence(announcement_readings(), speaker=speaker)
    except RequestException as error:
        raise HTTPException(status_code=502, detail="Could not reach the VOICEVOX engine") from error
    except (OSError, RuntimeError, ValueError) as error:
        raise HTTPException(status_code=502, detail="Could not play the VOICEVOX announcement") from error

    return {"status": "announced", "time": body.time}


@app.get("/speech-status")
def speech_status(request: Request):
    if request.client is None:
        raise HTTPException(status_code=403, detail="Speech status is only available from this machine")

    try:
        if not ipaddress.ip_address(request.client.host).is_loopback:
            raise HTTPException(status_code=403, detail="Speech status is only available from this machine")
    except ValueError:
        raise HTTPException(status_code=403, detail="Speech status is only available from this machine")

    origin = request.headers.get("origin")
    if origin and origin != "null":
        origin_host = urlparse(origin).hostname
        try:
            is_local_origin = origin_host == "localhost" or (
                origin_host is not None and ipaddress.ip_address(origin_host).is_loopback
            )
        except ValueError:
            is_local_origin = False
        if not is_local_origin:
            raise HTTPException(status_code=403, detail="Speech status is only available to local pages")

    from voicevox_gateway import is_speaking

    return {"speaking": is_speaking()}


@app.get("/speech-events")
def speech_events(request: Request):
    if request.client is None:
        raise HTTPException(status_code=403, detail="Speech events are only available from this machine")

    try:
        if not ipaddress.ip_address(request.client.host).is_loopback:
            raise HTTPException(status_code=403, detail="Speech events are only available from this machine")
    except ValueError:
        raise HTTPException(status_code=403, detail="Speech events are only available from this machine")

    origin = request.headers.get("origin")
    if origin and origin != "null":
        origin_host = urlparse(origin).hostname
        try:
            is_local_origin = origin_host == "localhost" or (
                origin_host is not None and ipaddress.ip_address(origin_host).is_loopback
            )
        except ValueError:
            is_local_origin = False
        if not is_local_origin:
            raise HTTPException(status_code=403, detail="Speech events are only available to local pages")

    from voicevox_gateway import subscribe_speech_events, unsubscribe_speech_events

    listener = subscribe_speech_events()

    def event_stream():
        try:
            while True:
                try:
                    speaking = listener.get(timeout=15)
                except queue.Empty:
                    yield ": keep-alive\n\n"
                    continue
                yield f"data: {{\"speaking\": {str(speaking).lower()}}}\n\n"
        finally:
            unsubscribe_speech_events(listener)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _require_local_page(request: Request, what: str):
    """Loopback clients only, and only pages served from this machine (or file://)."""
    address = _client_ip(request)
    if address is None or not address.is_loopback:
        raise HTTPException(status_code=403, detail=f"{what} is only available from this machine")

    origin = request.headers.get("origin")
    if origin and origin != "null":
        origin_host = urlparse(origin).hostname
        try:
            is_local_origin = origin_host == "localhost" or (
                origin_host is not None and ipaddress.ip_address(origin_host).is_loopback
            )
        except ValueError:
            is_local_origin = False
        if not is_local_origin:
            raise HTTPException(status_code=403, detail=f"{what} is only available to local pages")


@app.get("/media-session", include_in_schema=False)
async def media_session_state(request: Request):
    """What Windows is playing right now (Edge, Spotify, ...). { active, title, artist, status, ... }"""
    _require_local_page(request, "Media session")
    import media_session

    try:
        return await media_session.read_media_session()
    except media_session.MediaUnavailable as error:
        raise HTTPException(status_code=501, detail=str(error)) from error
    except Exception as error:
        raise HTTPException(status_code=502, detail=f"Could not read the media session: {error}") from error


@app.get("/media-thumbnail", include_in_schema=False)
async def media_session_thumbnail(request: Request, key: str = Query("", max_length=512)):
    """Cover art of the current session. `key` only busts the browser cache when the track changes."""
    _require_local_page(request, "Media thumbnail")
    import media_session

    try:
        thumbnail = await media_session.read_media_thumbnail()
    except media_session.MediaUnavailable as error:
        raise HTTPException(status_code=501, detail=str(error)) from error
    except Exception as error:
        raise HTTPException(status_code=502, detail=f"Could not read the cover art: {error}") from error
    if thumbnail is None:
        raise HTTPException(status_code=404, detail="No cover art for this track")

    data, media_type = thumbnail
    return Response(content=data, media_type=media_type, headers={"Cache-Control": "private, max-age=300"})


class MediaControlRequest(BaseModel):
    action: str
    position: float | None = None


@app.post("/media-control", include_in_schema=False)
async def media_session_control(body: MediaControlRequest, request: Request):
    """Transport controls: play_pause, next, previous, seek (position in seconds)."""
    _require_local_page(request, "Media control")
    if body.action not in {"play_pause", "next", "previous", "seek"}:
        raise HTTPException(status_code=400, detail="Unknown media action")
    if body.action == "seek" and (body.position is None or body.position < 0):
        raise HTTPException(status_code=400, detail="seek needs a non-negative position in seconds")
    import media_session

    try:
        accepted = await media_session.control_media_session(body.action, body.position)
    except media_session.MediaUnavailable as error:
        raise HTTPException(status_code=501, detail=str(error)) from error
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except Exception as error:
        raise HTTPException(status_code=502, detail=f"Could not control the media session: {error}") from error
    return {"status": "ok" if accepted else "ignored", "action": body.action}


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/system-usage")
def system_usage(request: Request):
    if request.client is None:
        raise HTTPException(status_code=403, detail="System usage is only available from this machine")

    try:
        if not ipaddress.ip_address(request.client.host).is_loopback:
            raise HTTPException(status_code=403, detail="System usage is only available from this machine")
    except ValueError:
        raise HTTPException(status_code=403, detail="System usage is only available from this machine")

    origin = request.headers.get("origin")
    if origin and origin != "null":
        origin_host = urlparse(origin).hostname
        try:
            is_local_origin = origin_host == "localhost" or (
                origin_host is not None and ipaddress.ip_address(origin_host).is_loopback
            )
        except ValueError:
            is_local_origin = False
        if not is_local_origin:
            raise HTTPException(status_code=403, detail="System usage is only available to local pages")

    return read_system_usage()
