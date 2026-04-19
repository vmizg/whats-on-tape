"""High-level scan pipeline: discover albums, extract metadata + duration, cache, emit outputs."""
from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .discovery import AlbumFolder, walk_library
from .durations import album_duration, ffprobe_available
from .enrich import build_clients, enrich_albums
from .models import Album
from .report import write_albums_json, write_scan_report
from .tags import parse_leaf_folder_name, read_tags_from_files

CACHE_VERSION = 1


def _cache_key(folder: AlbumFolder) -> dict[str, Any]:
    """Stable cache key: album root, plus (name, mtime, size) of each audio file."""
    entries = []
    for f in folder.audio_files:
        try:
            st = f.stat()
            entries.append({"name": f.name, "mtime": int(st.st_mtime), "size": st.st_size})
        except OSError:
            entries.append({"name": f.name, "mtime": 0, "size": 0})
    return {"root": str(folder.root), "files": entries, "multi_disc": folder.is_multi_disc}


def _cache_fingerprint(key: dict[str, Any]) -> str:
    return json.dumps(key, sort_keys=True, ensure_ascii=False)


def _load_cache(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict) or data.get("version") != CACHE_VERSION:
        return {}
    entries = data.get("entries", {})
    return entries if isinstance(entries, dict) else {}


def _save_cache(path: Path, entries: dict[str, dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"version": CACHE_VERSION, "entries": entries}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _primary_format(folder: AlbumFolder) -> str:
    exts: dict[str, int] = {}
    for f in folder.audio_files:
        e = f.suffix.lower().lstrip(".")
        exts[e] = exts.get(e, 0) + 1
    if not exts:
        return ""
    return max(exts.items(), key=lambda kv: kv[1])[0]


def _build_album(folder: AlbumFolder, library_root: Path | None = None) -> Album:
    warnings: list[str] = []
    parsed = parse_leaf_folder_name(folder.root.name)

    artist = parsed.artist if parsed else ""
    album_name = parsed.album if parsed else ""
    year = parsed.year if parsed else ""
    source = parsed.source if parsed else ""

    tags = read_tags_from_files(folder.audio_files)
    # Track whether each field ended up with a trustworthy value. A "good" source is
    # the folder-name parser or audio-file tags; a weak fallback (parent folder name
    # or the leaf folder name itself) only produces a warning if nothing better was
    # available.
    artist_from_good_source = bool(artist)
    album_from_good_source = bool(album_name)

    if not artist:
        tag_artist = (tags.get("albumartist") or tags.get("artist") or "").strip()
        if tag_artist:
            artist = tag_artist
            artist_from_good_source = True
        else:
            parent_name = folder.root.parent.name
            # Don't use the library root itself (e.g. "Music") as an artist fallback:
            # it isn't an artist, it's just where the album happens to sit.
            if library_root is not None and folder.root.parent.resolve() == library_root.resolve():
                artist = ""
            else:
                artist = parent_name
    if not album_name:
        tag_album = (tags.get("album") or "").strip()
        if tag_album:
            album_name = tag_album
            album_from_good_source = True
        else:
            album_name = folder.root.name
    if not year:
        year = (tags.get("year") or "").strip()

    # Only warn about the folder-name pattern when it actually hurt us: the parse
    # failed AND we had to guess at artist or album from a weak fallback. If tags
    # or the parser filled in both cleanly, the user doesn't care how we got there.
    if parsed is None and not (artist_from_good_source and album_from_good_source):
        warnings.append("folder name did not match expected 'Artist - Album (Year) [Source]' pattern")

    duration, dur_warn = album_duration(folder.audio_files)
    warnings.extend(dur_warn)

    genres_val = tags.get("genres") or []
    genres: list[str] = list(genres_val) if isinstance(genres_val, list) else []

    return Album(
        path=str(folder.root),
        artist=artist,
        album=album_name,
        year=year,
        source=source,
        duration_sec=duration,
        track_count=len(folder.audio_files),
        genres=genres,
        format=_primary_format(folder),
        warnings=warnings,
    )


def scan_library(
    root: Path,
    out_dir: Path,
    progress: bool = True,
    workers: int | None = None,
    enrich: bool = True,
    lastfm_key: str | None = None,
) -> list[Album]:
    out_dir.mkdir(parents=True, exist_ok=True)
    cache_path = out_dir / ".scan-cache.json"
    cache = _load_cache(cache_path)
    fresh_cache: dict[str, dict[str, Any]] = {}

    folders = walk_library(root)

    if not ffprobe_available():
        print("WARNING: ffprobe not found on PATH; DFF/ISO/VOB durations may be missing. Install ffmpeg.")

    # Split into cached-hit / to-probe up front so the cache lookup is fast and we only
    # parallelize the expensive path (duration + tag I/O).
    albums: list[Album] = []
    to_probe: list[tuple[AlbumFolder, str]] = []
    for folder in folders:
        key_str = _cache_fingerprint(_cache_key(folder))
        cached = cache.get(str(folder.root))
        if cached and cached.get("key") == key_str and cached.get("album"):
            try:
                album = Album.from_dict(cached["album"])
                albums.append(album)
                fresh_cache[str(folder.root)] = cached
                continue
            except (KeyError, TypeError, ValueError):
                pass
        to_probe.append((folder, key_str))

    # Thread pool: I/O-bound (disk reads + occasional ffprobe subprocess), so threads fit.
    if workers is None:
        workers = min(16, (os.cpu_count() or 4) * 2)

    pbar = None
    if progress and to_probe:
        try:
            from tqdm import tqdm  # type: ignore
            pbar = tqdm(total=len(to_probe), unit="album", desc="Scanning")
        except Exception:
            pbar = None

    if to_probe:
        if workers <= 1:
            for folder, key_str in to_probe:
                album = _build_album(folder, library_root=root)
                albums.append(album)
                fresh_cache[str(folder.root)] = {"key": key_str, "album": asdict(album)}
                if pbar is not None:
                    pbar.update(1)
        else:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = {pool.submit(_build_album, folder, root): (folder, key_str) for folder, key_str in to_probe}
                for fut in as_completed(futures):
                    folder, key_str = futures[fut]
                    try:
                        album = fut.result()
                    except Exception as exc:  # pragma: no cover - defensive
                        album = Album(path=str(folder.root), artist="", album=folder.root.name, warnings=[f"scan failed: {exc}"])
                    albums.append(album)
                    fresh_cache[str(folder.root)] = {"key": key_str, "album": asdict(album)}
                    if pbar is not None:
                        pbar.update(1)

    if pbar is not None:
        pbar.close()

    albums.sort(key=lambda a: (a.artist.lower(), a.album.lower(), a.path.lower()))

    # Genre enrichment for albums that still lack a GENRE tag.
    if enrich:
        mb_client, lastfm_client, wiki_client = build_clients(
            out_dir, enable_mb=True, lastfm_key=lastfm_key, enable_wiki=True
        )
        summary = enrich_albums(
            albums, mb=mb_client, lastfm=lastfm_client, wiki=wiki_client, progress=progress
        )
        if summary["candidates"]:
            enriched_total = (
                summary["enriched_mb"] + summary["enriched_lastfm"] + summary["enriched_wiki"]
            )
            clarified_total = (
                summary["clarified_mb"] + summary["clarified_lastfm"] + summary["clarified_wiki"]
            )
            print(
                f"Enrichment: {enriched_total} filled "
                f"(MB={summary['enriched_mb']}, Last.fm={summary['enriched_lastfm']}, "
                f"Wiki={summary['enriched_wiki']}; {summary['still_empty']} still empty "
                f"of {summary['candidates_empty']} empty candidates); "
                f"{clarified_total} clarified "
                f"(MB={summary['clarified_mb']}, Last.fm={summary['clarified_lastfm']}, "
                f"Wiki={summary['clarified_wiki']}; {summary['still_vague']} still vague "
                f"of {summary['candidates_vague']} vague candidates)."
            )
        # Update cache entries for enriched albums so the next scan keeps the enrichment.
        for a in albums:
            entry = fresh_cache.get(a.path)
            if entry is not None:
                entry["album"] = asdict(a)

    _save_cache(cache_path, fresh_cache)

    write_albums_json(out_dir / "albums.json", albums)
    write_scan_report(out_dir / "report.md", albums)
    return albums
