from pathlib import Path

from src.discovery import is_disc_folder_name, walk_library


def test_is_disc_folder_name():
    assert is_disc_folder_name("CD1")
    assert is_disc_folder_name("CD 2")
    assert is_disc_folder_name("Disc 3")
    assert is_disc_folder_name("disk-4")
    assert is_disc_folder_name("LP1")
    assert is_disc_folder_name("LP 2")
    assert is_disc_folder_name("Vinyl-1")
    assert is_disc_folder_name("Side 2")
    assert is_disc_folder_name("CD1 - (CD) Remastered by James Guthrie (1973, 2011)")
    assert not is_disc_folder_name("Bonus")
    assert not is_disc_folder_name("CD no number")
    assert not is_disc_folder_name("LP sleeve")


def test_walk_library_simple(tmp_path: Path):
    # layout:
    # root/
    #   Artist - AlbumA (2000) [CD]/track1.flac
    #   Artist - AlbumB (2001) [CD]/CD1/track1.flac
    #                                CD2/track1.flac
    #   # Clips/ignored.flac
    root = tmp_path
    a = root / "Artist - AlbumA (2000) [CD]"
    a.mkdir()
    (a / "01.flac").write_bytes(b"")
    b = root / "Artist - AlbumB (2001) [CD]"
    (b / "CD1").mkdir(parents=True)
    (b / "CD2").mkdir(parents=True)
    (b / "CD1" / "01.flac").write_bytes(b"")
    (b / "CD2" / "01.flac").write_bytes(b"")
    skip = root / "# Clips"
    skip.mkdir()
    (skip / "ignored.flac").write_bytes(b"")

    folders = walk_library(root)
    names = sorted(f.root.name for f in folders)
    assert names == ["Artist - AlbumA (2000) [CD]", "Artist - AlbumB (2001) [CD]"]

    by_name = {f.root.name: f for f in folders}
    assert not by_name["Artist - AlbumA (2000) [CD]"].is_multi_disc
    assert by_name["Artist - AlbumB (2001) [CD]"].is_multi_disc
    assert len(by_name["Artist - AlbumB (2001) [CD]"].audio_files) == 2


def test_walk_library_lp_discs(tmp_path: Path):
    # 2-LP vinyl rip: disc folders named LP1/LP2 must be treated as one multi-disc album,
    # not two standalone albums.
    root = tmp_path
    album = root / "Foje - Kitoks Pasaulis (1992) [LP 32-192]"
    (album / "LP1").mkdir(parents=True)
    (album / "LP2").mkdir(parents=True)
    (album / "LP1" / "01.flac").write_bytes(b"")
    (album / "LP1" / "02.flac").write_bytes(b"")
    (album / "LP2" / "01.flac").write_bytes(b"")

    folders = walk_library(root)
    assert len(folders) == 1
    f = folders[0]
    assert f.root.name == "Foje - Kitoks Pasaulis (1992) [LP 32-192]"
    assert f.is_multi_disc
    assert len(f.audio_files) == 3


def test_walk_library_disc_with_nested_video_ts(tmp_path: Path):
    # Pink Floyd Immersion Box Set shape: CD1..CD3 have direct audio; CD4 has no
    # direct audio but contains a VIDEO_TS subfolder full of .vob files. The whole
    # thing should still be recognized as a single multi-disc album.
    root = tmp_path
    album = root / "Pink Floyd - DSOTM (Immersion Box) (1972-1974, 2011) [CD]"
    for n in (1, 2, 3):
        d = album / f"CD{n} - (CD) Stuff (1973, 2011)"
        d.mkdir(parents=True)
        (d / "01.flac").write_bytes(b"")
    cd4 = album / "CD4 - (DVD) Audio-Visual Material"
    (cd4 / "VIDEO_TS").mkdir(parents=True)
    (cd4 / "VIDEO_TS" / "VTS_01_1.VOB").write_bytes(b"")
    (cd4 / "VIDEO_TS" / "VTS_01_2.VOB").write_bytes(b"")

    folders = walk_library(root)
    assert len(folders) == 1
    f = folders[0]
    assert f.is_multi_disc
    assert len(f.disc_folders) == 4
    assert len(f.audio_files) == 5


def test_walk_library_box_set_with_artwork_and_missing_disc(tmp_path: Path):
    # Real-world Pink Floyd Immersion Box Set shape: CD1..CD6 discs alongside an
    # `Artwork/` helper folder, and one disc folder that is just a "(missing)"
    # placeholder with no files. The box must still be recognized as one album.
    root = tmp_path
    album = root / "Pink Floyd - DSOTM (Immersion Box) (1972-1974, 2011) [CD]"
    for n in (1, 2, 6):
        d = album / f"CD{n} - (CD) Disc {n}"
        d.mkdir(parents=True)
        (d / "01.flac").write_bytes(b"")
    cd4 = album / "CD4 - (DVD) Video"
    (cd4 / "VIDEO_TS").mkdir(parents=True)
    (cd4 / "VIDEO_TS" / "VTS_01_1.VOB").write_bytes(b"")
    # CD5 is the "(missing)" placeholder: disc-named, no audio anywhere.
    (album / "CD5 - (BR) High Resolution (missing)").mkdir()
    # Non-disc helper folder with images only.
    artwork = album / "Artwork"
    artwork.mkdir()
    (artwork / "front.jpg").write_bytes(b"")
    (artwork / "booklet.pdf").write_bytes(b"")

    folders = walk_library(root)
    assert len(folders) == 1
    f = folders[0]
    assert f.is_multi_disc
    assert len(f.disc_folders) == 4
    assert len(f.audio_files) == 4


def test_walk_library_rejects_container_when_extras_hold_audio(tmp_path: Path):
    # If a non-disc subfolder actually holds audio (e.g. `Bonus/*.flac`), we do NOT
    # treat the parent as a multi-disc container (we'd silently drop that audio).
    root = tmp_path
    album = root / "Artist - Album (2020) [CD]"
    cd1 = album / "CD1"
    cd1.mkdir(parents=True)
    (cd1 / "01.flac").write_bytes(b"")
    bonus = album / "Bonus"
    bonus.mkdir()
    (bonus / "bonus.flac").write_bytes(b"")

    folders = walk_library(root)
    # The container should NOT be treated as a single multi-disc album.
    container_as_album = [f for f in folders if f.root == album and f.is_multi_disc]
    assert container_as_album == []


def test_walk_library_disc_with_ambiguous_nested_audio(tmp_path: Path):
    # A disc folder with two audio-bearing subfolders is ambiguous: we don't try to
    # guess which one is "the disc", so we refuse to treat the parent as a
    # multi-disc container (otherwise we'd silently drop CD1's audio).
    root = tmp_path
    album = root / "Artist - Album (2020) [CD]"
    cd1 = album / "CD1"
    cd1.mkdir(parents=True)
    (cd1 / "a").mkdir()
    (cd1 / "b").mkdir()
    (cd1 / "a" / "01.flac").write_bytes(b"")
    (cd1 / "b" / "01.flac").write_bytes(b"")
    cd2 = album / "CD2"
    cd2.mkdir()
    (cd2 / "01.flac").write_bytes(b"")

    folders = walk_library(root)
    assert not any(f.root == album and f.is_multi_disc for f in folders)


def test_walk_library_skip_dirs_override_replaces_defaults(tmp_path: Path):
    """When the caller passes `skip_dirs`, it FULLY replaces the built-in
    list. So '# Clips' is no longer skipped if the override omits it, and a
    new pattern like '## experiments*' gets skipped."""
    root = tmp_path
    a = root / "Artist - Album (2000) [CD]"
    a.mkdir()
    (a / "01.flac").write_bytes(b"")
    clips = root / "# Clips"
    clips.mkdir()
    (clips / "loop.flac").write_bytes(b"")
    experiments = root / "## experiments"
    experiments.mkdir()
    (experiments / "rough.flac").write_bytes(b"")

    folders = walk_library(root, skip_dirs=("## experiments*",))
    names = sorted(f.root.name for f in folders)
    assert "# Clips" in names
    assert "## experiments" not in names

    # Empty tuple disables skipping entirely.
    folders_all = walk_library(root, skip_dirs=())
    names_all = sorted(f.root.name for f in folders_all)
    assert "# Clips" in names_all
    assert "## experiments" in names_all

    # None (the default) keeps the built-in SKIP_DIRS, so '# Clips' is filtered.
    folders_default = walk_library(root)
    names_default = sorted(f.root.name for f in folders_default)
    assert "# Clips" not in names_default


def test_walk_library_skip_dirs_glob_matches_basename_at_any_depth(tmp_path: Path):
    """Bare patterns (no '/') match folder basenames at any depth, so a nested
    'Demos'-suffixed folder under a genre tree gets pruned even though the
    default `SKIP_DIRS` only catches top-level '# clips*'-style folders."""
    root = tmp_path
    (root / "Rock" / "Artist - Album (2000) [CD]").mkdir(parents=True)
    (root / "Rock" / "Artist - Album (2000) [CD]" / "01.flac").write_bytes(b"")
    (root / "Rock" / "Artist - Demos").mkdir()
    (root / "Rock" / "Artist - Demos" / "01.flac").write_bytes(b"")

    folders = walk_library(root, skip_dirs=("* - demos",))
    names = sorted(f.root.name for f in folders)
    assert "Artist - Album (2000) [CD]" in names
    assert "Artist - Demos" not in names


def test_walk_library_skip_dirs_glob_anchored_path(tmp_path: Path):
    """A pattern with '/' matches the full relative path, so we can scope a
    prune to one subtree only (e.g. 'Jazz/**/Sketches' hits only Jazz sketches,
    not Rock sketches)."""
    root = tmp_path
    jazz_sketches = root / "Jazz" / "Sketches"
    jazz_sketches.mkdir(parents=True)
    (jazz_sketches / "01.flac").write_bytes(b"")
    rock_sketches = root / "Rock" / "Sketches"
    rock_sketches.mkdir(parents=True)
    (rock_sketches / "01.flac").write_bytes(b"")

    folders = walk_library(root, skip_dirs=("jazz/sketches",))
    roots = {f.root for f in folders}
    assert jazz_sketches not in roots
    assert rock_sketches in roots


def test_walk_library_skip_dirs_glob_is_case_insensitive(tmp_path: Path):
    """Matching is case-insensitive regardless of how the user spells the
    pattern or the folder."""
    root = tmp_path
    loud = root / "LOUD CLIPS"
    loud.mkdir()
    (loud / "01.flac").write_bytes(b"")
    ok = root / "Artist - Album (2000) [CD]"
    ok.mkdir()
    (ok / "01.flac").write_bytes(b"")

    folders = walk_library(root, skip_dirs=("loud *",))
    names = sorted(f.root.name for f in folders)
    assert "LOUD CLIPS" not in names
    assert "Artist - Album (2000) [CD]" in names


