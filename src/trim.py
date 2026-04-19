"""Trim over-length albums to a fittable "core" duration for tape planning.

Problem: deluxe / expanded / anniversary editions often blow past any tape length
because of demos, alternate mixes, remastered-single versions, bonus discs, etc.
A 2.5-hour "Super Deluxe" of a 42-minute original album shouldn't be reported as
unplaced when the user will obviously just skip the bonus material on recording day.

Two strategies, in priority order:

1. **MusicBrainz canonical-release length**. For the (artist, album) pair, find
   candidate release-groups on MB, then pick a representative short "core" release
   (typically the earliest / shortest) and use its total length. This is the most
   authoritative: we get the actual duration of the original LP or the single-disc
   pressing. Weakness: MB matching is noisy and release-groups for compilations are
   often a single long release-group too.

2. **Track-title heuristic**. Open the album's audio files with mutagen, read per-track
   titles, and flag tracks whose titles match common "bonus" patterns: `(Demo)`,
   `(Live)`, `(Alternate Mix)`, `(Remix)`, `(Extended Mix)`, `(Single Version)`,
   `(Instrumental)`, `(Remaster*)`, `(B-Side)`, `(Early Version)`, `(Bonus Track)`,
   `(Previously Unreleased)`, `(Outtake)`, etc. Subtract those durations. Cheap and
   works offline, but only catches tracks whose rips have descriptive titles.

Guardrail: we refuse to trim compilations / live albums / best-ofs / retrospectives.
For those there's no meaningful "core" — the whole thing IS the album. We flag them
with a note instead so the user can hand-split if they want.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .models import Album

# ---------------------------------------------------------------------------
# Regex tables
# ---------------------------------------------------------------------------

# Title suffixes that imply this is a reissue / expanded version of a shorter
# original. Matching any of these is what makes us WILLING to trim the album.
_REISSUE_SUFFIX = re.compile(
    r"\b(?:"
    r"deluxe(?:\s+edition)?|super\s+deluxe|expanded(?:\s+edition)?|"
    r"extended(?:\s+edition)?|anniversary(?:\s+edition)?|"
    r"\d+(?:st|nd|rd|th)\s+anniversary|"
    r"remastered(?:\s+edition)?|remaster(?:\s+edition)?|"
    r"collector'?s\s+edition|legacy\s+edition|special\s+edition|"
    r"reissue|re-?release|"
    r"immersion\s+box\s+set|box\s+set|"
    r"further\s+listening|bonus\s+disc|"
    r"hi[\s-]*res"
    r")\b",
    re.IGNORECASE,
)

# Titles that indicate "this is a compilation / live album and trimming isn't
# meaningful". Trim is refused for these; a note is added instead.
_COMPILATION_SUFFIX = re.compile(
    r"\b(?:"
    r"best\s+of|greatest\s+hits|essential|the\s+essential|collection|"
    r"the\s+collection|singles|selected|retrospective|anthology|"
    r"the\s+very\s+best|classics|the\s+classics|compilation|"
    r"live(?:\s+at|\s+in|\s+from)?|the\s+story\s+so\s+far|"
    r"bootleg\s+series|fillmore|pulse|"
    r"original\s+(?:motion\s+picture\s+)?soundtrack|ost|score"
    r")\b",
    re.IGNORECASE,
)

# Track-title patterns used by the heuristic pass. The bracketed annotation must be
# surrounded by `(...)` or `[...]` (or the bare word at end of title) to avoid false
# positives (e.g. "Remaster" as part of a proper title).
_BONUS_TRACK_ANNOTATIONS = [
    "demo",
    "demos",
    "alternate",
    "alternate mix",
    "alternate version",
    "alternate take",
    "alt. mix",
    "alt mix",
    "alternative version",
    "extended",
    "extended mix",
    "extended version",
    "remix",
    "remixes",
    "re-mix",
    "re-recorded",
    "rerecorded",
    "instrumental",
    "instrumental version",
    "single version",
    "single edit",
    "radio edit",
    "edit",
    "7\" version",
    "7-inch version",
    "12\" version",
    "12-inch version",
    "mono",
    "mono version",
    "stereo",
    "stereo version",
    "bonus",
    "bonus track",
    "bonus tracks",
    "b-side",
    "b side",
    "outtake",
    "outtakes",
    "early version",
    "early mix",
    "rough mix",
    "rough cut",
    "unreleased",
    "previously unreleased",
    "live",  # inside parens on a studio album this almost always means a bonus live cut
    "live version",
    "session",
    "bbc session",
    "bbc sessions",
    "peel session",
    "peel sessions",
    "interview",
    "documentary",
    "commentary",
    "remaster",
    "remastered",
    "remastered version",
    "2009 remaster",
    "2011 remaster",
    "a cappella",
    "acapella",
    "acoustic",
    "acoustic version",
    "reprise",
    "intro",
    "outro",
    "skit",
    "hidden track",
]

# Compiled once: match "(thing)" or "[thing]" where `thing` is one of our annotations.
# The capture is just the annotation body, case-insensitive.
_bracket_re = re.compile(
    r"[\(\[]\s*(" + "|".join(
        re.escape(p) for p in sorted(_BONUS_TRACK_ANNOTATIONS, key=len, reverse=True)
    ) + r")\s*[\)\]]",
    re.IGNORECASE,
)

# Dash-separated trailing annotation: "Track Name - Demo" or "Track Name - Live".
# Only activates when the annotation is the *final* component after a dash.
_dash_re = re.compile(
    r"\s[-\u2013\u2014]\s(" + "|".join(
        re.escape(p) for p in sorted(_BONUS_TRACK_ANNOTATIONS, key=len, reverse=True)
    ) + r")\s*$",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class TrimResult:
    """Outcome of attempting to trim an album to a fittable core length."""
    original_duration_sec: int
    trimmed_duration_sec: int  # equals original when no trim was applied
    method: str = "none"  # "mb" | "title-heuristic" | "none"
    skip_labels: list[str] = field(default_factory=list)  # human-readable: "tracks 12-22" or track titles
    note: str = ""  # short human-readable summary shown in plan.md
    refused_reason: str = ""  # if set, trim was refused (e.g. "compilation")

    @property
    def trimmed(self) -> bool:
        return self.method != "none" and self.trimmed_duration_sec < self.original_duration_sec


# ---------------------------------------------------------------------------
# Classifiers
# ---------------------------------------------------------------------------

def is_reissue_title(album_title: str) -> bool:
    return bool(_REISSUE_SUFFIX.search(album_title or ""))


def is_compilation_title(album_title: str) -> bool:
    return bool(_COMPILATION_SUFFIX.search(album_title or ""))


# ---------------------------------------------------------------------------
# Track-title heuristic
# ---------------------------------------------------------------------------

def _list_audio_files(folder: Path) -> list[Path]:
    """Recursive enumeration of audio files in an album folder.

    Mirrors discovery.py's set of audio extensions but is intentionally local
    here to keep this module free of circular imports.
    """
    audio_exts = {".flac", ".mp3", ".wav", ".ogg", ".opus", ".m4a", ".aac", ".dsf",
                  ".wv", ".ape", ".dff"}
    try:
        files: list[Path] = []
        stack = [folder]
        while stack:
            current = stack.pop()
            for p in current.iterdir():
                if p.is_file() and p.suffix.lower() in audio_exts:
                    files.append(p)
                elif p.is_dir():
                    stack.append(p)
        files.sort()
        return files
    except (PermissionError, OSError):
        return []


def _read_track_info(path: Path) -> tuple[str, float]:
    """Return (title, duration_sec) for a single audio file, or ('', 0) on failure."""
    try:
        from mutagen import File as MutagenFile  # type: ignore
    except Exception:
        return "", 0.0
    try:
        mf = MutagenFile(str(path), easy=True)
    except Exception:
        return "", 0.0
    if mf is None:
        return "", 0.0

    title = ""
    tags = getattr(mf, "tags", None) or {}
    try:
        v = tags.get("title") if hasattr(tags, "get") else None
        if v:
            title = str(v[0]) if isinstance(v, list) and v else str(v)
    except Exception:
        title = ""

    duration = 0.0
    info = getattr(mf, "info", None)
    if info is not None:
        length = getattr(info, "length", 0) or 0
        try:
            duration = float(length)
        except (TypeError, ValueError):
            duration = 0.0

    return title, duration


def _is_bonus_title(title: str) -> bool:
    """True when the track title looks like a bonus / alternate / remix / demo / etc."""
    if not title:
        return False
    if _bracket_re.search(title):
        return True
    if _dash_re.search(title):
        return True
    return False


def trim_by_track_titles(album_folder: Path) -> tuple[int, list[str]]:
    """Sum durations of non-bonus tracks.

    Returns (core_seconds, skipped_track_labels). If no track info is readable,
    returns (0, []) and the caller should treat that as "heuristic failed".
    """
    files = _list_audio_files(album_folder)
    if not files:
        return 0, []

    core_sec = 0.0
    skipped: list[str] = []
    for f in files:
        title, dur = _read_track_info(f)
        if dur <= 0:
            continue
        label = title or f.stem
        if _is_bonus_title(label):
            skipped.append(label)
        else:
            core_sec += dur
    return int(round(core_sec)), skipped


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def compute_trim(
    album: Album,
    max_tape_sec: int,
    mb: Any | None = None,  # MBClient | None; Any avoids a forward-import dance
    min_improvement_sec: int = 60,
) -> TrimResult:
    """Attempt to shrink an album's effective duration to fit tape.

    Order of operations:
      1. If album is a compilation/live/best-of, refuse with a note.
      2. Try MusicBrainz canonical-release lookup (if the client is available).
         Accept the MB answer if it's materially shorter than the album AND
         fits `max_tape_sec`.
      3. Otherwise run the title heuristic on the album folder's audio files.
      4. If nothing reduced the album below `max_tape_sec + min_improvement_sec`,
         return a "none" result (caller treats the album as still unplaced).
    """
    result = TrimResult(
        original_duration_sec=album.duration_sec,
        trimmed_duration_sec=album.duration_sec,
    )

    if is_compilation_title(album.album):
        result.refused_reason = "compilation/live"
        result.note = "whole-album compilation/live release; consider a manual 2-sided split"
        return result

    # --- MusicBrainz canonical lookup ---
    if mb is not None and getattr(mb, "enabled", False) and getattr(mb, "_ready", False):
        mb_sec = 0
        try:
            mb_sec = int(mb.canonical_release_length_sec(album.artist, album.album, album.year))
        except Exception:
            mb_sec = 0
        if mb_sec > 0 and mb_sec + min_improvement_sec < album.duration_sec and mb_sec <= max_tape_sec:
            result.trimmed_duration_sec = mb_sec
            result.method = "mb"
            saved = album.duration_sec - mb_sec
            result.note = (
                f"trimmed to MusicBrainz canonical release length "
                f"({_fmt_hms(mb_sec)} vs. {_fmt_hms(album.duration_sec)} on disk; saves {_fmt_hms(saved)})"
            )
            return result

    # --- Track-title heuristic ---
    folder = Path(album.path)
    if folder.exists() and folder.is_dir():
        core_sec, skipped = trim_by_track_titles(folder)
        if core_sec > 0 and core_sec + min_improvement_sec < album.duration_sec and core_sec <= max_tape_sec:
            result.trimmed_duration_sec = core_sec
            result.method = "title-heuristic"
            result.skip_labels = skipped
            saved = album.duration_sec - core_sec
            n = len(skipped)
            result.note = (
                f"trimmed via track-title heuristic: skip {n} bonus track(s), "
                f"saves {_fmt_hms(saved)}"
            )
            return result

    return result


def _fmt_hms(sec: int) -> str:
    total = max(0, int(sec))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"
