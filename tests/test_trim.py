"""Tests for src/trim.py and the planner's trim integration."""
from __future__ import annotations

from dataclasses import replace

import pytest

from src.models import Album
from src.planner import PlannerConfig, plan_tapes
from src.trim import (
    TrimResult,
    _is_bonus_title,
    compute_trim,
    is_compilation_title,
    is_reissue_title,
)


# ---------------------------------------------------------------------------
# Classifier tests
# ---------------------------------------------------------------------------

class TestReissueDetection:
    @pytest.mark.parametrize("title", [
        "The Dark Side Of The Moon (Immersion Box Set)",
        "Abbey Road (Super Deluxe Edition)",
        "L.A. Woman (50th Anniversary Deluxe Edition)",
        "Kim Wilde Select (Expanded & Remastered)",
        "Stadium Arcadium",  # actually NOT a reissue per title; should be False
    ])
    def test_reissue_keyword_detection(self, title):
        # Parametrize isn't quite right for this mixed-bag test; use the non-reissue
        # as a separate assertion.
        if title == "Stadium Arcadium":
            assert not is_reissue_title(title)
        else:
            assert is_reissue_title(title)

    def test_reissue_empty_title(self):
        assert not is_reissue_title("")


class TestCompilationDetection:
    @pytest.mark.parametrize("title", [
        "The Essential Michael Jackson",
        "Gold (Anniversary Edition)",  # "Gold" is a compilation title but no compilation keyword
        "Greatest Hits",
        "The Very Best of The Clash",
        "Singles",
        "Live at Budokan",
        "Pulse (Live)",
        "Tron Legacy (Original Motion Picture Soundtrack)",
    ])
    def test_compilation_keywords(self, title):
        expected_comp = title not in {"Gold (Anniversary Edition)"}
        assert is_compilation_title(title) == expected_comp

    def test_plain_studio_album_is_not_compilation(self):
        assert not is_compilation_title("Dark Side of the Moon")
        assert not is_compilation_title("Abbey Road")


# ---------------------------------------------------------------------------
# Track-title heuristic tests
# ---------------------------------------------------------------------------

class TestBonusTrackDetection:
    @pytest.mark.parametrize("title,expected", [
        ("Money (Demo)", True),
        ("Money [Demo]", True),
        ("Money (Alternate Mix)", True),
        ("Money (2009 Remaster)", True),
        ("Money (Live)", True),
        ("Money (Bonus Track)", True),
        ("Money (Early Version)", True),
        ("Money - Demo", True),
        ("Money - Live", True),
        ("Money (Previously Unreleased)", True),
        ("Money", False),
        ("Money (instrumental break)", False),  # not surrounded; "break" isn't a marker
        ("The Great Gig in the Sky", False),
        ("Interstellar Overdrive", False),
    ])
    def test_bonus_annotations(self, title, expected):
        assert _is_bonus_title(title) == expected


# ---------------------------------------------------------------------------
# compute_trim() tests
# ---------------------------------------------------------------------------

def _album(path: str, artist: str, album: str, duration_min: int) -> Album:
    return Album(
        path=path, artist=artist, album=album,
        duration_sec=duration_min * 60,
    )


class TestComputeTrim:
    def test_compilation_is_refused(self, tmp_path):
        a = _album(str(tmp_path), "VA", "The Essential Michael Jackson", 140)
        result = compute_trim(a, max_tape_sec=120 * 60, mb=None)
        assert result.method == "none"
        assert result.refused_reason == "compilation/live"
        assert "manual 2-sided split" in result.note

    def test_non_existent_folder_and_no_mb_falls_back_cleanly(self, tmp_path):
        a = _album(str(tmp_path / "does_not_exist"), "X", "Some Deluxe Edition", 140)
        result = compute_trim(a, max_tape_sec=120 * 60, mb=None)
        assert result.method == "none"
        assert result.trimmed_duration_sec == a.duration_sec

    def test_mb_canonical_duration_used_when_shorter(self, tmp_path):
        """When MB returns a shorter canonical release that fits, use it."""
        class FakeMB:
            enabled = True
            _ready = True
            def canonical_release_length_sec(self, artist, album, year=""):
                return 44 * 60  # 44 minutes, well under tape max

        a = _album(str(tmp_path), "The Beatles", "Abbey Road (Super Deluxe Edition)", 135)
        result = compute_trim(a, max_tape_sec=120 * 60, mb=FakeMB())
        assert result.method == "mb"
        assert result.trimmed_duration_sec == 44 * 60
        assert "MusicBrainz canonical" in result.note

    def test_mb_result_that_is_not_shorter_is_ignored(self, tmp_path):
        class FakeMB:
            enabled = True
            _ready = True
            def canonical_release_length_sec(self, artist, album, year=""):
                return 134 * 60  # almost identical; min_improvement_sec should reject

        a = _album(str(tmp_path), "X", "Y (Deluxe)", 135)
        result = compute_trim(a, max_tape_sec=120 * 60, mb=FakeMB(), min_improvement_sec=60)
        assert result.method == "none"

    def test_mb_result_still_over_tape_max_is_ignored(self, tmp_path):
        class FakeMB:
            enabled = True
            _ready = True
            def canonical_release_length_sec(self, artist, album, year=""):
                return 130 * 60  # shorter than 135, but still > 120-min tape

        a = _album(str(tmp_path), "X", "Y (Deluxe)", 135)
        result = compute_trim(a, max_tape_sec=120 * 60, mb=FakeMB())
        assert result.method == "none"

    def test_title_heuristic_via_stub_readers(self, monkeypatch, tmp_path):
        """Drive compute_trim's title-heuristic branch by stubbing the mutagen reader.

        We need `_list_audio_files` to return file paths and `_read_track_info`
        to return (title, duration) pairs keyed by path.
        """
        from src import trim as trim_mod

        # Fake 8 tracks: 6 clean (4 min each = 24 min), 2 bonus (3 min each = 6 min).
        # Total = 30 min; core = 24 min; saves 6 min.
        tracks = [
            ("track01.flac", "Opening", 240),
            ("track02.flac", "Next", 240),
            ("track03.flac", "Middle", 240),
            ("track04.flac", "Another", 240),
            ("track05.flac", "Penultimate", 240),
            ("track06.flac", "Closer", 240),
            ("track07.flac", "Opening (Demo)", 180),
            ("track08.flac", "Closer (Alternate Mix)", 180),
        ]
        files = []
        for fname, _, _ in tracks:
            p = tmp_path / fname
            p.write_bytes(b"")  # content doesn't matter; we stub reading
            files.append(p)

        monkeypatch.setattr(trim_mod, "_list_audio_files", lambda folder: files)

        info_by_path = {
            str(tmp_path / fname): (title, dur)
            for fname, title, dur in tracks
        }
        monkeypatch.setattr(
            trim_mod,
            "_read_track_info",
            lambda p: info_by_path.get(str(p), ("", 0)),
        )

        # Original is 30 min. Fake an album claiming 30 min, over-length for a
        # 26-min tape, should trim to 24 min.
        a = _album(str(tmp_path), "Band", "Album (Deluxe Edition)", 30)
        result = compute_trim(a, max_tape_sec=26 * 60, mb=None)

        assert result.method == "title-heuristic"
        assert result.trimmed_duration_sec == 24 * 60
        assert set(result.skip_labels) == {"Opening (Demo)", "Closer (Alternate Mix)"}
        assert "skip 2 bonus track(s)" in result.note

    def test_title_heuristic_refused_when_core_still_too_long(self, monkeypatch, tmp_path):
        """If core is still over tape max, trim is NOT applied."""
        from src import trim as trim_mod

        tracks = [("t.flac", "Track", 30 * 60)]  # single 30-min track, no bonus
        files = [tmp_path / t[0] for t in tracks]
        for p, (_, title, dur) in zip(files, tracks):
            p.write_bytes(b"")

        monkeypatch.setattr(trim_mod, "_list_audio_files", lambda folder: files)
        monkeypatch.setattr(
            trim_mod,
            "_read_track_info",
            lambda p: ("Track", 30 * 60),
        )

        a = _album(str(tmp_path), "A", "X (Deluxe)", 30)
        result = compute_trim(a, max_tape_sec=20 * 60, mb=None)
        assert result.method == "none"


# ---------------------------------------------------------------------------
# Planner integration
# ---------------------------------------------------------------------------

class TestPlannerTrimIntegration:
    def test_trim_off_leaves_overlength_unplaced(self, tmp_path):
        a = _album(str(tmp_path), "X", "Album (Deluxe Edition)", 140)
        cfg = PlannerConfig(
            allow_musicbrainz=False, allow_lastfm=False, trim_mode="off",
        )
        assignments, unplaced = plan_tapes([a], mb=None, cfg=cfg)
        assert assignments == []
        assert len(unplaced) == 1

    def test_trim_unplaced_rescues_via_mb(self, tmp_path):
        """trim_mode='unplaced' should use MB to shrink an unplaceable album."""
        class FakeMB:
            enabled = True
            _ready = True
            def canonical_release_length_sec(self, artist, album, year=""):
                return 47 * 60  # fits a 54-min cassette, originally 140 min (unplaceable)

        a = _album(str(tmp_path), "The Beatles", "Abbey Road (Super Deluxe Edition)", 140)
        cfg = PlannerConfig(
            allow_musicbrainz=False, allow_lastfm=False, trim_mode="unplaced",
        )
        assignments, unplaced = plan_tapes([a], mb=FakeMB(), cfg=cfg)
        assert unplaced == []
        assert len(assignments) == 1
        asn = assignments[0]
        assert asn.match_kind == "solo-trimmed"
        assert asn.side_a.duration_sec == 47 * 60
        assert asn.side_a_original_sec == 140 * 60
        assert "MusicBrainz canonical" in asn.side_a_trim_note

    def test_trim_unplaced_compilation_refused(self, tmp_path):
        class FakeMB:
            enabled = True
            _ready = True
            def canonical_release_length_sec(self, artist, album, year=""):
                return 44 * 60  # MB would say the "canonical" is 44 min, but we refuse

        a = _album(str(tmp_path), "VA", "Greatest Hits (Anniversary Edition)", 140)
        cfg = PlannerConfig(
            allow_musicbrainz=False, allow_lastfm=False, trim_mode="unplaced",
        )
        assignments, unplaced = plan_tapes([a], mb=FakeMB(), cfg=cfg)
        assert assignments == []
        assert len(unplaced) == 1

    def test_trim_all_shrinks_overlength_before_planning(self, tmp_path):
        """trim_mode='all' uses the trimmed length for greedy planning decisions.

        Here an 85-min deluxe version of an actually-45-min album would normally
        land on a 90-min reel (solo), leaving the smaller cassettes unused. With
        trim_mode='all', the MB-canonical 45-min version lets it live on a
        46-min cassette instead."""
        class FakeMB:
            enabled = True
            _ready = True
            def canonical_release_length_sec(self, artist, album, year=""):
                return 45 * 60

        a = _album(str(tmp_path), "X", "Y (Deluxe Edition)", 85)
        cfg = PlannerConfig(
            allow_musicbrainz=False, allow_lastfm=False, trim_mode="all",
        )
        assignments, unplaced = plan_tapes([a], mb=FakeMB(), cfg=cfg)
        assert unplaced == []
        asn = assignments[0]
        assert asn.side_a.duration_sec == 45 * 60
        assert asn.side_a_original_sec == 85 * 60
        assert asn.tape.total_sec == 46 * 60  # smallest fitting tape after trim
        assert "MusicBrainz canonical" in asn.side_a_trim_note

    def test_trim_all_does_not_touch_already_fitting_album(self, tmp_path):
        """An album that already fits the smallest tape doesn't get trimmed
        (no MB call, no tag reading), even with trim_mode='all'."""
        class SpyMB:
            enabled = True
            _ready = True
            def __init__(self):
                self.calls = 0
            def canonical_release_length_sec(self, artist, album, year=""):
                self.calls += 1
                return 30 * 60

        a = _album(str(tmp_path), "X", "Y (Deluxe Edition)", 40)  # already fits 46-min
        mb = SpyMB()
        cfg = PlannerConfig(
            allow_musicbrainz=False, allow_lastfm=False, trim_mode="all",
        )
        assignments, _ = plan_tapes([a], mb=mb, cfg=cfg)
        assert mb.calls == 0, "MB should not be consulted for albums that already fit"
        # Album lands on its natural 46-min cassette, untrimmed.
        assert assignments[0].side_a_original_sec == 0
        assert not assignments[0].side_a_trim_note
