"""Folder-name parsing and tag-based metadata fallback."""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

# Artist <sep> Album (Year[optional extras]) [Source]
# <sep> is a space-flanked dash: hyphen-minus, en-dash, em-dash, or the U+FFFD
# replacement char (commonly left behind when an en-dash was decoded with the wrong
# codepage on Windows).
# e.g. "AC-DC - Back In Black (1980) [Tidal 24-48]"
#      "Alphaville - Forever Young (1984) [LP 24-192] [P-13065]"
#      "Alphaville - Afternoons in Utopia (1986, 2021) [Tidal 24-48]"
#      "Rockets \ufffd Imperception (1984) [LP 24-192]"
_LEAF_RE = re.compile(
    r"^(?P<artist>.+?)\s[-\u2013\u2014\ufffd]\s(?P<album>.+?)\s\((?P<year>\d{4}[^)]*)\)"
    r"(?:\s\[(?P<source>[^\]]+)\])?"
    r"(?:\s\[[^\]]+\])*"
    r"\s*$"
)

_YEAR_ONLY = re.compile(r"^(\d{4})")

# Split genre tag values on common separators; keep ordering but drop duplicates.
_GENRE_SPLIT = re.compile(r"[;,/|]+")


@dataclass
class ParsedName:
    artist: str
    album: str
    year: str
    source: str


def parse_leaf_folder_name(name: str) -> ParsedName | None:
    m = _LEAF_RE.match(name.strip())
    if not m:
        return None
    return ParsedName(
        artist=m.group("artist").strip(),
        album=m.group("album").strip(),
        year=m.group("year").strip(),
        source=(m.group("source") or "").strip(),
    )


def extract_year(raw: str) -> str:
    m = _YEAR_ONLY.match(raw.strip()) if raw else None
    return m.group(1) if m else (raw.strip() if raw else "")


def _uniq_preserve(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for x in items:
        if not x:
            continue
        key = x.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(x)
    return out


def _split_genre_value(val: str) -> list[str]:
    parts = [p.strip() for p in _GENRE_SPLIT.split(val) if p and p.strip()]
    return parts


def majority_genres(genre_tag_values: list[str], top_n: int = 3) -> list[str]:
    """Given raw GENRE tag values across tracks, return the most common genres (order by count)."""
    counter: Counter[str] = Counter()
    original_case: dict[str, str] = {}
    for raw in genre_tag_values:
        for g in _split_genre_value(raw):
            key = g.lower()
            counter[key] += 1
            original_case.setdefault(key, g)
    return [original_case[k] for k, _ in counter.most_common(top_n)]


def read_tags_from_files(audio_files: list[Path]) -> dict[str, object]:
    """Read tags from a sample of audio files using mutagen.

    Returns a dict with keys: artist, albumartist, album, year, genres, track_count.
    Silently tolerates mutagen failures.
    """
    try:
        from mutagen import File as MutagenFile  # type: ignore
    except Exception:
        return {"artist": "", "albumartist": "", "album": "", "year": "", "genres": [], "track_count": len(audio_files)}

    artist_vals: list[str] = []
    albumartist_vals: list[str] = []
    album_vals: list[str] = []
    year_vals: list[str] = []
    genre_raws: list[str] = []

    sample = audio_files[: min(8, len(audio_files))]
    for path in sample:
        try:
            mf = MutagenFile(str(path), easy=True)
        except Exception:
            continue
        if mf is None:
            continue
        tags = getattr(mf, "tags", None) or {}

        def first(key: str) -> str:
            v = tags.get(key) if hasattr(tags, "get") else None
            if not v:
                return ""
            if isinstance(v, list):
                return str(v[0]) if v else ""
            return str(v)

        if a := first("artist"):
            artist_vals.append(a)
        if aa := first("albumartist"):
            albumartist_vals.append(aa)
        if alb := first("album"):
            album_vals.append(alb)
        if y := (first("date") or first("year") or first("originaldate")):
            year_vals.append(y)
        if g := first("genre"):
            genre_raws.append(g)

    def _mode(vals: list[str]) -> str:
        if not vals:
            return ""
        return Counter(vals).most_common(1)[0][0]

    return {
        "artist": _mode(artist_vals),
        "albumartist": _mode(albumartist_vals),
        "album": _mode(album_vals),
        "year": extract_year(_mode(year_vals)),
        "genres": _uniq_preserve(majority_genres(genre_raws)),
        "track_count": len(audio_files),
    }
