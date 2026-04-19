"""Tape size configuration and bucketing helpers."""
from __future__ import annotations

from .models import Tape

TAPES: list[Tape] = [
    Tape(name="46min cassette", total_sec=46 * 60, split_sec=None),
    Tape(name="54min cassette", total_sec=54 * 60, split_sec=None),
    Tape(name="60min cassette/reel", total_sec=60 * 60, split_sec=30 * 60),
    Tape(name="70min cassette", total_sec=70 * 60, split_sec=35 * 60),
    Tape(name="90min cassette/reel", total_sec=90 * 60, split_sec=45 * 60),
    Tape(name="120min reel", total_sec=120 * 60, split_sec=60 * 60),
]


def tape_by_total_minutes(minutes: int) -> Tape | None:
    for t in TAPES:
        if t.total_sec == minutes * 60:
            return t
    return None


def smallest_fitting_tape(duration_sec: int) -> Tape | None:
    """Return the smallest tape whose total length >= duration. None if it doesn't fit any tape."""
    for t in TAPES:
        if duration_sec <= t.total_sec:
            return t
    return None


def bucket_label(duration_sec: int) -> str:
    """Human bucket label for report.md."""
    t = smallest_fitting_tape(duration_sec)
    if t is None:
        return ">120 (won't fit)"
    prev = 0
    for candidate in TAPES:
        if candidate is t:
            break
        prev = candidate.total_sec // 60
    upper = t.total_sec // 60
    if prev == 0:
        return f"<={upper}"
    return f"{prev}<x<={upper}"


def format_hms(duration_sec: int) -> str:
    total = max(0, int(duration_sec))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"
