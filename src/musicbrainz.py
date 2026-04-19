"""Thin MusicBrainz wrapper with disk cache and polite rate limiting.

The public MusicBrainz API asks for:
- a User-Agent identifying the application
- at most ~1 request/sec

We expose one function `search_albums_by_genre(genre, max_duration_sec, min_duration_sec)`
that returns a list of {artist, title, duration_sec, genre, url, mbid} dicts.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

USER_AGENT = ("MusicTapePlanner", "0.1", "https://github.com/local/music-tape-planner")
CACHE_TTL_SEC = 24 * 60 * 60


class MBClient:
    def __init__(self, cache_path: Path, enabled: bool = True):
        self.cache_path = cache_path
        self.enabled = enabled
        self._cache = self._load_cache()
        self._ready = False
        if enabled:
            self._ready = self._init_client()

    def _init_client(self) -> bool:
        try:
            import musicbrainzngs  # type: ignore
        except Exception:
            return False
        musicbrainzngs.set_useragent(*USER_AGENT)
        musicbrainzngs.set_rate_limit(limit_or_interval=1.0, new_requests=1)
        self._mb = musicbrainzngs
        return True

    def _load_cache(self) -> dict[str, dict[str, Any]]:
        if not self.cache_path.exists():
            return {}
        try:
            data = json.loads(self.cache_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except (OSError, json.JSONDecodeError):
            pass
        return {}

    def _save_cache(self) -> None:
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            self.cache_path.write_text(json.dumps(self._cache, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            pass

    def _cache_get(self, key: str) -> Any | None:
        entry = self._cache.get(key)
        if not entry:
            return None
        ts = entry.get("ts", 0)
        if time.time() - ts > CACHE_TTL_SEC:
            return None
        return entry.get("value")

    def _cache_put(self, key: str, value: Any) -> None:
        self._cache[key] = {"ts": time.time(), "value": value}
        self._save_cache()

    def genres_for_album(self, artist: str, album: str, year: str = "") -> list[str]:
        """Look up MB release-group tags for (artist, album). Returns top tags (by vote count).

        Returns [] on miss (but caches the miss with the normal TTL so repeat scans are quick).
        """
        if not self.enabled or not self._ready or not artist or not album:
            return []

        key = f"genres|{artist.lower()}|{album.lower()}|{year[:4] if year else ''}"
        cached = self._cache_get(key)
        if cached is not None:
            return list(cached)

        query_parts = [f'artist:"{_escape(artist)}"', f'release:"{_escape(album)}"']
        query = " AND ".join(query_parts)
        try:
            res = self._mb.search_release_groups(query=query, limit=5)
        except Exception:
            self._cache_put(key, [])
            return []

        groups = res.get("release-group-list", []) if isinstance(res, dict) else []
        # Pick the best match: highest ext:score, prefer matching year when we have one.
        best_group = None
        best_score = -1
        want_year = year[:4] if year else ""
        for g in groups:
            try:
                score = int(g.get("ext:score", 0))
            except (TypeError, ValueError):
                score = 0
            if want_year:
                first_release = g.get("first-release-date", "") or ""
                if first_release.startswith(want_year):
                    score += 5
            if score > best_score:
                best_group = g
                best_score = score

        if not best_group:
            self._cache_put(key, [])
            return []

        # Release-group tags are sometimes in the search result directly, but usually thin.
        # Fetch the release-group with includes=["tags"] to get full, vote-counted tags.
        tags: list[tuple[str, int]] = []
        try:
            rg_res = self._mb.get_release_group_by_id(best_group["id"], includes=["tags"])
            rg = rg_res.get("release-group", {}) if isinstance(rg_res, dict) else {}
            tag_list = rg.get("tag-list", []) or []
            for t in tag_list:
                name = t.get("name", "") or ""
                try:
                    count = int(t.get("count", 0))
                except (TypeError, ValueError):
                    count = 0
                if name:
                    tags.append((name, count))
        except Exception:
            pass

        tags.sort(key=lambda x: (-x[1], x[0].lower()))
        out = [name for name, count in tags if count >= 1 and _looks_like_genre(name)][:5]
        self._cache_put(key, out)
        return out

    def canonical_release_length_sec(self, artist: str, album: str, year: str = "") -> int:
        """Return the duration (seconds) of a representative "core" release for
        this (artist, album) pair, or 0 if not confidently resolved.

        Strategy:
          - Find the best release-group match (same scoring as `genres_for_album`).
          - Browse its releases with media+recordings, preferring the earliest
            release whose duration is under 90 minutes (single LP-ish). This
            captures the original pressing before deluxe/anniversary bloat.
          - Fall back to the shortest available release if no sub-90-min one exists.

        Weakness: if the release-group ITSELF is a deluxe-only compilation (rare but
        happens), we can't help. The caller treats 0 as "MB couldn't clarify".
        """
        if not self.enabled or not self._ready or not artist or not album:
            return 0

        key = f"canonlen|{artist.lower()}|{album.lower()}|{year[:4] if year else ''}"
        cached = self._cache_get(key)
        if cached is not None:
            try:
                return int(cached)
            except (TypeError, ValueError):
                return 0

        query = f'artist:"{_escape(artist)}" AND release:"{_escape(album)}"'
        try:
            res = self._mb.search_release_groups(query=query, limit=5)
        except Exception:
            self._cache_put(key, 0)
            return 0

        groups = res.get("release-group-list", []) if isinstance(res, dict) else []
        best_group = None
        best_score = -1
        want_year = year[:4] if year else ""
        for g in groups:
            try:
                score = int(g.get("ext:score", 0))
            except (TypeError, ValueError):
                score = 0
            if want_year:
                first_release = g.get("first-release-date", "") or ""
                if first_release.startswith(want_year):
                    score += 5
            if score > best_score:
                best_group = g
                best_score = score

        if not best_group or best_score < 80:
            self._cache_put(key, 0)
            return 0

        try:
            rel_res = self._mb.browse_releases(
                release_group=best_group["id"],
                includes=["media", "recordings"],
                limit=25,
            )
            releases = rel_res.get("release-list", []) or []
        except Exception:
            self._cache_put(key, 0)
            return 0

        sized: list[tuple[int, str]] = []
        for r in releases:
            total_ms = 0
            for m in r.get("medium-list") or r.get("media") or []:
                for t in m.get("track-list") or m.get("tracks") or []:
                    length = t.get("length") or t.get("recording", {}).get("length")
                    try:
                        total_ms += int(length) if length is not None else 0
                    except (TypeError, ValueError):
                        continue
            sec = total_ms // 1000
            if sec > 0:
                sized.append((sec, r.get("date", "") or ""))

        if not sized:
            self._cache_put(key, 0)
            return 0

        # Prefer releases <=90 minutes (a plausible "original LP" length). Within
        # that pool, pick the earliest by release date (tiebreak: shortest).
        LP_MAX = 90 * 60
        short_pool = [s for s in sized if s[0] <= LP_MAX]
        if short_pool:
            short_pool.sort(key=lambda x: (x[1] or "9999", x[0]))
            chosen = short_pool[0][0]
        else:
            sized.sort(key=lambda x: x[0])
            chosen = sized[0][0]

        self._cache_put(key, chosen)
        return chosen

    def search_albums_by_genre(
        self,
        genre: str,
        max_duration_sec: int,
        min_duration_sec: int = 0,
        limit: int = 40,
    ) -> list[dict[str, Any]]:
        """Search release-groups of type=Album tagged with the genre, then resolve to releases
        whose length falls in [min_duration_sec, max_duration_sec].
        """
        if not self.enabled or not self._ready or not genre:
            return []

        key = f"genre={genre.lower()}|max={max_duration_sec}|min={min_duration_sec}|limit={limit}"
        cached = self._cache_get(key)
        if cached is not None:
            return list(cached)

        results: list[dict[str, Any]] = []
        try:
            rg = self._mb.search_release_groups(
                query=f'tag:"{genre}" AND primarytype:Album',
                limit=limit,
            )
        except Exception:
            self._cache_put(key, [])
            return []

        release_groups = rg.get("release-group-list", []) if isinstance(rg, dict) else []
        for item in release_groups[:limit]:
            try:
                # Always browse_releases with media+recordings to fetch per-track lengths.
                # "media" alone returns empty track-lists; "recordings" is what provides
                # the actual track length data via medium-list[].track-list[].length.
                try:
                    rel_res = self._mb.browse_releases(
                        release_group=item["id"], includes=["media", "recordings"], limit=3
                    )
                    releases = rel_res.get("release-list", [])
                except Exception:
                    releases = []
                total_sec = _best_release_length(releases)
                if total_sec <= 0:
                    continue
                if total_sec > max_duration_sec:
                    continue
                if total_sec < min_duration_sec:
                    continue
                artist = ""
                credits = item.get("artist-credit", [])
                if isinstance(credits, list) and credits:
                    for c in credits:
                        if isinstance(c, dict):
                            artist = c.get("name") or c.get("artist", {}).get("name", "")
                            if artist:
                                break
                title = item.get("title", "")
                mbid = item.get("id", "")
                url = f"https://musicbrainz.org/release-group/{mbid}" if mbid else ""
                results.append({
                    "artist": artist,
                    "title": title,
                    "duration_sec": total_sec,
                    "genre": genre,
                    "url": url,
                    "mbid": mbid,
                })
            except Exception:
                continue

        self._cache_put(key, results)
        return results


import re as _re

# MB tag-list often includes non-genre meta tags (charts, personal lists, years, languages...).
# This is a coarse filter to drop obvious non-genre noise without being too aggressive.
_NON_GENRE_PATTERNS = [
    _re.compile(r"\d"),  # any tag containing a digit (e.g. "5+ wochen", "top 2013")
    _re.compile(r"^seen live$", _re.IGNORECASE),
    _re.compile(r"^favou?rites?$", _re.IGNORECASE),
    _re.compile(r"^owned$", _re.IGNORECASE),
    _re.compile(r"^to listen$", _re.IGNORECASE),
    _re.compile(r"^wochen$", _re.IGNORECASE),
]


def _looks_like_genre(name: str) -> bool:
    n = name.strip()
    if not n or len(n) > 50:
        return False
    for pat in _NON_GENRE_PATTERNS:
        if pat.search(n):
            return False
    return True


def _escape(s: str) -> str:
    """Escape Lucene-ish special characters for MusicBrainz queries."""
    specials = r'+-&|!(){}[]^"~*?:\\/ '
    out: list[str] = []
    for ch in s:
        if ch in specials:
            out.append("\\" + ch)
        else:
            out.append(ch)
    return "".join(out).strip()


def _best_release_length(releases: list[dict[str, Any]]) -> int:
    """Pick a reasonable total length (seconds) from a list of MB release dicts.

    Many release dicts don't include media lengths unless we browse with includes=['media'].
    Falls back to 0 when unknown.
    """
    best = 0
    for r in releases:
        media = r.get("medium-list") or r.get("media") or []
        total_ms = 0
        for m in media:
            tracks = m.get("track-list") or m.get("tracks") or []
            for t in tracks:
                length = t.get("length") or t.get("recording", {}).get("length")
                try:
                    total_ms += int(length) if length is not None else 0
                except (TypeError, ValueError):
                    continue
        if total_ms > best:
            best = total_ms
    return best // 1000
