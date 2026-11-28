"""
Tiny RSS/Atom proxy for the Ambient Dashboard.

Fetches a feed server-side (no browser CORS restrictions apply here)
and re-serves it with permissive CORS headers so dashboard.html can
fetch it directly, regardless of which browser is loading the page.
Also serves dashboard.html at the server root.
Generates QR-code PNGs for RSS entry links.

Run:
    pip install fastapi uvicorn httpx "qrcode[pil]" psutil playwright ytmusicapi --break-system-packages
    (Pillow comes with qrcode[pil]; it is also used for the picture cache)
    uvicorn rss_proxy:app --host 0.0.0.0 --port 8787

Open the dashboard at:
    http://localhost:8787/

In the dashboard's RSS URL setting, enter the feed's URL directly:
    https://feeds.bbci.co.uk/news/world/rss.xml
"""

import asyncio
import hashlib
import html
import io
import ipaddress
import mimetypes
import os
import queue
import random
import re
import socket
import threading
import time
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response, StreamingResponse
from pydantic import BaseModel
from starlette.background import BackgroundTask

from album_shelf import scan_album_shelf
#from qr import create_qr_png
from manga_share import TOKEN_TTL_SECONDS, build_archive, create_share, lan_host, lookup_share
from manga_shelf import scan_manga_shelf
from system_usage import read_system_usage

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
    # Some feed hosts reject requests with no/odd User-Agent, so identify as an
    # ordinary browser and send the headers a feed reader would normally send.
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
    ),
    "Accept": "application/rss+xml, application/rdf+xml, application/atom+xml, "
              "application/xml;q=0.9, text/xml;q=0.8, */*;q=0.5",
    "Accept-Language": "ja,en;q=0.8",
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


# Hosts that put bot detection in front of their feeds answer plain HTTP clients with
# one of these. When that happens the feed is fetched again through a real browser.
BROWSER_FALLBACK_STATUSES = {403, 429, 503}
_browser_lock = threading.Lock()  # one browser at a time is plenty for a dashboard


def _fetch_with_browser(url: str) -> bytes:
    """Fetch a feed through a real headless browser (Playwright).

    Needs:  pip install playwright --break-system-packages
    Uses the Edge that ships with Windows, so no `playwright install` is required;
    falls back to Playwright's own Chromium if Edge cannot be launched.
    """
    try:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import sync_playwright
    except ImportError as error:
        raise HTTPException(
            status_code=502,
            detail="This feed blocks plain requests and Playwright is not installed (pip install playwright)",
        ) from error

    with _browser_lock:
        try:
            with sync_playwright() as playwright:
                try:
                    browser = playwright.chromium.launch(channel="msedge", headless=True)
                except PlaywrightError:
                    browser = playwright.chromium.launch(headless=True)
                try:
                    context = browser.new_context(
                        user_agent=HEADERS["User-Agent"],
                        locale="ja-JP",
                        extra_http_headers={"Accept-Language": HEADERS["Accept-Language"]},
                    )
                    page = context.new_page()
                    # Open the site itself first (so any browser check can clear and the session
                    # gets its cookies), then read the feed from inside that page. Reading it in
                    # the page also avoids Chromium turning feed MIME types into file downloads.
                    parsed = urlparse(url)
                    home = page.goto(f"{parsed.scheme}://{parsed.netloc}/", wait_until="domcontentloaded", timeout=20000)
                    if home is None or not home.ok:
                        page.wait_for_timeout(6000)  # give a JavaScript check time to clear itself
                    values = page.evaluate(
                        """async (feedUrl) => {
                            const r = await fetch(feedUrl, { credentials: 'include' });
                            if (!r.ok) return null;
                            return Array.from(new Uint8Array(await r.arrayBuffer()));
                        }""",
                        url,
                    )
                    body = bytes(values) if values else b""
                finally:
                    browser.close()
        except PlaywrightError as error:
            raise HTTPException(status_code=502, detail=f"Browser fetch failed: {error}") from error

    if not body or b"<html" in body[:1000].lower():
        raise HTTPException(
            status_code=502,
            detail="The feed host blocked the browser as well (challenge page instead of a feed)",
        )
    return body


def _fetch_rss_content(url: str) -> bytes:
    parsed_url = urlparse(url)
    if parsed_url.scheme not in {"http", "https"} or not parsed_url.hostname or parsed_url.username or parsed_url.password:
        raise HTTPException(status_code=400, detail="RSS feed URL must be a valid HTTP or HTTPS URL")

    now = time.time()
    cached = _cache.get(url)
    if cached and now - cached[0] < CACHE_SECONDS:
        return cached[1]

    content_type = "application/xml"
    try:
        with httpx.Client(timeout=10, follow_redirects=True) as client:
            response = client.get(url, headers=HEADERS)
            response.raise_for_status()
        content = response.content
        content_type = response.headers.get("content-type", "application/xml")
    except httpx.TimeoutException as error:
        raise HTTPException(status_code=504, detail="RSS feed timed out") from error
    except httpx.HTTPStatusError as error:
        if error.response.status_code not in BROWSER_FALLBACK_STATUSES:
            raise HTTPException(status_code=error.response.status_code, detail="RSS feed returned an error") from error
        content = _fetch_with_browser(url)
    except httpx.RequestError as error:
        raise HTTPException(status_code=502, detail=f"Could not reach RSS feed: {error}") from error

    _cache[url] = (now, content, content_type)
    return content


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


@app.get("/album-shelf", include_in_schema=False)
def album_shelf(request: Request, path: str = Query("album.txt", min_length=1, max_length=1024)):
    if request.client is None:
        raise HTTPException(status_code=403, detail="Album shelf is only available from this machine")

    try:
        if not ipaddress.ip_address(request.client.host).is_loopback:
            raise HTTPException(status_code=403, detail="Album shelf is only available from this machine")
    except ValueError:
        raise HTTPException(status_code=403, detail="Album shelf is only available from this machine")

    try:
        return scan_album_shelf(path)
    except FileNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except RuntimeError as error:  # for example ytmusicapi not installed
        raise HTTPException(status_code=500, detail=str(error)) from error
    except OSError as error:
        raise HTTPException(status_code=500, detail=f"Could not read the album list: {error}") from error


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
    now = time.time()
    cached = _cache.get(url)
    if cached and now - cached[0] < CACHE_SECONDS:
        _, body, content_type = cached
        return Response(content=body, media_type=content_type)

    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
            resp = await client.get(url, headers=HEADERS)
            resp.raise_for_status()
        content = resp.content
        content_type = resp.headers.get("content-type", "application/xml")
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="Upstream feed timed out")
    except httpx.HTTPStatusError as e:
        if e.response.status_code not in BROWSER_FALLBACK_STATUSES:
            raise HTTPException(status_code=e.response.status_code, detail="Upstream feed returned an error")
        content = await asyncio.to_thread(_fetch_with_browser, url)
        content_type = "application/xml"
    except httpx.RequestError as e:
        raise HTTPException(status_code=502, detail=f"Could not reach feed: {e}")

    _cache[url] = (now, content, content_type)

    return Response(content=content, media_type=content_type)


# ---------------------------------------------------------------------------
# Article pictures (used by the dashboard's "RSS (Figsoku)" card)
# ---------------------------------------------------------------------------
# The figure pictures on a fig速 article page are named like fig_20260122_1769050841810.jpg.
# The same file with a size suffix (fig_20260122_1769050841810-200x150.jpg) is a thumbnail and is
# not matched. If the site ever changes its file naming, this is the one line to edit.
ARTICLE_IMAGE_NAME = re.compile(r"/fig_\d{8}_\d+\.(?:jpe?g|png|webp|gif)$", re.IGNORECASE)
_ARTICLE_URL_ATTRIBUTE = re.compile(
    r"""(?:src|href|data-src|data-lazy-src|data-original|data-full-url|srcset|data-srcset)\s*=\s*["']([^"']+)["']""",
    re.IGNORECASE,
)
_article_cache: dict[str, tuple[float, list[str]]] = {}
ARTICLE_CACHE_SECONDS = 600
MAX_ARTICLE_CHARS = 3_000_000


def _assert_public_http_url(url: str) -> None:
    """Only fetch ordinary public web pages; never the machine itself or the local network."""
    parsed_url = urlparse(url)
    if parsed_url.scheme not in {"http", "https"} or not parsed_url.hostname or parsed_url.username or parsed_url.password:
        raise HTTPException(status_code=400, detail="Article URL must be a valid HTTP or HTTPS URL")
    try:
        addresses = {info[4][0] for info in socket.getaddrinfo(parsed_url.hostname, None)}
    except socket.gaierror as error:
        raise HTTPException(status_code=502, detail="Could not resolve the article host") from error
    for address in addresses:
        ip = ipaddress.ip_address(address.split("%")[0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            raise HTTPException(status_code=400, detail="Article URL must point to a public host")


# The side column (<aside>) carries ads whose pictures use the same fig_… file names as the article
# photos, so it is cut out of the page before looking for pictures.
_INNERMOST_ASIDE = re.compile(r"<aside\b(?:(?!<aside\b).)*?</aside\s*>", re.IGNORECASE | re.DOTALL)
_SIDE_COLUMN_COMMENTS = re.compile(r"<!--\s*サイドカラムここから\s*-->.*?<!--\s*サイドカラムここまで\s*-->", re.DOTALL)


def _strip_side_column(page_html: str) -> str:
    page_html = _SIDE_COLUMN_COMMENTS.sub("", page_html)
    for _ in range(20):  # innermost first, so nested <aside> elements are handled too
        stripped = _INNERMOST_ASIDE.sub("", page_html)
        if stripped == page_html:
            break
        page_html = stripped
    return page_html


def _extract_article_images(page_html: str, base_url: str) -> list[str]:
    """Every figure picture in the article (not the side column), in page order, without duplicates or thumbnails."""
    page_html = _strip_side_column(page_html).replace("\\/", "/")
    seen: set[str] = set()
    images: list[str] = []
    for match in _ARTICLE_URL_ATTRIBUTE.finditer(page_html):
        for candidate in re.split(r"[\s,]+", match.group(1).strip()):
            if not candidate:
                continue
            absolute = urljoin(base_url, html.unescape(candidate))
            parsed = urlparse(absolute)
            if parsed.scheme not in {"http", "https"} or not ARTICLE_IMAGE_NAME.search(parsed.path):
                continue
            key = f"{parsed.netloc}{parsed.path}"
            if key in seen:
                continue
            seen.add(key)
            images.append(parsed._replace(fragment="").geturl())
    return images


@app.get("/article-images")
async def get_article_images(url: str = Query(..., min_length=1, max_length=2048, description="Article page URL")):
    now = time.time()
    cached = _article_cache.get(url)
    if cached and now - cached[0] < ARTICLE_CACHE_SECONDS:
        return {"images": cached[1]}

    await asyncio.to_thread(_assert_public_http_url, url)
    headers = {**HEADERS, "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.5"}
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
            resp = await client.get(url, headers=headers)
            resp.raise_for_status()
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="Article page timed out")
    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=e.response.status_code, detail="Article page returned an error")
    except httpx.RequestError as e:
        raise HTTPException(status_code=502, detail=f"Could not reach article page: {e}")

    images = _extract_article_images(resp.text[:MAX_ARTICLE_CHARS], str(resp.url))
    if len(_article_cache) >= 200:
        for stale_url in sorted(_article_cache, key=lambda key: _article_cache[key][0])[:50]:
            del _article_cache[stale_url]
    _article_cache[url] = (now, images)
    return {"images": images}


# ---------------------------------------------------------------------------
# Picture cache (used by the "RSS (Figsoku)" card)
# ---------------------------------------------------------------------------
# Each picture is downloaded once, shrunk and kept on disk next to this script (folder: image_cache),
# then served from there. That makes the carousel instant, spares the original site repeated
# downloads and avoids hotlink blocking. Oldest files are deleted once the folder passes the limit.
IMAGE_CACHE_DIR = Path(__file__).with_name("image_cache")
IMAGE_CACHE_MAX_BYTES = 500 * 1024 * 1024
IMAGE_MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024
IMAGE_MAX_SIDE = 1200  # longest side kept, in pixels; the cards show the pictures at about 200-500 px
IMAGE_CACHE_CONTROL = {"Cache-Control": "public, max-age=604800"}
_image_locks: dict[str, asyncio.Lock] = {}


def _image_cache_path(url: str) -> Path:
    return IMAGE_CACHE_DIR / f"{hashlib.sha256(url.encode('utf-8')).hexdigest()}.jpg"


def _prune_image_cache() -> None:
    try:
        files = sorted(IMAGE_CACHE_DIR.glob("*.jpg"), key=lambda file: file.stat().st_mtime)
        total = sum(file.stat().st_size for file in files)
        while files and total > IMAGE_CACHE_MAX_BYTES:
            oldest = files.pop(0)
            total -= oldest.stat().st_size
            oldest.unlink(missing_ok=True)
    except OSError:
        pass


def _store_cached_image(data: bytes, path: Path) -> None:
    from PIL import Image, ImageOps

    with Image.open(io.BytesIO(data)) as opened:
        image = ImageOps.exif_transpose(opened)
        image.thumbnail((IMAGE_MAX_SIDE, IMAGE_MAX_SIDE))
        if image.mode in {"RGBA", "LA", "P"}:
            rgba = image.convert("RGBA")
            image = Image.new("RGB", rgba.size, (255, 255, 255))
            image.paste(rgba, mask=rgba.getchannel("A"))
        else:
            image = image.convert("RGB")
        IMAGE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        image.save(temporary, "JPEG", quality=88, optimize=True)
    os.replace(temporary, path)
    _prune_image_cache()


@app.get("/cached-image")
async def get_cached_image(url: str = Query(..., min_length=1, max_length=2048, description="Picture URL")):
    path = _image_cache_path(url)
    if path.exists():
        os.utime(path, None)  # recently used files are the last to be pruned
        return FileResponse(path, media_type="image/jpeg", headers=IMAGE_CACHE_CONTROL)

    lock = _image_locks.setdefault(url, asyncio.Lock())
    async with lock:
        if not path.exists():
            await asyncio.to_thread(_assert_public_http_url, url)
            origin = urlparse(url)
            headers = {
                **HEADERS,
                "Accept": "image/avif,image/webp,image/*,*/*;q=0.5",
                "Referer": f"{origin.scheme}://{origin.netloc}/",
            }
            chunks: list[bytes] = []
            size = 0
            try:
                async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
                    async with client.stream("GET", url, headers=headers) as resp:
                        resp.raise_for_status()
                        if not resp.headers.get("content-type", "").lower().startswith("image/"):
                            raise HTTPException(status_code=415, detail="URL is not a picture")
                        async for chunk in resp.aiter_bytes():
                            size += len(chunk)
                            if size > IMAGE_MAX_DOWNLOAD_BYTES:
                                raise HTTPException(status_code=413, detail="Picture is too large")
                            chunks.append(chunk)
            except httpx.TimeoutException:
                raise HTTPException(status_code=504, detail="Picture download timed out")
            except httpx.HTTPStatusError as e:
                raise HTTPException(status_code=e.response.status_code, detail="Picture host returned an error")
            except httpx.RequestError as e:
                raise HTTPException(status_code=502, detail=f"Could not reach picture host: {e}")
            try:
                await asyncio.to_thread(_store_cached_image, b"".join(chunks), path)
            except Exception as error:
                raise HTTPException(status_code=422, detail=f"Could not read the picture: {error}") from error
    _image_locks.pop(url, None)
    return FileResponse(path, media_type="image/jpeg", headers=IMAGE_CACHE_CONTROL)


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