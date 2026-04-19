"""Tests for the warning-emission logic in scan._build_album.

We exercise the function directly with stubbed tag/duration lookups so we don't
need real audio files; the interesting behavior is which warnings (if any) end
up attached to the resulting Album record.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from src import scan
from src.discovery import AlbumFolder


@pytest.fixture
def stub_io(monkeypatch: pytest.MonkeyPatch):
    """Stub out the two external side-effects of _build_album."""
    tag_result: dict[str, object] = {}

    def set_tags(**kwargs: object) -> None:
        tag_result.clear()
        tag_result.update(kwargs)

    monkeypatch.setattr(scan, "read_tags_from_files", lambda _files: dict(tag_result))
    monkeypatch.setattr(scan, "album_duration", lambda _files: (0, []))
    return set_tags


def _folder(tmp_path: Path, name: str, tracks: int = 1) -> AlbumFolder:
    root = tmp_path / name
    root.mkdir(parents=True, exist_ok=True)
    files = []
    for i in range(tracks):
        f = root / f"{i + 1:02d}.flac"
        f.write_bytes(b"")
        files.append(f)
    return AlbumFolder(root=root, audio_files=files, is_multi_disc=False, disc_folders=[])


def test_no_warning_when_folder_parses_cleanly(tmp_path: Path, stub_io) -> None:
    stub_io()
    f = _folder(tmp_path, "Artist - Album (2020) [CD]")
    a = scan._build_album(f, library_root=tmp_path)
    assert a.artist == "Artist"
    assert a.album == "Album"
    assert a.warnings == []


def test_no_warning_when_tags_supply_missing_artist_and_album(tmp_path: Path, stub_io) -> None:
    # The folder name doesn't match the expected pattern ("no hyphen"), but the
    # audio files have proper ALBUMARTIST and ALBUM tags, so we know who made
    # this and what it's called. No warning should fire.
    stub_io(albumartist="Wendy Carlos", album="Clockwork Orange Soundtrack", year="1972")
    f = _folder(tmp_path, "Clockwork Orange Soundtrack (1972) [MP3]")
    a = scan._build_album(f, library_root=tmp_path)
    assert a.artist == "Wendy Carlos"
    assert a.album == "Clockwork Orange Soundtrack"
    assert a.warnings == []


def test_warning_when_parse_fails_and_artist_falls_back_to_parent(tmp_path: Path, stub_io) -> None:
    # No tags, folder name doesn't parse, and the folder sits under an
    # artist-named parent. We'll take "Some Artist" as the artist, but that is
    # a guess based on folder structure - the user should know.
    stub_io()
    parent = tmp_path / "Some Artist"
    parent.mkdir()
    f = _folder(parent, "Album Without Year")
    a = scan._build_album(f, library_root=tmp_path)
    assert a.artist == "Some Artist"  # fallback from parent folder
    assert a.album == "Album Without Year"  # fallback from folder name itself
    assert any("did not match expected" in w for w in a.warnings)


def test_warning_when_at_library_root_and_tags_missing(tmp_path: Path, stub_io) -> None:
    # Folder name doesn't parse, no tags, AND it sits directly under the
    # library root so we refuse to synthesize an artist from "Music".
    stub_io()
    f = _folder(tmp_path, "Mystery Album (1999) [Tape]")
    a = scan._build_album(f, library_root=tmp_path)
    assert a.artist == ""
    assert any("did not match expected" in w for w in a.warnings)


def test_no_warning_when_tags_rescue_album_at_library_root(tmp_path: Path, stub_io) -> None:
    # Folder name doesn't parse, folder sits directly under the library root,
    # but tags supply both artist and album - no warning needed.
    stub_io(albumartist="Tigro Metai", album="Kardiofonas", year="1987")
    f = _folder(tmp_path, "Tigro Metai, Kardiofonas (1987) [Tape]")
    a = scan._build_album(f, library_root=tmp_path)
    assert a.artist == "Tigro Metai"
    assert a.album == "Kardiofonas"
    assert a.warnings == []
