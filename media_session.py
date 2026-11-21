"""
Windows "now playing" access for the Ambient Dashboard.

Reads the same media session Windows shows in its media flyout (System Media
Transport Controls), which covers Edge, Chrome, Spotify, VLC and anything else
that registers with Windows. Must run on Windows, in the same user session as
the browser/player.

Install one of:
    pip install winsdk                              (Python 3.12 or older)
    pip install winrt-runtime winrt-Windows.Media.Control winrt-Windows.Storage.Streams winrt-Windows.Foundation winrt-Windows.Foundation.Collections
"""

from __future__ import annotations

from datetime import datetime, timezone

_IMPORT_ERRORS: list[str] = []
_Manager = None
_DataReader = None
_BACKEND = None

try:  # winsdk (older Pythons)
    from winsdk.windows.media.control import (
        GlobalSystemMediaTransportControlsSessionManager as _Manager,
    )
    from winsdk.windows.storage.streams import DataReader as _DataReader
    _BACKEND = "winsdk"
except ImportError as error:
    _IMPORT_ERRORS.append(f"winsdk: {error}")
    try:  # pywinrt split packages (newer Pythons)
        from winrt.windows.media.control import (
            GlobalSystemMediaTransportControlsSessionManager as _Manager,
        )
        from winrt.windows.storage.streams import DataReader as _DataReader
        _BACKEND = "winrt"
    except ImportError as error:
        _IMPORT_ERRORS.append(f"winrt: {error}")
        _Manager = None
        _DataReader = None


class MediaUnavailable(RuntimeError):
    """The Windows media API cannot be used in this environment."""


_PLAYBACK_STATUS = {
    0: "closed",
    1: "opened",
    2: "changing",
    3: "stopped",
    4: "playing",
    5: "paused",
}
_TICKS_PER_SECOND = 10_000_000


def _require_backend():
    if _Manager is None:
        raise MediaUnavailable(
            "Windows media API not available (" + "; ".join(_IMPORT_ERRORS) + "). "
            "Run the proxy on Windows and install 'winsdk' (or the winrt-* packages) "
            "into the same Python that runs uvicorn."
        )


def _friendly_app_name(app_id: str) -> str:
    raw = (app_id or "").strip()
    lowered = raw.lower()
    for needle, name in (
        ("msedge", "Microsoft Edge"),
        ("chrome", "Google Chrome"),
        ("firefox", "Firefox"),
        ("brave", "Brave"),
        ("opera", "Opera"),
        ("spotify", "Spotify"),
        ("vlc", "VLC"),
        ("zunemusic", "Media Player"),
        ("mediaplayer", "Media Player"),
        ("applemusic", "Apple Music"),
    ):
        if needle in lowered:
            return name
    name = raw.split("!")[0]
    if name.lower().endswith(".exe"):
        name = name[:-4]
    return name or "Media"


def _seconds(value) -> float | None:
    try:
        return float(value.total_seconds())
    except AttributeError:
        return None


async def _get_manager():
    _require_backend()
    return await _Manager.request_async()


def _pick_session(manager):
    """Prefer a session that is actually playing, then Windows' own 'current' one."""
    sessions = list(manager.get_sessions())
    for session in sessions:
        if session.get_playback_info().playback_status == 4:  # PLAYING
            return session
    current = manager.get_current_session()
    if current is not None:
        return current
    return sessions[0] if sessions else None


def _timeline(session, status: str):
    """Return (position, duration) in seconds, extrapolating while playing."""
    try:
        timeline = session.get_timeline_properties()
        start = _seconds(timeline.start_time) or 0.0
        end = _seconds(timeline.end_time)
        if end is None or end - start <= 0:
            return None, None
        duration = end - start
        position = (_seconds(timeline.position) or 0.0) - start

        if status == "playing":
            updated = timeline.last_updated_time
            if isinstance(updated, datetime) and updated.year > 2000:
                if updated.tzinfo is None:
                    updated = updated.replace(tzinfo=timezone.utc)
                elapsed = (datetime.now(timezone.utc) - updated).total_seconds()
                rate = session.get_playback_info().playback_rate or 1.0
                position += max(0.0, elapsed) * rate
        return max(0.0, min(position, duration)), duration
    except Exception:  # timeline is optional; never fail the whole read
        return None, None


async def read_media_session() -> dict:
    """Snapshot of the current media session, JSON-ready."""
    manager = await _get_manager()
    session = _pick_session(manager)
    if session is None:
        return {"active": False}

    properties = await session.try_get_media_properties_async()
    info = session.get_playback_info()
    controls = info.controls
    status = _PLAYBACK_STATUS.get(int(info.playback_status), "stopped")
    position, duration = _timeline(session, status)

    title = properties.title or ""
    artist = properties.artist or properties.album_artist or ""
    album = properties.album_title or ""
    app = _friendly_app_name(session.source_app_user_model_id)
    has_thumbnail = properties.thumbnail is not None

    return {
        "active": True,
        "app": app,
        "title": title,
        "artist": artist,
        "album": album,
        "status": status,
        "position": position,
        "duration": duration,
        "hasThumbnail": has_thumbnail,
        "thumbnailKey": f"{app}|{title}|{artist}|{album}|{int(has_thumbnail)}",
        "canPlayPause": bool(controls.is_play_enabled or controls.is_pause_enabled),
        "canNext": bool(controls.is_next_enabled),
        "canPrevious": bool(controls.is_previous_enabled),
        "canSeek": bool(controls.is_playback_position_enabled) and duration is not None,
    }


async def read_media_thumbnail() -> tuple[bytes, str] | None:
    """Cover art of the current session as (bytes, content_type), or None."""
    manager = await _get_manager()
    session = _pick_session(manager)
    if session is None:
        return None
    properties = await session.try_get_media_properties_async()
    reference = properties.thumbnail
    if reference is None:
        return None

    stream = await reference.open_read_async()
    size = int(stream.size)
    if size <= 0:
        return None
    reader = _DataReader(stream)
    await reader.load_async(size)
    data = bytearray(size)
    reader.read_bytes(data)
    reader.close()
    stream.close()

    return _enhance_thumbnail(bytes(data))


_THUMBNAIL_TARGET = 640  # longest side, in pixels, that small cover art is upscaled to


def _sniff_media_type(payload: bytes) -> str:
    if payload.startswith(b"\x89PNG"):
        return "image/png"
    if payload.startswith(b"BM"):
        return "image/bmp"
    if payload[:4] == b"RIFF" and payload[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


def _enhance_thumbnail(payload: bytes) -> tuple[bytes, str]:
    """Upscale small cover art with Lanczos resampling so it stays smooth when the card enlarges it."""
    try:
        from io import BytesIO

        from PIL import Image, ImageFilter

        image = Image.open(BytesIO(payload))
        image.load()
        width, height = image.size
        longest = max(width, height)
        if longest >= _THUMBNAIL_TARGET:
            return payload, _sniff_media_type(payload)

        scale = _THUMBNAIL_TARGET / longest
        resized = image.convert("RGBA" if image.mode in ("RGBA", "LA", "P") else "RGB").resize(
            (round(width * scale), round(height * scale)), Image.LANCZOS
        )
        resized = resized.filter(ImageFilter.UnsharpMask(radius=1.2, percent=60, threshold=2))

        output = BytesIO()
        if resized.mode == "RGBA":
            resized.save(output, "PNG")
            return output.getvalue(), "image/png"
        resized.save(output, "JPEG", quality=93, subsampling=0)
        return output.getvalue(), "image/jpeg"
    except Exception:  # Pillow missing or an odd image: fall back to the original bytes
        return payload, _sniff_media_type(payload)


async def control_media_session(action: str, position: float | None = None) -> bool:
    """Run a transport command. Returns whether the player accepted it."""
    manager = await _get_manager()
    session = _pick_session(manager)
    if session is None:
        raise LookupError("No active media session")

    if action == "play_pause":
        return bool(await session.try_toggle_play_pause_async())
    if action == "next":
        return bool(await session.try_skip_next_async())
    if action == "previous":
        return bool(await session.try_skip_previous_async())
    if action == "seek":
        if position is None:
            raise ValueError("seek needs a position")
        start = _seconds(session.get_timeline_properties().start_time) or 0.0
        ticks = int((start + max(0.0, position)) * _TICKS_PER_SECOND)
        return bool(await session.try_change_playback_position_async(ticks))
    raise ValueError(f"Unknown media action: {action}")
