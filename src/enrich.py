"""Genre enrichment: fill in missing (or too-vague) `genres` on Album records using
MusicBrainz first, then Last.fm (if an API key was supplied), then Wikipedia as a
last resort. All providers are disk-cached.

Two categories of album are enriched:

1. **Empty**: no genres at all. We ask the providers in order and take the first
   non-empty answer, then keep going to merge in additional providers' tags.
2. **Vague**: genres present but only generic top-level tags (e.g. just "Rock" or
   "Electronic"). The planner's relaxed-pairing logic can't discriminate anything
   useful between two albums that are both tagged literally "Rock", so we ask the
   providers whether they have anything more specific. Any specific tags we find
   are prepended to the album's genres; the vague original stays as a fallback.

The enrichment runs single-threaded on purpose: MusicBrainz's rate limit is 1
req/sec and Last.fm's / Wikipedia's politeness budgets are similar. A thread pool
would not actually help wall-clock time.
"""
from __future__ import annotations

from pathlib import Path

from .lastfm import LastFmClient
from .models import Album
from .musicbrainz import MBClient
from .wikipedia import WikipediaClient

# Tags we treat as "not specific enough to be useful for planning". Roughly the
# set of PARENT_MAP bucket names plus a couple of equally-generic alternates.
# If an album's ONLY tags come from this set, we'll ask the online databases
# whether they have something more specific.
_VAGUE_GENRES: frozenset[str] = frozenset({
    "rock", "pop", "metal", "punk",
    "electronic", "dance",
    "hip hop", "hip-hop", "rap",
    "r&b", "rnb", "soul",
    "jazz", "blues", "country", "folk", "classical",
    "reggae", "soundtrack",
    "alternative", "indie",
})


def _is_vague(genre: str) -> bool:
    return genre.strip().lower() in _VAGUE_GENRES


def _all_vague(genres: list[str]) -> bool:
    return bool(genres) and all(_is_vague(g) for g in genres)


def _merge_genres(existing: list[str], new_tags: list[str]) -> list[str]:
    """Merge new provider tags into existing ones, putting specific tags first.

    - Specific new tags come first (they're what we asked the provider for).
    - Then existing tags (including any vague ones) as a fallback.
    - Case-insensitive dedup; we keep the first-seen spelling.
    """
    seen_lower: set[str] = set()
    merged: list[str] = []
    # Specific new tags first, skipping anything equally vague (no improvement).
    for g in new_tags:
        key = g.strip().lower()
        if not key or key in seen_lower:
            continue
        if _is_vague(g):
            continue
        seen_lower.add(key)
        merged.append(g)
    # Then the original tags (including the vague ones, for fallback).
    for g in existing:
        key = g.strip().lower()
        if not key or key in seen_lower:
            continue
        seen_lower.add(key)
        merged.append(g)
    return merged


def enrich_albums(
    albums: list[Album],
    mb: MBClient | None,
    lastfm: LastFmClient | None,
    wiki: WikipediaClient | None = None,
    progress: bool = True,
) -> dict[str, int]:
    """Fill in empty or too-vague `album.genres` in-place using MB, Last.fm, then Wikipedia.

    Returns a summary dict with counts by source and by category (empty vs vague).
    """
    empty_targets = [a for a in albums if not a.genres and a.artist and a.album]
    vague_targets = [a for a in albums if a.genres and _all_vague(a.genres) and a.artist and a.album]
    targets = empty_targets + vague_targets

    summary = {
        "candidates": len(targets),
        "candidates_empty": len(empty_targets),
        "candidates_vague": len(vague_targets),
        "enriched_mb": 0,
        "enriched_lastfm": 0,
        "enriched_wiki": 0,
        "clarified_mb": 0,
        "clarified_lastfm": 0,
        "clarified_wiki": 0,
        "still_empty": 0,
        "still_vague": 0,
    }

    pbar = None
    if progress and targets:
        try:
            from tqdm import tqdm  # type: ignore
            pbar = tqdm(total=len(targets), unit="album", desc="Enriching")
        except Exception:
            pbar = None

    for a in targets:
        had_genres = bool(a.genres)
        # Provider-by-provider results, so we can credit whichever one supplied
        # the first SPECIFIC tag (not just the first non-empty answer).
        mb_tags: list[str] = []
        lf_tags: list[str] = []
        wiki_tags: list[str] = []
        first_nonempty = ""  # used as the credit source for empty-target albums

        if mb is not None and mb.enabled:
            mb_tags = list(mb.genres_for_album(a.artist, a.album, a.year))
            if mb_tags and not first_nonempty:
                first_nonempty = "mb"

        if lastfm is not None and lastfm.enabled:
            lf_tags = list(lastfm.genres_for_album(a.artist, a.album))
            if lf_tags and not first_nonempty:
                first_nonempty = "lastfm"

        # Wikipedia is expensive, so skip it if earlier providers already gave us
        # something specific. It runs if NO specific tag has shown up yet (either
        # because MB/LF returned nothing, or because they only returned vague tags).
        def _has_specific(tags: list[str]) -> bool:
            return any(not _is_vague(t) for t in tags)

        earlier_has_specific = _has_specific(mb_tags) or _has_specific(lf_tags)
        if not earlier_has_specific and wiki is not None and wiki.enabled:
            wiki_tags = list(wiki.genres_for_album(a.artist, a.album, a.year))
            if wiki_tags and not first_nonempty:
                first_nonempty = "wiki"

        # Merge all provider tags in priority order: MB, Last.fm, Wikipedia.
        combined: list[str] = []
        combined_lower: set[str] = set()
        for group in (mb_tags, lf_tags, wiki_tags):
            for t in group:
                key = t.strip().lower()
                if key and key not in combined_lower:
                    combined_lower.add(key)
                    combined.append(t)

        # Identify which provider contributed the first specific tag (the one
        # that would appear first in the merged output). Used for the summary.
        specific_src = ""
        for t in combined:
            if not _is_vague(t):
                if t in mb_tags:
                    specific_src = "mb"
                elif t in lf_tags:
                    specific_src = "lastfm"
                elif t in wiki_tags:
                    specific_src = "wiki"
                break

        if had_genres:
            # Clarification path: merge, keeping specifics first and the vague
            # originals as a fallback. Only count as "clarified" if something
            # more specific than what we had actually showed up.
            before = {g.lower() for g in a.genres}
            merged = _merge_genres(a.genres, combined)
            added_specific = any(
                not _is_vague(g) and g.lower() not in before for g in merged
            )
            a.genres = merged[:5]
            if added_specific and specific_src:
                summary[f"clarified_{specific_src}"] += 1
            else:
                summary["still_vague"] += 1
        else:
            # Empty path: take everything we found, or flag as still-empty.
            if combined:
                a.genres = combined[:5]
                if first_nonempty:
                    summary[f"enriched_{first_nonempty}"] += 1
            else:
                summary["still_empty"] += 1

        if pbar is not None:
            pbar.update(1)

    if pbar is not None:
        pbar.close()
    return summary


def build_clients(
    out_dir: Path,
    enable_mb: bool,
    lastfm_key: str | None,
    enable_wiki: bool = True,
) -> tuple[MBClient | None, LastFmClient | None, WikipediaClient | None]:
    mb = MBClient(cache_path=out_dir / ".mb-cache.json", enabled=enable_mb)
    lf_key = (lastfm_key or "").strip()
    lastfm = LastFmClient(api_key=lf_key, cache_path=out_dir / ".lastfm-cache.json") if lf_key else None
    wiki = WikipediaClient(cache_path=out_dir / ".wiki-cache.json", enabled=enable_wiki)
    return mb, lastfm, wiki
