"""Duration extraction.

Strategy:
  - For container formats that mutagen understands natively (FLAC, MP3, WAV, OGG, M4A/AAC, DSF, WV),
    read the length straight from the file header via mutagen. This is in-process and 10-100x faster
    than spawning ffprobe per file, which matters for libraries with thousands of files.
  - For everything else (DFF, ISO, VOB, APE, unknown extensions), shell out to ffprobe.
  - On mutagen failure, also fall back to ffprobe.

Also handles the "single large FLAC + CUE sheet" case implicitly: the FLAC's own duration is the
whole album's duration (no sub-track slicing needed for our purpose).
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

FFPROBE_BIN: str | None = None

# Extensions we trust mutagen to handle quickly and correctly.
MUTAGEN_EXTS = {".flac", ".mp3", ".wav", ".ogg", ".opus", ".m4a", ".aac", ".dsf", ".wv", ".ape"}

# Extensions where mutagen has no handler or unreliable length \u2014 ffprobe is required.
FFPROBE_ONLY_EXTS = {".dff", ".iso", ".vob"}


def ffprobe_available() -> bool:
    global FFPROBE_BIN
    if FFPROBE_BIN is not None:
        return bool(FFPROBE_BIN)
    found = shutil.which("ffprobe")
    FFPROBE_BIN = found or ""
    return bool(found)


def _ffprobe_duration(path: Path) -> float | None:
    if not ffprobe_available():
        return None
    try:
        res = subprocess.run(
            [
                FFPROBE_BIN,  # type: ignore[list-item]
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "json",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None
    if res.returncode != 0:
        return None
    try:
        data = json.loads(res.stdout or "{}")
        d = data.get("format", {}).get("duration")
        if d is None:
            return None
        return float(d)
    except (ValueError, json.JSONDecodeError):
        return None


def _mutagen_duration(path: Path) -> float | None:
    try:
        from mutagen import File as MutagenFile  # type: ignore
    except Exception:
        return None
    try:
        mf = MutagenFile(str(path))
    except Exception:
        return None
    if mf is None or not getattr(mf, "info", None):
        return None
    length = getattr(mf.info, "length", None)
    if length is None:
        return None
    try:
        return float(length)
    except (TypeError, ValueError):
        return None


def file_duration(path: Path) -> float | None:
    ext = path.suffix.lower()
    if ext in MUTAGEN_EXTS:
        d = _mutagen_duration(path)
        if d is not None and d > 0:
            return d
        # Fall through to ffprobe on mutagen failure.
    if ext in FFPROBE_ONLY_EXTS or ext not in MUTAGEN_EXTS:
        d = _ffprobe_duration(path)
        if d is not None and d > 0:
            return d
    # Last resort: try mutagen even on non-listed extensions.
    return _mutagen_duration(path)


def album_duration(audio_files: list[Path]) -> tuple[int, list[str]]:
    """Sum the duration of all files in an album. Returns (seconds, warnings).

    If a single-large-FLAC + CUE case is detected (one FLAC > 10 minutes and no other audio),
    trust that FLAC's duration as the whole album.
    """
    warnings: list[str] = []
    if not audio_files:
        return 0, ["no audio files"]

    total = 0.0
    any_ok = False
    for f in audio_files:
        d = file_duration(f)
        if d is None:
            warnings.append(f"duration unknown: {f.name}")
            continue
        any_ok = True
        total += d

    if not any_ok:
        return 0, warnings or ["all duration probes failed"]
    return int(round(total)), warnings
