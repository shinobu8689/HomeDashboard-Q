"""Short-lived download links for manga volumes, for the dashboard's Manga shelf panel.

The dashboard asks for a link (loopback only); the server remembers a random token for that volume
folder and hands back a URL on the laptop's LAN address. A phone that opens the URL receives the
volume as a .cbz (a zip of the page images). Tokens expire, and the URL never contains a file path.

create_share(path, number) -> (token, download_name)
lookup_share(token)        -> (folder, download_name) | None
build_archive(folder)      -> Path of a temporary archive (the caller deletes it)
lan_host()                 -> the laptop's LAN IPv4 address
"""
import os
import re
import secrets
import socket
import tempfile
import threading
import time
import zipfile
from pathlib import Path

from manga_shelf import scan_manga_shelf

TOKEN_TTL_SECONDS = 600         # how long a download link keeps working after it was created
DOWNLOAD_EXTENSION = ".cbz"     # a .cbz is a normal zip; change to ".zip" if your phone's reader prefers it
JUNK_FILES = {"thumbs.db", ".ds_store", "desktop.ini"}

_lock = threading.Lock()
_links: dict[str, tuple[float, Path, str]] = {}   # token -> (expires at, volume folder, download name)


def _purge_expired(now):
    for token in [token for token, (expires, _, _) in _links.items() if expires <= now]:
        del _links[token]


def _safe_filename(text):
    return re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", text).strip(" .") or "volume"


def create_share(path, number):
    """Create a link for volume `number` of the series folder `path`.

    Raises FileNotFoundError if the series folder is missing and LookupError if it has no such volume.
    """
    shelf = scan_manga_shelf(path)
    volume = next((item for item in shelf["volumes"] if item["number"] == number), None)
    if volume is None:
        raise LookupError(f"Volume {number} not found")

    folder = (Path(path).expanduser() / volume["name"]).resolve(strict=True)
    if not folder.is_dir():
        raise FileNotFoundError(f"Not a folder: {folder}")

    # Folders are usually already named "<series> <volume>", so only prepend the series when they are not.
    label = volume["name"] if volume["name"].startswith(shelf["title"]) else f"{shelf['title']} {volume['name']}"
    download_name = _safe_filename(label) + DOWNLOAD_EXTENSION
    token = secrets.token_urlsafe(9)   # 12 characters keeps the QR code small
    now = time.time()
    with _lock:
        _purge_expired(now)
        _links[token] = (now + TOKEN_TTL_SECONDS, folder, download_name)
    return token, download_name


def lookup_share(token):
    now = time.time()
    with _lock:
        _purge_expired(now)
        entry = _links.get(token)
    return (entry[1], entry[2]) if entry else None


def build_archive(folder):
    """Pack every page under `folder` into a temporary archive. Images are already compressed, so
    they are stored as they are, which is quick and gives the phone a real file size to show."""
    descriptor, name = tempfile.mkstemp(suffix=DOWNLOAD_EXTENSION, prefix="manga-")
    os.close(descriptor)
    archive = Path(name)
    try:
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_STORED) as bundle:
            for file in sorted(folder.rglob("*")):
                if file.is_file() and file.name.lower() not in JUNK_FILES:
                    bundle.write(file, file.relative_to(folder).as_posix())
    except BaseException:
        archive.unlink(missing_ok=True)
        raise
    return archive


def lan_host():
    """The address a phone on the same network can reach. Set DASHBOARD_LAN_HOST to override it
    (for example when the laptop has a VPN or several network adapters)."""
    override = os.environ.get("DASHBOARD_LAN_HOST", "").strip()
    if override:
        return override
    try:
        # A UDP "connect" sends nothing; it only makes the OS pick the outgoing interface.
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("192.0.2.1", 9))
            return probe.getsockname()[0]
    except OSError:
        try:
            return socket.gethostbyname(socket.gethostname())
        except OSError:
            return "127.0.0.1"
