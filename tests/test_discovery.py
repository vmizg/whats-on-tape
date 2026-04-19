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
