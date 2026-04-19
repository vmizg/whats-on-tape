"""Tape pairing logic with the genre-match escalation ladder:
  1. solo (album fills tape by itself)
  2. local tight      - matching primary genre
  3. local relaxed    - overlapping genres / same parent genre
  4. MusicBrainz      - external album suggestions
  5. Last.fm          - external album suggestions (fallback when MB returns nothing useful)
  6. search-url       - RYM / Discogs pre-filled searches
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote_plus

from .lastfm import LastFmClient
from .models import Album, Assignment, SideCandidate, Tape
from .musicbrainz import MBClient
from .tapes import TAPES, effective_split_sec, effective_total_sec
from .trim import TrimResult, compute_trim, is_compilation_title

# Rough parent-genre normalization. Kept small and practical; extend as needed.
#
# Genre \u2192 parent bucket. Used by `_genre_match` in relaxed mode so two albums
# pair up when their primary tags share a parent rather than being literally equal.
#
# Design notes, since this gets easy to break:
# - The set of distinct VALUES here is the set of "buckets" the planner cares about.
#   Current buckets: rock, punk, metal, synthwave, dance, ambient, pop, hip hop,
#   r&b, soul, jazz, blues, country, folk, classical, soundtrack, reggae.
# - A few plain top-level genres (e.g. "electronic", "blues", "reggae") aren't
#   keys here because when they appear literally we'd rather they stand on their
#   own than be forcibly re-bucketed.
# - There's a test (`test_lt_translations_resolve_via_parent_map`) that pins
#   the invariant "every LT-translated genre resolves via this map".
#
# The three former "electronic" buckets are split intentionally:
# - **synthwave**: retro / song-oriented / 80s-sounding electronic (synth-pop,
#   new wave, italo disco, electroclash, \u2026). Stylistically melodic.
# - **dance**: modern club / dancefloor (house, techno, trance, EDM, dubstep,
#   drum & bass, \u2026). Beat-driven.
# - **ambient**: headphones / texture / non-dance (ambient, IDM, downtempo,
#   trip hop, electronica, chillwave, glitch, \u2026). Atmospheric.
#
PARENT_MAP: dict[str, str] = {
    # --- rock family ---
    "hard rock": "rock",
    "classic rock": "rock",
    "soft rock": "rock",
    "progressive rock": "rock",
    "prog rock": "rock",
    "prog-rock": "rock",
    "symphonic prog": "rock",
    "psychedelic rock": "rock",
    "psychedelic": "rock",
    "neo-psychedelia": "rock",
    "alternative rock": "rock",
    "alt rock": "rock",
    "alt-rock": "rock",
    "alternative": "rock",
    "alternative and indie": "rock",
    "alternative/indie rock": "rock",
    "alternative rock / pop": "rock",
    "alternative pop/rock": "rock",
    "adult alternative": "rock",
    "adult alternative pop/rock": "rock",
    "indie rock": "rock",
    "indie": "rock",
    "glam rock": "rock",
    "glam": "rock",
    "blues rock": "rock",
    "blues-rock": "rock",
    "stoner rock": "rock",
    "post-rock": "rock",
    "grunge": "rock",
    "rock and roll": "rock",
    "art rock": "rock",
    "album rock": "rock",
    "arena rock": "rock",
    "symphonic rock": "rock",
    "space rock": "rock",
    "krautrock": "rock",
    "garage rock": "rock",
    "garage rock revival": "rock",
    "garage psych": "rock",
    "heavy psych": "rock",
    "funk rock": "rock",
    "aussie rock": "rock",
    "anatolian rock": "rock",
    "lithuanian rock": "rock",
    "experimental rock": "rock",
    "gothic rock": "rock",
    "piano rock": "rock",
    "instrumental rock": "rock",
    "pop/rock": "rock",
    "pop / rock / metal": "rock",
    "pop rock": "rock",
    "dance-rock": "rock",
    "rock ballad": "rock",
    "rock ballads": "rock",
    "britpop": "rock",
    "post-britpop": "rock",
    "boogie rock": "rock",
    "country rock": "rock",
    "country-rock": "rock",
    "jazz-rock": "rock",
    "70s classic rock": "rock",

    # --- punk family ---
    "punk rock": "punk",
    "post-punk": "punk",
    "hardcore punk": "punk",
    "deathrock": "punk",
    "new rave": "punk",

    # --- metal family ---
    "heavy metal": "metal",
    "thrash metal": "metal",
    "death metal": "metal",
    "black metal": "metal",
    "power metal": "metal",
    "doom metal": "metal",
    "nu metal": "metal",
    "glam metal": "metal",
    "djent": "metal",
    "math metal": "metal",
    "nwobhm": "metal",
    "classic british metal": "metal",
    "metal & hard rock": "metal",

    # --- synthwave (retro / 80s-sounding electronic) ---
    "synthwave": "synthwave",
    "synth-pop": "synthwave",
    "synthpop": "synthwave",
    "synth pop": "synthwave",
    "synth funk": "synthwave",
    "new wave": "synthwave",
    "new romantic": "synthwave",
    "neue deutsche welle": "synthwave",
    "electro": "synthwave",
    "electropop": "synthwave",
    "electroclash": "synthwave",
    "french electro": "synthwave",
    "disco": "synthwave",
    "italo-disco": "synthwave",
    "italo disco": "synthwave",
    "euro-disco": "synthwave",
    "eurodisco": "synthwave",
    "nu disco": "synthwave",
    "electro-disco": "synthwave",
    "space disco": "synthwave",
    "spacesynth": "synthwave",
    "chillsynth": "synthwave",
    "europop": "synthwave",
    "eurodance": "synthwave",
    "dance-pop": "synthwave",
    "euro house": "synthwave",
    "austropop": "synthwave",

    # --- dance (modern club) ---
    "techno": "dance",
    "house": "dance",
    "electro house": "dance",
    "progressive house": "dance",
    "tech house": "dance",
    "trance": "dance",
    "edm": "dance",
    "dance": "dance",
    "club/dance": "dance",
    "dance/electronic": "dance",
    "dance & house": "dance",
    "drum and bass": "dance",
    "drum & bass": "dance",
    "dubstep": "dance",
    "breakbeat": "dance",
    "future bass": "dance",
    "alternative dance": "dance",
    "deconstructed club": "dance",

    # --- ambient (non-dance electronic / texture) ---
    "ambient": "ambient",
    "ambient house": "ambient",
    "ambient dub": "ambient",
    "ambient pop": "ambient",
    "dark ambient": "ambient",
    "idm": "ambient",
    "downtempo": "ambient",
    "metro downtempo": "ambient",
    "trip hop": "ambient",
    "trip-hop": "ambient",
    "electronica": "ambient",
    "indietronica": "ambient",
    "chillout": "ambient",
    "chillwave": "ambient",
    "leftfield": "ambient",
    "wonky": "ambient",
    "glitch hop": "ambient",
    "glitch pop": "ambient",
    "progressive electronic": "ambient",
    "funktronica": "ambient",
    "future jazz": "ambient",
    "new age": "ambient",

    # --- hip hop family ---
    "rap": "hip hop",
    "hip-hop": "hip hop",
    "hip-hop idm": "hip hop",
    "boom bap": "hip hop",
    "gangsta rap": "hip hop",
    "jazz rap": "hip hop",
    "pop rap": "hip hop",
    "instrumental hip hop": "hip hop",

    # --- r&b ---
    "contemporary r&b": "r&b",
    "adult contemporary r&b": "r&b",
    "alternative r&b": "r&b",
    "rnb": "r&b",
    "rhythm & blues": "r&b",

    # --- soul / funk ---
    "soul/funk": "soul",
    "funk": "soul",
    "neo soul": "soul",
    "smooth soul": "soul",
    "blue-eyed soul": "soul",
    "alt funk": "soul",

    # --- jazz ---
    "smooth jazz": "jazz",
    "jazz fusion": "jazz",
    "fusion": "jazz",
    "bebop": "jazz",
    "bop": "jazz",
    "hard bop": "jazz",
    "post-bop": "jazz",
    "post bop": "jazz",
    "cool jazz": "jazz",
    "modal jazz": "jazz",
    "swing": "jazz",
    "bossa nova": "jazz",
    "latin jazz": "jazz",
    "afro-cuban jazz": "jazz",
    "jazz-funk": "jazz",
    "jazz pop": "jazz",
    "vocal jazz": "jazz",
    "free jazz": "jazz",
    "dark jazz": "jazz",
    "spiritual jazz": "jazz",
    "contemporary jazz": "jazz",
    "mainstream jazz": "jazz",
    "instrumental jazz": "jazz",
    "third stream": "jazz",

    # --- blues ---
    "country blues": "blues",

    # --- country ---
    "bluegrass": "country",
    "americana": "country",

    # --- folk ---
    "singer-songwriter": "folk",
    "singer/songwriter": "folk",
    "folk rock": "folk",
    "chanson": "folk",

    # --- classical ---
    "baroque": "classical",
    "romantic": "classical",
    "classical period": "classical",
    "opera": "classical",
    "symphonic": "classical",
    "orchestra": "classical",
    "orchestral": "classical",
    "symphony": "classical",

    # --- soundtrack ---
    "film score": "soundtrack",
    "film soundtrack": "soundtrack",
    "movie soundtrack": "soundtrack",
    "ost": "soundtrack",
    "original motion picture soundtrack": "soundtrack",

    # --- reggae ---
    "dub": "reggae",
    "ragga": "reggae",
    "reggae-pop": "reggae",
    "roots reggae": "reggae",

    # --- pop (real bucket now) ---
    "song": "pop",
    "progressive pop": "pop",
    "psychedelic pop": "pop",
    "dream pop": "pop",
    "indie pop": "pop",
    "art pop": "pop",
    "baroque pop": "pop",
    "chamber pop": "pop",
    "power pop": "pop",
    "symphonic pop": "pop",
    "world pop": "pop",
    "city pop": "pop",
    "jpop": "pop",
    "bubblegum bass": "pop",
    "am pop": "pop",
    "classic pop and rock": "pop",
    "adult contemporary": "pop",
    "oldies": "pop",
}


def _norm(g: str) -> str:
    return (g or "").strip().lower()


def _parent(g: str) -> str:
    n = _norm(g)
    return PARENT_MAP.get(n, n)


def _genre_keyset(album: Album) -> set[str]:
    keys: set[str] = set()
    for g in album.genres:
        keys.add(_norm(g))
        keys.add(_parent(g))
    keys.discard("")
    return keys


def _genre_match(a: Album, b: Album, mode: str) -> bool:
    """mode: 'tight' -> same primary genre (case-insensitive); 'relaxed' -> any overlap via parent map."""
    if mode == "tight":
        return bool(a.primary_genre) and _norm(a.primary_genre) == _norm(b.primary_genre)
    if mode == "relaxed":
        ka = _genre_keyset(a)
        kb = _genre_keyset(b)
        return bool(ka & kb)
    return False


@dataclass
class PlannerConfig:
    buffer_sec: int = 60  # leave this much headroom per tape
    min_side_a_ratio: float = 0.40  # side A must be >= 40% of tape
    max_side_a_ratio: float = 0.95  # side A must be <= 95% of tape (else it's effectively solo)
    allow_musicbrainz: bool = True
    allow_lastfm: bool = True
    mb_min_ratio: float = 0.70  # external B-side candidate length must be >=70% of remaining
    mb_candidate_count: int = 5

    # Strict per-side sizing: when True, Side B on a split tape must fit entirely on a
    # single physical side (b.duration + buffer <= tape.split_sec) rather than sharing
    # a total-budget with Side A. Turn OFF with --allow-overlapping-sides for users who
    # record programs that span the midpoint.
    strict_side_fit: bool = True

    # Per-side slack caps used during PAIRING only: if either side would have more
    # unused time than these caps, the planner refuses to offer a loose pairing and
    # falls back to solo placement. Solo placement ignores these caps (short albums
    # are always allowed to sit on the smallest fitting tape).
    max_slack_small_sec: int = 10 * 60  # for sides <=45 min long (and solo tapes <=45 min)
    max_slack_large_sec: int = 15 * 60  # for sides >45 min long (and solo tapes >45 min)

    # Trim mode for over-length deluxe / anniversary / expanded-edition albums:
    #  - "off":      never trim; over-length albums land in the unplaced section.
    #  - "unplaced": only trim albums that would otherwise be unplaceable.
    #  - "all":      also trim albums that WOULD fit a large tape but could fit a
    #                smaller one after removing bonus tracks. Gives more flexibility
    #                but makes plan.md reflect an opinionated "core" for every
    #                deluxe edition rather than the full on-disk content.
    trim_mode: str = "unplaced"


def _fits_as_solo(album: Album, tape: Tape, cfg: PlannerConfig) -> bool:
    """Solo-fit check using the tape's EFFECTIVE capacity (nominal + stretch tolerance).

    The buffer is intentionally NOT applied here so that a 31-min album fits a
    30-min side with the default 120s tolerance. The buffer remains in effect for
    pairing decisions, where headroom between two albums actually matters.
    """
    return album.duration_sec <= effective_total_sec(tape)


def _side_capacity_sec(tape: Tape) -> int:
    """Effective capacity of one side (nominal split_sec + stretch tolerance).

    For solo (non-split) tapes, the whole tape IS the side so we use the full
    effective total length instead.
    """
    eff_split = effective_split_sec(tape)
    if eff_split is not None:
        return eff_split
    return effective_total_sec(tape)


def _max_slack_for_side(side_cap_sec: int, cfg: PlannerConfig) -> int:
    """Slack cap for a single side of a tape. Threshold is 45 min side length."""
    return cfg.max_slack_small_sec if side_cap_sec <= 45 * 60 else cfg.max_slack_large_sec


def _side_fits_under_cap(duration_sec: int, side_cap_sec: int, cfg: PlannerConfig) -> bool:
    """True when an album of this duration fits on a side AND the resulting
    side-slack is within the cap.

    `side_cap_sec` is expected to be the EFFECTIVE side capacity (nominal +
    stretch tolerance). Slack is computed against that effective capacity, so an
    album that fully consumes the stretch zone shows ~0 slack rather than a
    negative number.
    """
    if duration_sec > side_cap_sec:
        return False
    side_slack = side_cap_sec - duration_sec
    return side_slack <= _max_slack_for_side(side_cap_sec, cfg)


def _rym_search_url(genres: list[str]) -> str:
    g = ",".join(quote_plus(_norm(g).replace(" ", "-")) for g in genres if g)
    return f"https://rateyourmusic.com/customchart?genres={g}&limit=25&page=1"


def _discogs_search_url(genres: list[str], decade: str = "") -> str:
    parts: list[str] = []
    if genres:
        parts.append(f"genre={quote_plus(genres[0])}")
    parts.append("format=Album")
    if decade:
        parts.append(f"decade={decade}")
    return "https://www.discogs.com/search/?" + "&".join(parts)


def _decade_from_year(year: str) -> str:
    y = year.strip()[:4]
    if len(y) == 4 and y.isdigit():
        return y[:3] + "0"
    return ""


def _find_local_partner(
    side_a: Album,
    b_max_sec: int,
    b_min_sec: int,
    available: Iterable[Album],
    cfg: PlannerConfig,
    mode: str,
) -> Album | None:
    """Pick the longest-fitting library partner for Side B.

    - `b_max_sec`: hard upper bound on B's duration (physical side/remaining budget).
    - `b_min_sec`: lower bound on B's duration (enforces the slack cap on Side B).
    """
    best: Album | None = None
    best_fit = -1
    for b in available:
        if b.path == side_a.path:
            continue
        if b.duration_sec <= 0:
            continue
        if b.duration_sec + cfg.buffer_sec > b_max_sec:
            continue
        if b.duration_sec < b_min_sec:
            continue
        if not _genre_match(side_a, b, mode):
            continue
        if b.duration_sec > best_fit:
            best = b
            best_fit = b.duration_sec
    return best


def _genres_to_try(side_a: Album) -> list[str]:
    out: list[str] = []
    if side_a.primary_genre:
        out.append(side_a.primary_genre)
    for g in side_a.genres[1:]:
        if g not in out:
            out.append(g)
    parent = _parent(side_a.primary_genre) if side_a.primary_genre else ""
    if parent and parent not in [_norm(g) for g in out]:
        out.append(parent)
    return out[:3]


def _mb_candidates(
    side_a: Album,
    b_max_sec: int,
    b_min_sec: int,
    cfg: PlannerConfig,
    mb: MBClient | None,
) -> list[SideCandidate]:
    if not mb or not cfg.allow_musicbrainz or not mb.enabled:
        return []
    target_max = b_max_sec - cfg.buffer_sec
    # Prefer the tighter of the slack-cap minimum and the legacy mb_min_ratio floor.
    target_min = max(b_min_sec, int(target_max * cfg.mb_min_ratio))

    collected: list[SideCandidate] = []
    seen_mbids: set[str] = set()
    seen_labels: set[str] = set()
    for g in _genres_to_try(side_a):
        hits = mb.search_albums_by_genre(g, max_duration_sec=target_max, min_duration_sec=target_min)
        for h in hits:
            mbid = h.get("mbid") or ""
            if mbid and mbid in seen_mbids:
                continue
            label = f"{h.get('artist','')} - {h.get('title','')}"
            if label.lower() in seen_labels:
                continue
            seen_mbids.add(mbid)
            seen_labels.add(label.lower())
            collected.append(SideCandidate(
                source="musicbrainz",
                label=label,
                duration_sec=int(h.get("duration_sec", 0)),
                genre=h.get("genre", ""),
                url=h.get("url", ""),
            ))
            if len(collected) >= cfg.mb_candidate_count:
                return collected
    return collected


def _lastfm_candidates(
    side_a: Album,
    b_max_sec: int,
    b_min_sec: int,
    cfg: PlannerConfig,
    lastfm: LastFmClient | None,
    exclude_labels: set[str],
) -> list[SideCandidate]:
    if not lastfm or not lastfm.enabled or not cfg.allow_lastfm:
        return []
    target_max = b_max_sec - cfg.buffer_sec
    target_min = max(b_min_sec, int(target_max * cfg.mb_min_ratio))

    collected: list[SideCandidate] = []
    seen_labels: set[str] = set(exclude_labels)
    for g in _genres_to_try(side_a):
        hits = lastfm.search_albums_by_genre(g, max_duration_sec=target_max, min_duration_sec=target_min)
        for h in hits:
            label = f"{h.get('artist','')} - {h.get('title','')}"
            if label.lower() in seen_labels:
                continue
            seen_labels.add(label.lower())
            collected.append(SideCandidate(
                source="lastfm",
                label=label,
                duration_sec=int(h.get("duration_sec", 0)),
                genre=h.get("genre", ""),
                url=h.get("url", ""),
            ))
            if len(collected) >= cfg.mb_candidate_count:
                return collected
    return collected


def _search_url_candidates(side_a: Album) -> list[SideCandidate]:
    genres = side_a.genres or ([side_a.primary_genre] if side_a.primary_genre else [])
    out: list[SideCandidate] = []
    out.append(SideCandidate(source="search-url", label="RateYourMusic", url=_rym_search_url(genres) if genres else "https://rateyourmusic.com/"))
    out.append(SideCandidate(source="search-url", label="Discogs", url=_discogs_search_url(genres, _decade_from_year(side_a.year))))
    return out


def _largest_tape_sec() -> int:
    """Largest EFFECTIVE tape capacity, used as the trim heuristic's upper bound."""
    return max(effective_total_sec(t) for t in TAPES)


def _apply_trim_all(
    albums: list[Album],
    mb: MBClient | None,
    progress: bool,
) -> tuple[list[Album], dict[str, TrimResult]]:
    """Pre-pass for trim_mode='all': shrink every trimmable album before planning.

    Returns (planning_albums, trim_map). planning_albums is a new list with
    trimmed copies of each album where a trim applied; trim_map is keyed by
    album.path and holds the TrimResult so we can attach it to Assignments later.
    """
    trim_map: dict[str, TrimResult] = {}
    out: list[Album] = []
    largest = _largest_tape_sec()

    pbar = None
    if progress:
        try:
            from tqdm import tqdm  # type: ignore
            pbar = tqdm(total=len(albums), unit="album", desc="Trim scan")
        except Exception:
            pbar = None

    for a in albums:
        if a.duration_sec <= 0:
            out.append(a)
            if pbar is not None:
                pbar.update(1)
            continue
        # Only pay MB / tag-reading cost when there's a plausible benefit. If the
        # album already fits the smallest tape, skip outright. Same if it looks
        # like a compilation (refused by compute_trim anyway; short-circuit to
        # save one trim call per compilation album).
        if a.duration_sec <= effective_total_sec(TAPES[0]):
            out.append(a)
            if pbar is not None:
                pbar.update(1)
            continue
        if is_compilation_title(a.album):
            out.append(a)
            if pbar is not None:
                pbar.update(1)
            continue

        tr = compute_trim(a, max_tape_sec=largest, mb=mb)
        if tr.trimmed:
            trim_map[a.path] = tr
            out.append(replace(a, duration_sec=tr.trimmed_duration_sec))
        else:
            out.append(a)
        if pbar is not None:
            pbar.update(1)

    if pbar is not None:
        pbar.close()
    return out, trim_map


def plan_tapes(
    albums: list[Album],
    mb: MBClient | None = None,
    lastfm: LastFmClient | None = None,
    cfg: PlannerConfig | None = None,
    progress: bool = True,
) -> tuple[list[Assignment], list[Album]]:
    """Greedy tape planner.

    Strategy, per album (longest first):
      - fit as solo in the smallest tape where buffer + duration <= total.
      - if the album is <= tape.split_sec (i.e. fits on one side of a split-capable tape),
        look for a B-side partner via the escalation ladder.

    Albums that cannot be placed are returned as `unplaced`. When
    `cfg.trim_mode != 'off'`, over-length deluxe / expanded editions may be trimmed
    to a fittable "core" duration via MusicBrainz or a track-title heuristic.
    """
    cfg = cfg or PlannerConfig()
    assignments: list[Assignment] = []
    unplaced: list[Album] = []

    # Trim pre-pass: for trim_mode='all' we replace over-length albums with trimmed
    # planning copies BEFORE the main loop, so the greedy planner makes decisions
    # against the effective (shorter) durations. The `trim_map` retains metadata so
    # the emitted Assignment can show "trimmed to 44:08 (saves 1:29:12)" etc.
    trim_map: dict[str, TrimResult] = {}
    if cfg.trim_mode == "all":
        albums, trim_map = _apply_trim_all(list(albums), mb, progress)

    used_paths: set[str] = set()
    remaining_albums: list[Album] = sorted(
        [a for a in albums if a.duration_sec > 0],
        key=lambda a: -a.duration_sec,
    )

    min_side_b_sec = 10 * 60  # an album must leave >=10 min of other-side room to warrant pairing

    pbar = None
    if progress and remaining_albums:
        try:
            from tqdm import tqdm  # type: ignore
            pbar = tqdm(total=len(remaining_albums), unit="album", desc="Planning")
        except Exception:
            pbar = None

    def _tick(kind: str = "") -> None:
        if pbar is not None:
            if kind:
                pbar.set_postfix_str(kind, refresh=False)
            pbar.update(1)

    def _attach_trim(asn: Assignment) -> Assignment:
        """Populate side_a_trim_* / side_b_trim_* from trim_map (by path)."""
        tr_a = trim_map.get(asn.side_a.path)
        if tr_a is not None:
            asn.side_a_trim_note = tr_a.note
            asn.side_a_trim_skipped = list(tr_a.skip_labels)
            asn.side_a_original_sec = tr_a.original_duration_sec
        if asn.side_b is not None:
            tr_b = trim_map.get(asn.side_b.path)
            if tr_b is not None:
                asn.side_b_trim_note = tr_b.note
                asn.side_b_trim_skipped = list(tr_b.skip_labels)
                asn.side_b_original_sec = tr_b.original_duration_sec
        return asn

    for a in list(remaining_albums):
        if a.path in used_paths:
            _tick("paired")
            continue

        # 1) Prefer pairing on a split-capable tape when Side A itself won't
        #    waste too much of its side (we refuse loose pairings under the
        #    per-side slack cap). Scan tapes in order; the first split tape
        #    where Side A fits under the cap AND at least min_side_b_sec of
        #    Side B remains is the pairing target.
        split_tape: Tape | None = None
        split_side_cap: int = 0
        for tape in TAPES:
            if not tape.splits:
                continue
            if not _fits_as_solo(a, tape, cfg):
                continue
            side_cap = _side_capacity_sec(tape)
            if not _side_fits_under_cap(a.duration_sec, side_cap, cfg):
                # Side A would leave too much slack on its own side; don't pair here.
                continue
            # Space left on the OTHER side for B. Without strict mode we allow
            # Side B to spill into what would be Side A's remaining minutes, matching
            # the old behavior (useful for cross-midpoint programs). Both modes use
            # EFFECTIVE capacities so the stretch tolerance applies symmetrically.
            if cfg.strict_side_fit:
                b_max = side_cap - cfg.buffer_sec
            else:
                b_max = effective_total_sec(tape) - a.duration_sec - cfg.buffer_sec
            if b_max >= min_side_b_sec:
                split_tape = tape
                split_side_cap = side_cap
                break

        # 2) The smallest tape on which the album can sit solo (for the fallback path).
        solo_tape: Tape | None = None
        for tape in TAPES:
            if _fits_as_solo(a, tape, cfg):
                solo_tape = tape
                break

        if split_tape is None and solo_tape is None:
            unplaced.append(a)
            used_paths.add(a.path)
            _tick("unplaced")
            continue

        # Try to pair on the split-capable tape before falling back to solo.
        if split_tape is not None:
            # Upper bound on B: physical side (strict) or remaining budget
            # (overlapping). Both branches use effective capacities.
            if cfg.strict_side_fit:
                b_max_sec = split_side_cap
            else:
                b_max_sec = effective_total_sec(split_tape) - a.duration_sec
            # Lower bound on B: whichever is tighter between the minimum-room threshold
            # and the slack-cap floor (keep B-side slack <= cap).
            b_side_slack_cap = _max_slack_for_side(split_side_cap, cfg)
            b_min_sec = max(min_side_b_sec, b_max_sec - b_side_slack_cap)

            available = (x for x in remaining_albums if x.path not in used_paths)
            partner = _find_local_partner(a, b_max_sec, b_min_sec, available, cfg, mode="tight")
            match_kind = "tight-local" if partner else ""

            if partner is None:
                available = (x for x in remaining_albums if x.path not in used_paths)
                partner = _find_local_partner(a, b_max_sec, b_min_sec, available, cfg, mode="relaxed")
                if partner:
                    match_kind = "relaxed-local"

            if partner is not None:
                assignments.append(_attach_trim(
                    Assignment(tape=split_tape, side_a=a, side_b=partner, match_kind=match_kind)
                ))
                used_paths.add(a.path)
                used_paths.add(partner.path)
                _tick(match_kind)
                continue

            # No local partner found for the split tape.
            # If there is a tighter solo tape (e.g. 46min cassette) where this album would
            # almost fill the tape anyway, prefer the solo placement so we don't waste a
            # split-capable tape asking for external suggestions.
            if solo_tape is not None and solo_tape.total_sec < split_tape.total_sec:
                slack = effective_total_sec(solo_tape) - a.duration_sec - cfg.buffer_sec
                if slack < min_side_b_sec:
                    assignments.append(_attach_trim(
                        Assignment(tape=solo_tape, side_a=a, match_kind="solo")
                    ))
                    used_paths.add(a.path)
                    _tick("solo")
                    continue

            # Otherwise escalate: MusicBrainz, then Last.fm, then search URLs.
            # The "ext lookup" postfix is only meaningful when we're actually
            # about to hit the network. Skip it when:
            #   - both MB and Last.fm are disabled/offline, OR
            #   - all MB genre lookups for this album are already cached (in
            #     which case the call resolves in microseconds and the label
            #     would flicker misleadingly).
            mb_live = (
                cfg.allow_musicbrainz
                and mb is not None
                and getattr(mb, "enabled", False)
                and getattr(mb, "_ready", False)
            )
            lf_live = cfg.allow_lastfm and lastfm is not None
            if pbar is not None and (mb_live or lf_live):
                target_max = b_max_sec - cfg.buffer_sec
                target_min = max(b_min_sec, int(target_max * cfg.mb_min_ratio))
                genres = _genres_to_try(a)
                # `is_genre_search_cached` is present on real clients but may be
                # missing on test doubles; assume "not cached" in that case so
                # the postfix still appears, matching pre-optimization behavior.
                def _all_cached(client: Any, live: bool) -> bool:
                    if not live:
                        return True
                    probe = getattr(client, "is_genre_search_cached", None)
                    if probe is None:
                        return False
                    return all(probe(g, target_max, target_min) for g in genres)
                if not (_all_cached(mb, mb_live) and _all_cached(lastfm, lf_live)):
                    pbar.set_postfix_str(f"ext lookup: {a.artist[:24]}", refresh=True)
            mb_cands = _mb_candidates(a, b_max_sec, b_min_sec, cfg, mb)
            seen_labels = {c.label.lower() for c in mb_cands}
            lf_cands: list[SideCandidate] = []
            # Only call Last.fm if MB didn't find enough good candidates (saves API calls).
            if len(mb_cands) < cfg.mb_candidate_count:
                lf_cands = _lastfm_candidates(a, b_max_sec, b_min_sec, cfg, lastfm, exclude_labels=seen_labels)
            url_cands = _search_url_candidates(a)
            b_candidates = mb_cands + lf_cands + url_cands

            if mb_cands:
                kind = "musicbrainz" if not lf_cands else "musicbrainz+lastfm"
            elif lf_cands:
                kind = "lastfm"
            else:
                kind = "search-url"
            note = ""
            if not a.primary_genre:
                note = "no genre tag on album; suggestions may be off \u2014 consider tagging the album"
            assignments.append(_attach_trim(Assignment(
                tape=split_tape,
                side_a=a,
                side_b=None,
                b_candidates=b_candidates,
                match_kind=kind,
                note=note,
            )))
            used_paths.add(a.path)
            _tick(kind)
            continue

        # No split-pairing opportunity: solo on the smallest fitting tape.
        assert solo_tape is not None
        assignments.append(_attach_trim(
            Assignment(tape=solo_tape, side_a=a, match_kind="solo")
        ))
        used_paths.add(a.path)
        _tick("solo")

    if pbar is not None:
        pbar.close()

    # Post-pass for trim_mode='unplaced': for each over-length album in `unplaced`,
    # try to trim and re-place. We do this AFTER the main loop so the normal planning
    # is unaffected for albums that already fit; only the unplaceable ones get the
    # expensive MB / tag-reading treatment.
    if cfg.trim_mode == "unplaced" and unplaced:
        rescued = _trim_and_replace_unplaced(unplaced, mb, trim_map, cfg, progress)
        if rescued:
            for asn in rescued:
                assignments.append(_attach_trim(asn))
            # Any album we successfully rescued comes out of the unplaced list.
            rescued_paths = {asn.side_a.path for asn in rescued}
            unplaced = [a for a in unplaced if a.path not in rescued_paths]

    return assignments, unplaced


def _trim_and_replace_unplaced(
    unplaced: list[Album],
    mb: MBClient | None,
    trim_map: dict[str, TrimResult],
    cfg: PlannerConfig,
    progress: bool,
) -> list[Assignment]:
    """Try to trim each over-length unplaced album and place the trimmed version
    on the smallest fitting tape (solo or as Side A with no partner committed).

    We don't try to pair trimmed albums with other library albums here, since
    trimmed albums are almost always long-ish (~50-60 min core) and their most
    natural fit is a solo tape. This keeps the rescue pass focused and cheap.
    """
    rescued: list[Assignment] = []
    largest = _largest_tape_sec()

    pbar = None
    if progress and unplaced:
        try:
            from tqdm import tqdm  # type: ignore
            pbar = tqdm(total=len(unplaced), unit="album", desc="Trim rescue")
        except Exception:
            pbar = None

    for a in unplaced:
        if a.duration_sec <= 0:
            if pbar is not None:
                pbar.update(1)
            continue
        # Albums that DON'T exceed the largest tape are unplaced for some other reason
        # (e.g. "no compatible pairing slot"); trimming won't rescue those.
        if a.duration_sec <= largest:
            if pbar is not None:
                pbar.update(1)
            continue
        tr = compute_trim(a, max_tape_sec=largest, mb=mb)
        if not tr.trimmed:
            if pbar is not None:
                pbar.update(1)
            continue

        # Place the trimmed copy on the smallest tape it fits.
        trimmed = replace(a, duration_sec=tr.trimmed_duration_sec)
        chosen: Tape | None = None
        for tape in TAPES:
            if _fits_as_solo(trimmed, tape, cfg):
                chosen = tape
                break
        if chosen is None:
            # Trim wasn't enough to fit even the largest tape. Give up.
            if pbar is not None:
                pbar.update(1)
            continue

        trim_map[a.path] = tr
        rescued.append(Assignment(
            tape=chosen,
            side_a=trimmed,
            match_kind="solo-trimmed",
        ))
        if pbar is not None:
            pbar.update(1)

    if pbar is not None:
        pbar.close()
    return rescued


def load_candidates_filter(candidates_path: Path, albums: list[Album]) -> list[Album]:
    """Filter albums to those whose path appears in candidates_path (one per line, blanks + `#` ignored)."""
    lines = [ln.strip() for ln in candidates_path.read_text(encoding="utf-8").splitlines()]
    wanted = {ln for ln in lines if ln and not ln.startswith("#")}
    if not wanted:
        return albums
    wanted_norm = {_winpath_norm(p) for p in wanted}
    return [a for a in albums if _winpath_norm(a.path) in wanted_norm]


def _winpath_norm(p: str) -> str:
    return p.replace("/", "\\").rstrip("\\").lower()
