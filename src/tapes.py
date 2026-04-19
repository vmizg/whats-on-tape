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


# How many seconds OVER a tape's nominal capacity we still consider "physically
# fits" (cassettes typically have 1-3% extra recordable lead, and the user is
# happy to accept a few seconds of clipped tail rather than skip the album
# entirely). Keyed by capacity-in-seconds. Lookup picks the largest key
# <= the capacity being checked, so this works for both whole-tape sizes and
# per-side capacities (split_sec).
#
# Tweak these to play with planning: bigger tolerances place more albums
# (at the cost of risking a clipped track tail); smaller tolerances are stricter.
#
# Defaults are calibrated so that, with the default planner buffer of 60s, an
# album can be roughly 1-3 minutes longer than the nominal side/tape and still
# be placed:
#   30-min side  + 120s tolerance  -> 31-min album fits (30 + 1, with 60s buffer)
#   45-min side  + 180s tolerance  -> 47-min album fits
#   60-min tape  + 180s tolerance  -> 62-min album fits
#  120-min tape  + 300s tolerance  -> 124-min album fits
STRETCH_TOLERANCE_SEC: dict[int, int] = {
    0:        60,   # tiny baseline (anything <30 min)
    30 * 60:  120,
    35 * 60:  120,
    45 * 60:  180,
    46 * 60:  180,
    54 * 60:  180,
    60 * 60:  180,
    70 * 60:  240,
    90 * 60:  240,
    120 * 60: 300,
}


def stretch_tolerance_sec(capacity_sec: int) -> int:
    """Return the stretch tolerance (seconds) for a given nominal capacity.

    Matches the largest key <= capacity_sec in STRETCH_TOLERANCE_SEC. Falls
    back to 0 if capacity_sec is below every key (defensive; key 0 covers it).
    """
    if capacity_sec <= 0:
        return 0
    best_key = -1
    best_val = 0
    for k, v in STRETCH_TOLERANCE_SEC.items():
        if k <= capacity_sec and k > best_key:
            best_key = k
            best_val = v
    return best_val


def effective_total_sec(tape: Tape) -> int:
    """tape.total_sec plus its stretch tolerance."""
    return tape.total_sec + stretch_tolerance_sec(tape.total_sec)


def effective_split_sec(tape: Tape) -> int | None:
    """tape.split_sec plus its stretch tolerance, or None if the tape doesn't split."""
    if tape.split_sec is None:
        return None
    return tape.split_sec + stretch_tolerance_sec(tape.split_sec)


def tape_by_total_minutes(minutes: int) -> Tape | None:
    for t in TAPES:
        if t.total_sec == minutes * 60:
            return t
    return None


def smallest_fitting_tape(duration_sec: int) -> Tape | None:
    """Return the smallest tape whose effective length (nominal + stretch tolerance)
    is >= duration. Returns None if it doesn't fit any tape.
    """
    for t in TAPES:
        if duration_sec <= effective_total_sec(t):
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
