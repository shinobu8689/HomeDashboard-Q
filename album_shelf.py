"""Read album.txt (one YouTube Music album URL per line) for the dashboard's Album shelf panel.

scan_album_shelf(path) -> {
    "title": str,
    "albums": [{"id": str, "name": str, "artist": str, "year": str, "cover": str, "url": str,
                "tracks": [{"number": int, "name": str, "url": str, "duration": str}]}],
    "warnings": [str],
}
`cover` is an https image URL (the dashboard loads it straight from Google's image servers).
`url` of a track opens that song on YouTube Music; nothing is downloaded or stored locally.

lookup_song_cover(title, artist, duration) -> {"found": bool, "cover": str, "album": str, "title": str, "artist": str}
    is for the Now Playing card: it finds the song that is playing on YouTube Music and returns its album cover.

Needs:  pip install ytmusicapi   (no login required for public albums)

A relative `path` is resolved against the folder this file lives in. Blank lines and lines starting
with '#' are ignored. Titles come back in TITLE_LANGUAGE (Japanese by default). Album details are cached
in album_cache.json next to this file, so only new lines are looked up online; delete that file (or lower
CACHE_TTL) to force a refresh.
"""
import json
import re
import time
import unicodedata
from pathlib import Path
from urllib.parse import parse_qs, urlparse

BASE_DIR = Path(__file__).resolve().parent
# Language YouTube Music is asked to answer in. "ja" gives the Japanese (original) album and song titles;
# use "en" for English / romanised ones. Cached albums are kept per language, so changing this just refetches.
TITLE_LANGUAGE = "ja"
CACHE_PATH = BASE_DIR / "album_cache.json"
CACHE_TTL = 30 * 24 * 3600  # seconds a cached album is trusted
TRACK_URL = "https://music.youtube.com/watch?v={video}"
URL_RE = re.compile(r"https?://\S+")
ALBUM_PLAYLIST_PREFIX = "OLAK5uy"  # YouTube Music's id for an album's audio playlist
BROWSE_ID_RE = re.compile(r"^MPREb_[\w-]+$")


def parse_album_url(url):
    """Return ('browse', MPREb_...) or ('playlist', list_id) for a YouTube / YouTube Music link, else None."""
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if not (host == "youtu.be" or host.endswith("youtube.com")):
        return None
    match = re.search(r"/browse/([\w-]+)", parsed.path)
    if match and BROWSE_ID_RE.match(match.group(1)):
        return "browse", match.group(1)
    list_id = (parse_qs(parsed.query).get("list") or [""])[0]
    if list_id:
        return "playlist", list_id
    return None


def read_album_lines(path):
    """The URLs in album.txt, in order, without blanks, comments or duplicates."""
    file = Path(path).expanduser()
    if not file.is_absolute():
        file = BASE_DIR / file
    if not file.is_file():
        raise FileNotFoundError(f"Not a file: {file}")
    urls, seen = [], set()
    for raw in file.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        found = URL_RE.search(line)
        if found and found.group(0) not in seen:
            seen.add(found.group(0))
            urls.append(found.group(0))
    return urls


def _best_cover(thumbnails):
    """Largest thumbnail, asked for at 600 px square when it is one of Google's resizable image URLs."""
    if not thumbnails:
        return ""
    best = max(thumbnails, key=lambda t: (t.get("width") or 0) * (t.get("height") or 0))
    url = best.get("url") or ""
    return re.sub(r"=w\d+-h\d+.*$", "=w600-h600-l90-rj", url)


def _artist_names(artists):
    return ", ".join(a.get("name", "") for a in (artists or []) if a.get("name"))


def _build_album(source_url, data, playlist_id, warnings):
    tracks = []
    for entry in data.get("tracks") or []:
        video = entry.get("videoId")
        if not video:
            warnings.append(f"'{data.get('title', source_url)}': skipped an unavailable track '{entry.get('title', '?')}'")
            continue
        url = TRACK_URL.format(video=video)
        if playlist_id:
            url += f"&list={playlist_id}"
        tracks.append({
            "number": len(tracks) + 1,
            "name": entry.get("title") or "",
            "url": url,
            "duration": entry.get("duration") or "",
        })
    artists = data.get("artists") or ([data["author"]] if data.get("author") else [])
    return {
        "id": playlist_id or source_url,
        "name": data.get("title") or source_url,
        "artist": _artist_names(artists),
        "year": str(data.get("year") or ""),
        "cover": _best_cover(data.get("thumbnails")),
        "url": f"https://music.youtube.com/playlist?list={playlist_id}" if playlist_id else source_url,
        "tracks": tracks,
    }


def fetch_album(client, url, warnings):
    """Look one link up on YouTube Music. Raises ValueError for links that are not albums/playlists."""
    kind = parse_album_url(url)
    if kind is None:
        raise ValueError("not a YouTube Music album or playlist link")
    kind, identifier = kind
    if kind == "browse":
        data = client.get_album(identifier)
        return _build_album(url, data, data.get("audioPlaylistId"), warnings)
    if identifier.startswith(ALBUM_PLAYLIST_PREFIX):
        browse_id = client.get_album_browse_id(identifier)
        if browse_id:
            return _build_album(url, client.get_album(browse_id), identifier, warnings)
    # A regular playlist (or an album id that could not be converted): list it like an album.
    data = client.get_playlist(identifier, limit=None)
    return _build_album(url, data, identifier, warnings)


def _load_cache():
    try:
        return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_cache(cache):
    try:
        CACHE_PATH.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError:
        pass  # a read-only folder just means no caching


def scan_album_shelf(path, client=None, language=None):
    """`client` is only for tests; normally a ytmusicapi.YTMusic() is created on the first lookup."""
    language = language or TITLE_LANGUAGE
    urls = read_album_lines(path)
    cache = _load_cache()
    albums, warnings, now = [], [], time.time()
    changed = False
    for url in urls:
        key = f"{language}|{url}"
        cached = cache.get(key)
        if cached and now - cached.get("fetched", 0) < CACHE_TTL:
            albums.append(cached["album"])
            continue
        if parse_album_url(url) is None:
            warnings.append(f"Ignored {url}: not a YouTube Music album or playlist link")
            continue
        try:
            if client is None:
                from ytmusicapi import YTMusic  # imported late so a missing package is a clear message
                client = YTMusic(language=language)
            album = fetch_album(client, url, warnings)
        except ImportError:
            raise RuntimeError("ytmusicapi is not installed: run  pip install ytmusicapi")
        except Exception as error:  # one bad line should not hide the other albums
            if cached:
                albums.append(cached["album"])
                warnings.append(f"Could not refresh {url} ({error}); showing the saved copy")
            else:
                warnings.append(f"Could not load {url}: {error}")
            continue
        cache[key] = {"fetched": now, "album": album}
        albums.append(album)
        changed = True
    if changed:
        _save_cache(cache)
    return {"title": Path(path).stem, "albums": albums, "warnings": warnings}


# ----- Now Playing cover -----
MIN_COVER_SCORE = 5            # a match needs the title plus the artist or the length to agree
NEGATIVE_TTL = 24 * 3600       # a song that was not found is looked up again after a day


def _norm(text):
    """Case, width and punctuation insensitive form, so 'Lullaby!' matches 'lullaby' and ＡＢＣ matches abc."""
    return re.sub(r"[\W_]+", "", unicodedata.normalize("NFKC", text or "").casefold())


def _score_song(result, title, artist, duration):
    wanted_title, found_title = _norm(title), _norm(result.get("title"))
    if not found_title or not wanted_title:
        return 0
    if found_title == wanted_title:
        score = 4
    elif found_title in wanted_title or wanted_title in found_title:
        score = 2
    else:
        return 0  # the title has to at least overlap
    wanted_artist = _norm(artist)
    names = [_norm(a.get("name")) for a in (result.get("artists") or [])]
    if wanted_artist and any(n and (n in wanted_artist or wanted_artist in n) for n in names):
        score += 3
    seconds = result.get("duration_seconds")
    if duration and seconds:
        gap = abs(seconds - duration)
        score += 2 if gap <= 3 else 1 if gap <= 8 else 0
    return score


def lookup_song_cover(title, artist="", duration=None, client=None, language=None):
    """Find the playing song on YouTube Music and return its album cover (600 px square where possible).

    `duration` is the song's length in seconds, if known; it helps pick the right version.
    `client` is only for tests. Results are cached, a miss for a day.
    """
    language = language or TITLE_LANGUAGE
    title = (title or "").strip()
    if not title:
        return {"found": False}
    key = f"song2|{language}|{_norm(title)}|{_norm(artist)}"   # song2: matching rules changed, old misses are not reused
    cache = _load_cache()
    now = time.time()
    cached = cache.get(key)
    if cached:
        ttl = CACHE_TTL if cached["result"].get("found") else NEGATIVE_TTL
        if now - cached.get("fetched", 0) < ttl:
            return cached["result"]
    try:
        if client is None:
            from ytmusicapi import YTMusic
            client = YTMusic(language=language)
    except ImportError:
        raise RuntimeError("ytmusicapi is not installed: run  pip install ytmusicapi")
    try:
        results = client.search(f"{title} {artist}".strip(), filter="songs", limit=10)
    except Exception as error:  # not cached, so the next poll can try again
        return {"found": False, "error": str(error)}
    results = results or []
    scored = [(_score_song(result, title, artist, duration), result) for result in results]
    best, best_score = None, 0
    for score, result in scored:
        if score > best_score:
            best, best_score = result, score
    if best_score < MIN_COVER_SCORE:
        # The query already holds the artist, so YouTube Music's own top hit is trusted when its title is exactly the
        # one playing, even if the artist is credited differently (soundtracks, composers, "Various Artists").
        best = results[0] if results and _norm(results[0].get("title")) == _norm(title) else None
    found = {"found": False}
    cover = _best_cover(best.get("thumbnails")) if best is not None else ""
    if cover:
        found = {
            "found": True,
            "cover": cover,
            "album": (best.get("album") or {}).get("name", ""),
            "title": best.get("title") or "",
            "artist": _artist_names(best.get("artists")),
        }
    else:
        # Shown in the dashboard's browser console, to see what was searched and what came back.
        found["candidates"] = [{"title": r.get("title"), "artists": _artist_names(r.get("artists")), "score": sc}
                               for sc, r in scored[:3]]
    cache[key] = {"fetched": now, "result": found}
    _save_cache(cache)
    return found
