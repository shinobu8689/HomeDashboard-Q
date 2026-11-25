"""Scan a 作品 folder (one sub-folder per 単行本) for the dashboard's Manga shelf panel.

scan_manga_shelf(path) -> {"title": str, "volumes": [{"number": int, "name": str, "cover": str, "label": str, "spine_number": int|None}], "warnings": [str]}
`label` is the title printed down the spine; `spine_number` is the volume number printed at its foot (None = none).
`cover` is an absolute file path; the dashboard loads it through the existing /local-media endpoint.
"""
import re
import unicodedata
from pathlib import Path

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif"}  # the types /local-media can serve
KANJI_DIGITS = {"〇": 0, "零": 0, "一": 1, "二": 2, "三": 3, "四": 4,
                "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
COUNTER = "巻卷冊册集"
MISC_FOLDER = "misc"  # a series folder with this name lists every sub-folder as its own book


def _kanji_to_int(text):
    total, current = 0, 0
    for ch in text:
        if ch in KANJI_DIGITS:
            current = current * 10 + KANJI_DIGITS[ch] if ch in "〇零" else KANJI_DIGITS[ch]
        elif ch == "十":
            total += (current or 1) * 10
            current = 0
        elif ch == "百":
            total += (current or 1) * 100
            current = 0
    return total + current


def parse_volume_number(name):
    """Return the volume number found in a folder name (English or Japanese), or None."""
    s = unicodedata.normalize("NFKC", name)
    strong = [
        rf"第\s*(\d+)\s*[{COUNTER}]",
        rf"(\d+)\s*[{COUNTER}]",
        r"(?<![a-z])vol(?:ume)?s?\.?\s*[_\-]?\s*(\d+)",
        r"(?<![a-z])tome\s*(\d+)",
        r"(?<![a-z])v\.?\s*(\d+)(?!\d)",
        r"(?<![a-z])(?:no|n°)\.?\s*(\d+)",
        r"#\s*(\d+)",
    ]
    lowered = s.lower()
    for pattern in strong:
        m = re.search(pattern, lowered)
        if m:
            return int(m.group(1))
    m = re.search(rf"第\s*([〇零一二三四五六七八九十百]+)\s*[{COUNTER}]", s)
    if m:
        return _kanji_to_int(m.group(1))
    m = re.search(rf"([〇零一二三四五六七八九十百]+)\s*[{COUNTER}]", s)
    if m:
        return _kanji_to_int(m.group(1))
    # Fallback: last bare number that is not a year or a resolution/size tag.
    candidates = []
    for m in re.finditer(r"\d+", lowered):
        value = m.group(0)
        after = lowered[m.end():m.end() + 1]
        if len(value) >= 4 or after in ("p", "x") or (len(value) == 4 and value[:2] in ("19", "20")):
            continue
        candidates.append(int(value))
    return candidates[-1] if candidates else None


_TRAILING_ARABIC = re.compile(
    r"^(?P<title>.*?)[\s_\-]*[(\[【]?\s*(?:第|vol\.?|volume|v\.?|tome|no\.?|#)?\s*(?<!\d)(?P<num>\d{1,3})\s*[%s]?\s*[)\]】]?\s*$"
    % COUNTER, re.IGNORECASE)
_TRAILING_KANJI = re.compile(
    r"^(?P<title>.*?)[\s_\-]*[(\[【]?\s*第?\s*(?P<num>[〇零一二三四五六七八九十百]+)\s*[%s]\s*[)\]】]?\s*$" % COUNTER)


def split_title_number(name):
    """'それでも歩は寄せてくる（2）' -> ('それでも歩は寄せてくる', 2); a name without a trailing number -> (name, None)."""
    s = unicodedata.normalize("NFKC", name).strip()
    m = _TRAILING_KANJI.match(s)
    number = _kanji_to_int(m.group("num")) if m else None
    if not m:
        m = _TRAILING_ARABIC.match(s)
        number = int(m.group("num")) if m else None
    if not m:
        return s, None
    title = m.group("title").strip(" _-・([【")
    return (title, number) if title else (s, None)


_NUMBER_RUN = re.compile(r"(?<!\d)\d{3,}(?!\d)")  # a run of 3+ digits that isn't part of a longer number


def _cover_rank(stem):
    s = unicodedata.normalize("NFKC", stem).lower()
    rank = None
    for run in _NUMBER_RUN.findall(s):
        if set(run) == {"0"}:
            candidate = 0       # 000
        elif re.fullmatch(r"0+1", run):
            candidate = 1       # 001
        else:
            continue
        rank = candidate if rank is None else min(rank, candidate)
    if rank is not None:
        return rank
    if "cover" in s or "表紙" in s or "カバー" in s:
        return 2
    return None


def find_cover(volume_dir):
    """000 first, then 001, then a 'cover' keyword. Looks in the folder, then one level down."""
    best = None
    for depth_dirs in ([volume_dir], sorted(p for p in volume_dir.iterdir() if p.is_dir())):
        for directory in depth_dirs if isinstance(depth_dirs, list) else [depth_dirs]:
            for file in sorted(directory.iterdir()):
                if file.suffix.lower() not in IMAGE_EXTS or not file.is_file():
                    continue
                rank = _cover_rank(file.stem)
                if rank is not None and (best is None or rank < best[0]):
                    best = (rank, file)
        if best:
            break
    return best[1] if best else None


def first_image(folder):
    """Fallback for 'misc': any image in the folder (or one level down), since its files are rarely named 000/001/cover."""
    for directory in [folder] + sorted(p for p in folder.iterdir() if p.is_dir()):
        for file in sorted(directory.iterdir()):
            if file.is_file() and file.suffix.lower() in IMAGE_EXTS:
                return file
    return None


def scan_manga_shelf(path):
    root = Path(path).expanduser()
    if not root.is_dir():
        raise FileNotFoundError(f"Not a folder: {path}")
    volumes, warnings, seen = [], [], {}
    if root.name.strip().lower() == MISC_FOLDER:
        # "misc" holds unrelated works, one sub-folder each, in name order. The spine shows the title
        # (the folder name without its volume number) and, only if the name had one, the volume number.
        for entry in sorted(p for p in root.iterdir() if p.is_dir()):
            cover = find_cover(entry) or first_image(entry)
            if cover is None:
                warnings.append(f"No image found in '{entry.name}'")
                continue
            title, spine_number = split_title_number(entry.name)
            volumes.append({"number": len(volumes) + 1, "name": entry.name, "cover": str(cover.resolve()),
                            "label": title, "spine_number": spine_number})
        return {"title": root.name, "volumes": volumes, "warnings": warnings}
    for entry in sorted(p for p in root.iterdir() if p.is_dir()):
        number = parse_volume_number(entry.name)
        if number is None:
            warnings.append(f"No volume number in '{entry.name}'")
            continue
        if number in seen:
            warnings.append(f"Volume {number} duplicated: '{entry.name}' ignored (kept '{seen[number]}')")
            continue
        cover = find_cover(entry)
        if cover is None:
            warnings.append(f"No 000/001/cover image in '{entry.name}'")
            continue
        seen[number] = entry.name
        volumes.append({"number": number, "name": entry.name, "cover": str(cover.resolve()),
                        "label": root.name, "spine_number": number})
    volumes.sort(key=lambda v: v["number"])
    return {"title": root.name, "volumes": volumes, "warnings": warnings}
