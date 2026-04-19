"""Minimal Last.fm client using the REST+JSON interface via urllib (no new deps).

Used only for genre enrichment when an API key is provided.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

CACHE_TTL_SEC = 30 * 24 * 60 * 60  # 30 days
USER_AGENT = "MusicTapePlanner/0.1 (+https://github.com/local/music-tape-planner)"
BASE_URL = "https://ws.audioscrobbler.com/2.0/"
MIN_INTERVAL_SEC = 0.25  # ~4 req/sec; Last.fm allows higher but we're polite


class LastFmClient:
    def __init__(self, api_key: str | None, cache_path: Path):
        self.api_key = (api_key or "").strip()
        self.cache_path = cache_path
        self.enabled = bool(self.api_key)
        self._cache = self._load_cache()
        self._last_request_ts = 0.0

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
        if time.time() - entry.get("ts", 0) > CACHE_TTL_SEC:
            return None
        return entry.get("value")

    def _cache_put(self, key: str, value: Any) -> None:
        self._cache[key] = {"ts": time.time(), "value": value}
        self._save_cache()

    def _throttle(self) -> None:
        elapsed = time.time() - self._last_request_ts
        if elapsed < MIN_INTERVAL_SEC:
            time.sleep(MIN_INTERVAL_SEC - elapsed)
        self._last_request_ts = time.time()

    def _call(self, method: str, **params: str) -> dict[str, Any]:
        qp = {"method": method, "api_key": self.api_key, "format": "json", **params}
        url = BASE_URL + "?" + urlencode(qp)
        self._throttle()
        req = Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urlopen(req, timeout=20) as resp:
                data = resp.read()
            parsed = json.loads(data.decode("utf-8"))
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}

    def genres_for_album(self, artist: str, album: str) -> list[str]:
        """Return top tags for an album via album.getInfo. Empty list on miss."""
        if not self.enabled or not artist or not album:
            return []
        key = f"album|{artist.lower()}|{album.lower()}"
        cached = self._cache_get(key)
        if cached is not None:
            return list(cached)

        res = self._call("album.getInfo", artist=artist, album=album, autocorrect="1")
        tags_raw = _extract_tag_list(res.get("album", {}) if isinstance(res, dict) else {})
        names: list[str] = []
        for t in tags_raw:
            if isinstance(t, dict):
                name = t.get("name", "")
                if name and name not in names:
                    names.append(name)

        top = names[:5]
        self._cache_put(key, top)
        return top

    def album_duration_seconds(self, artist: str, album: str) -> int:
        """Return album duration in seconds by summing track lengths from album.getInfo.

        Last.fm's `tracks.track[].duration` is given in seconds (unlike MusicBrainz, which uses ms).
        Returns 0 if unavailable.
        """
        if not self.enabled or not artist or not album:
            return 0
        key = f"duration|{artist.lower()}|{album.lower()}"
        cached = self._cache_get(key)
        if cached is not None:
            try:
                return int(cached)
            except (TypeError, ValueError):
                return 0
        res = self._call("album.getInfo", artist=artist, album=album, autocorrect="1")
        album_block = res.get("album", {}) if isinstance(res, dict) else {}
        tracks_holder = album_block.get("tracks", {})
        tracks: list[dict[str, Any]] = []
        if isinstance(tracks_holder, dict):
            raw = tracks_holder.get("track", []) or []
            if isinstance(raw, list):
                tracks = [t for t in raw if isinstance(t, dict)]
            elif isinstance(raw, dict):
                tracks = [raw]

        total = 0
        for t in tracks:
            dur = t.get("duration", 0)
            try:
                total += int(dur) if dur not in (None, "") else 0
            except (TypeError, ValueError):
                continue
        self._cache_put(key, total)
        return total

    def search_albums_by_genre(
        self,
        genre: str,
        max_duration_sec: int,
        min_duration_sec: int = 0,
        limit: int = 40,
    ) -> list[dict[str, Any]]:
        """Search Last.fm tag top-albums, then resolve duration via album.getInfo.

        Returns filtered list of {artist, title, duration_sec, genre, url}.
        Only up to the first 50 tag candidates are resolved (each resolution is a second HTTP
        call); we stop early once we have enough matches.
        """
        if not self.enabled or not genre:
            return []

        key = f"tagalbums|{genre.lower()}|max={max_duration_sec}|min={min_duration_sec}|limit={limit}"
        cached = self._cache_get(key)
        if cached is not None:
            return list(cached)

        res = self._call("tag.getTopAlbums", tag=genre, limit=str(limit))
        top = res.get("albums", {}) if isinstance(res, dict) else {}
        raw_albums = top.get("album", []) if isinstance(top, dict) else []
        if isinstance(raw_albums, dict):
            raw_albums = [raw_albums]

        results: list[dict[str, Any]] = []
        for item in raw_albums:
            if not isinstance(item, dict):
                continue
            artist_info = item.get("artist", {}) or {}
            artist_name = (
                artist_info.get("name", "") if isinstance(artist_info, dict) else str(artist_info)
            )
            title = item.get("name", "")
            url = item.get("url", "")
            if not artist_name or not title:
                continue

            total_sec = self.album_duration_seconds(artist_name, title)
            if total_sec <= 0:
                continue
            if total_sec > max_duration_sec:
                continue
            if total_sec < min_duration_sec:
                continue

            results.append({
                "artist": artist_name,
                "title": title,
                "duration_sec": total_sec,
                "genre": genre,
                "url": url,
            })
            if len(results) >= 10:
                break

        self._cache_put(key, results)
        return results


def _extract_tag_list(album_block: dict[str, Any]) -> list[dict[str, Any]]:
    tag_holder = album_block.get("tags", {})
    if isinstance(tag_holder, dict):
        raw = tag_holder.get("tag", []) or []
    elif isinstance(tag_holder, list):
        raw = tag_holder
    else:
        raw = []
    if isinstance(raw, dict):
        return [raw]
    if isinstance(raw, list):
        return [x for x in raw if isinstance(x, dict)]
    return []
