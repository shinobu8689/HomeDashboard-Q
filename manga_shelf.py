"""Scan a 作品 folder (one sub-folder per 単行本) for the dashboard's Manga shelf panel.

scan_manga_shelf(path) -> {"title": str, "volumes": [{"number": int, "name": str, "cover": str}], "warnings": [str]}
`cover` is an absolute file path; the dashboard loads it through the existing /local-media endpoint.
"""
import re
import unicodedata
from pathlib import Path

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif"}  # the types /local-media can serve
KANJI_DIGITS = {"〇": 0, "零": 0, "一": 1, "二": 2, "三": 3, "四": 4,
                "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
COUNTER = "巻卷冊册集"


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


def _cover_rank(stem):
    s = unicodedata.normalize("NFKC", stem).lower()
    digits = re.fullmatch(r"\D*?(\d{3,})", s)
    if digits and set(digits.group(1)) == {"0"}:
        return 0            # 000
    if digits and re.fullmatch(r"0+1", digits.group(1)):
        return 1            # 001
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


def scan_manga_shelf(path):
    root = Path(path).expanduser()
    if not root.is_dir():
        raise FileNotFoundError(f"Not a folder: {path}")
    volumes, warnings, seen = [], [], {}
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
        volumes.append({"number": number, "name": entry.name, "cover": str(cover.resolve())})
    volumes.sort(key=lambda v: v["number"])
    return {"title": root.name, "volumes": volumes, "warnings": warnings}
