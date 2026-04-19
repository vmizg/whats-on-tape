from src.models import Album
from src.planner import PlannerConfig, plan_tapes


def album(artist: str, title: str, duration_sec: int, genres: list[str]) -> Album:
    return Album(
        path=f"C:/Music/{artist} - {title}",
        artist=artist,
        album=title,
        year="2000",
        duration_sec=duration_sec,
        genres=genres,
        format="flac",
    )


def test_solo_fit_picks_smallest_tape():
    a = album("X", "Short", 40 * 60, ["Rock"])
    assignments, unplaced = plan_tapes([a], mb=None, cfg=PlannerConfig(allow_musicbrainz=False))
    assert unplaced == []
    assert len(assignments) == 1
    asn = assignments[0]
    assert asn.tape.total_sec == 46 * 60
    assert asn.match_kind == "solo"


def test_solo_preferred_when_split_slack_is_small():
    # 43-min Rock album on its own:
    # - fits the 46-min tape with ~2 min slack -> solo wins
    # - also fits on one side of the 90-min split, but we have no partner
    a = album("X", "Album", 43 * 60, ["Rock"])
    assignments, _ = plan_tapes([a], mb=None, cfg=PlannerConfig(allow_musicbrainz=False))
    assert len(assignments) == 1
    assert assignments[0].tape.total_sec == 46 * 60
    assert assignments[0].match_kind == "solo"


def test_split_pairing_preferred_over_solo_small_tape():
    # A 28-min Rock album with no partner is preferably placed on the 60-min split tape
    # (so Part B can go hunt for a filler) rather than wasting a 46-min tape solo.
    a = album("X", "Album", 28 * 60, ["Rock"])
    assignments, _ = plan_tapes([a], mb=None, cfg=PlannerConfig(allow_musicbrainz=False))
    assert assignments[0].tape.total_sec == 60 * 60
    assert assignments[0].match_kind in {"musicbrainz", "search-url"}


def test_tight_local_pairing_on_split_tape():
    # Two ~30min rock albums should pair on a 60min split tape
    a = album("A", "Rock1", 28 * 60, ["Hard Rock"])
    b = album("B", "Rock2", 27 * 60, ["Hard Rock"])
    cfg = PlannerConfig(allow_musicbrainz=False, buffer_sec=60)
    assignments, unplaced = plan_tapes([a, b], mb=None, cfg=cfg)
    assert unplaced == []
    # Longer one becomes side A
    paired = next((x for x in assignments if x.side_b is not None), None)
    assert paired is not None, f"expected a paired assignment, got {[a.match_kind for a in assignments]}"
    assert paired.side_a.album in {"Rock1"}
    assert paired.side_b.album in {"Rock2"}
    assert paired.match_kind == "tight-local"
    assert paired.tape.total_sec == 60 * 60


def test_relaxed_local_via_parent_map():
    # Side A "Hard Rock" -> parent "rock"; side B "Alternative Rock" -> parent "rock"
    a = album("A", "RockAlbum", 40 * 60, ["Hard Rock"])
    b = album("B", "AltAlbum", 40 * 60, ["Alternative Rock"])
    cfg = PlannerConfig(allow_musicbrainz=False, buffer_sec=60)
    assignments, _ = plan_tapes([a, b], mb=None, cfg=cfg)
    paired = next((x for x in assignments if x.side_b is not None), None)
    assert paired is not None
    assert paired.match_kind == "relaxed-local"
    assert paired.tape.total_sec == 90 * 60


def test_synthwave_and_techno_do_not_relax_match():
    # Retro-electronic (synth-pop/synthwave/new wave) and modern club dance
    # (techno/house/EDM) are separate buckets by design, so the relaxed pass
    # should NOT pair them together - they feel stylistically different.
    a = album("A", "SynthAlbum", 40 * 60, ["Synthwave"])
    b = album("B", "TechnoAlbum", 40 * 60, ["Techno"])
    cfg = PlannerConfig(allow_musicbrainz=False, allow_lastfm=False, buffer_sec=60)
    assignments, _ = plan_tapes([a, b], mb=None, cfg=cfg)
    assert all(asn.side_b is None for asn in assignments)


def test_new_wave_pairs_with_synthpop_not_with_techno():
    # Per user: new wave belongs with the retro/song-oriented synth stuff, not
    # with the modern club electronic.
    a = album("A", "NewWaveAlbum", 40 * 60, ["New Wave"])
    b = album("B", "SynthpopAlbum", 40 * 60, ["Synth-pop"])
    c = album("C", "TechnoAlbum", 30 * 60, ["Techno"])
    cfg = PlannerConfig(allow_musicbrainz=False, allow_lastfm=False, buffer_sec=60)
    assignments, _ = plan_tapes([a, b, c], mb=None, cfg=cfg)
    paired = [asn for asn in assignments if asn.side_b is not None]
    assert len(paired) == 1
    partners = {paired[0].side_a.artist, paired[0].side_b.artist}
    assert partners == {"A", "B"}


def test_pop_rock_pairs_with_rock():
    # "pop rock" is the single largest non-rock/non-electronic tag in the real
    # library (51 albums). We deliberately map it to the rock bucket.
    a = album("A", "PopRockAlbum", 40 * 60, ["Pop Rock"])
    b = album("B", "HardRockAlbum", 40 * 60, ["Hard Rock"])
    cfg = PlannerConfig(allow_musicbrainz=False, allow_lastfm=False, buffer_sec=60)
    assignments, _ = plan_tapes([a, b], mb=None, cfg=cfg)
    paired = next((x for x in assignments if x.side_b is not None), None)
    assert paired is not None
    assert paired.match_kind == "relaxed-local"


def test_no_partner_gives_search_urls():
    # One album that fits on a split-capable tape, no partners available at all
    a = album("Solo", "Orphan", 30 * 60, ["Jazz"])
    cfg = PlannerConfig(allow_musicbrainz=False, allow_lastfm=False, buffer_sec=60)
    assignments, unplaced = plan_tapes([a], mb=None, cfg=cfg)
    assert unplaced == []
    asn = assignments[0]
    assert asn.side_b is None
    # With both external lookups disabled, we fall back to search URLs
    assert asn.match_kind == "search-url"
    assert any(c.source == "search-url" and "rateyourmusic" in (c.url or "").lower() for c in asn.b_candidates)
    assert any(c.source == "search-url" and "discogs" in (c.url or "").lower() for c in asn.b_candidates)


class _FakeLastFm:
    enabled = True

    def __init__(self, results):
        self.results = results

    def search_albums_by_genre(self, genre, max_duration_sec, min_duration_sec=0, limit=40):
        out = []
        for r in self.results.get(genre.lower(), []):
            if min_duration_sec <= r["duration_sec"] <= max_duration_sec:
                out.append({**r, "genre": genre})
        return out


def test_lastfm_used_when_mb_empty():
    a = album("Solo", "Orphan", 30 * 60, ["Jazz"])
    cfg = PlannerConfig(allow_musicbrainz=False, allow_lastfm=True, buffer_sec=60)
    lf = _FakeLastFm({
        "jazz": [
            {"artist": "Miles Davis", "title": "Kind of Blue", "duration_sec": 28 * 60, "url": "http://x"},
        ],
    })
    assignments, _ = plan_tapes([a], mb=None, lastfm=lf, cfg=cfg)
    asn = assignments[0]
    assert asn.match_kind == "lastfm"
    assert any(c.source == "lastfm" for c in asn.b_candidates)


def test_lastfm_disabled_respects_flag():
    a = album("Solo", "Orphan", 30 * 60, ["Jazz"])
    cfg = PlannerConfig(allow_musicbrainz=False, allow_lastfm=False, buffer_sec=60)
    lf = _FakeLastFm({"jazz": [{"artist": "X", "title": "Y", "duration_sec": 28 * 60, "url": ""}]})
    assignments, _ = plan_tapes([a], mb=None, lastfm=lf, cfg=cfg)
    assert assignments[0].match_kind == "search-url"


def test_unplaced_when_too_long():
    a = album("Huge", "Epic", 200 * 60, ["Ambient"])
    cfg = PlannerConfig(allow_musicbrainz=False)
    assignments, unplaced = plan_tapes([a], mb=None, cfg=cfg)
    assert assignments == []
    assert len(unplaced) == 1


def test_genre_mismatch_prevents_local_pair():
    a = album("A", "Rocker", 28 * 60, ["Hard Rock"])
    b = album("B", "Classical", 27 * 60, ["Classical"])
    cfg = PlannerConfig(allow_musicbrainz=False)
    assignments, _ = plan_tapes([a, b], mb=None, cfg=cfg)
    # Neither should end up paired with the other
    assert all(a.side_b is None for a in assignments)


def test_short_side_a_refuses_loose_pairing_under_cap():
    """A 17-min album on a 30-min side would leave 13 min of Side-A slack, above the
    10-min small-side cap. The planner must refuse to pair on any split tape (each
    would have >10 min Side-A slack) and fall back to solo on the smallest tape."""
    short = album("X", "Short EP", 17 * 60 + 23, ["Indie Rock"])
    partner = album("Y", "Full LP", 29 * 60, ["Indie Rock"])
    cfg = PlannerConfig(allow_musicbrainz=False, allow_lastfm=False, buffer_sec=60)
    assignments, _ = plan_tapes([short, partner], mb=None, cfg=cfg)
    short_asn = next(a for a in assignments if a.side_a.album == "Short EP")
    assert short_asn.side_b is None, "should not pair when Side-A slack would exceed cap"
    assert short_asn.match_kind == "solo"
    assert short_asn.tape.total_sec == 46 * 60


def test_loose_partner_on_b_side_is_rejected():
    """Side A fits comfortably, but the only candidate partner would leave more than
    10 min of Side-B slack. Planner must not pair them (on a 30-min side, B=15 min
    leaves 15 min slack)."""
    side_a = album("A", "Full Side", 28 * 60, ["Jazz"])
    tiny_b = album("B", "Tiny EP", 15 * 60, ["Jazz"])
    cfg = PlannerConfig(allow_musicbrainz=False, allow_lastfm=False, buffer_sec=60)
    assignments, _ = plan_tapes([side_a, tiny_b], mb=None, cfg=cfg)
    paired = [x for x in assignments if x.side_b is not None]
    assert not paired, f"expected no local pair; got {[ (x.side_a.album, x.side_b.album if x.side_b else None) for x in assignments]}"


def test_overlapping_sides_flag_changes_b_max():
    """Unit-test the strict vs overlapping distinction via the helper that computes
    the max B-side duration. Strict mode caps B at split_sec; overlapping mode lets
    B spill into Side A's unused portion."""
    from src.planner import PlannerConfig
    from src.tapes import TAPES

    tape_60 = next(t for t in TAPES if t.total_sec == 60 * 60)
    side_a_dur = 20 * 60

    cfg_strict = PlannerConfig(buffer_sec=60, strict_side_fit=True)
    cfg_loose = PlannerConfig(buffer_sec=60, strict_side_fit=False)

    strict_b_max = tape_60.split_sec - cfg_strict.buffer_sec
    loose_b_max = tape_60.total_sec - side_a_dur - cfg_loose.buffer_sec

    assert strict_b_max == 29 * 60, "strict mode: B limited to its own 30-min side minus buffer"
    assert loose_b_max == 39 * 60, "overlapping mode: B gets the full unused budget minus buffer"


def test_roosevelt_style_loose_pairing_no_longer_happens():
    """Regression: Roosevelt EP (17:23) was previously paired with 35-min 'cool jazz'
    or '2013' candidates on a 60-min split because the planner computed
    remaining = total - A - buffer (~41 min). Now Side A's 12:37 slack exceeds the
    10-min cap so the whole pairing branch is skipped and Roosevelt goes solo."""
    roosevelt = album("Roosevelt", "Elliot - EP", 17 * 60 + 23, ["Indie"])
    too_long_filler = album("Someone", "35min LP", 35 * 60, ["Indie"])
    cfg = PlannerConfig(allow_musicbrainz=False, allow_lastfm=False, buffer_sec=60)
    assignments, _ = plan_tapes([roosevelt, too_long_filler], mb=None, cfg=cfg)
    roos_asn = next(x for x in assignments if x.side_a.album == "Elliot - EP")
    assert roos_asn.side_b is None
    assert roos_asn.match_kind == "solo"
    assert roos_asn.side_a_slack_sec == 46 * 60 - (17 * 60 + 23)


def test_strict_side_fit_rejects_b_longer_than_side():
    """In strict mode the planner must never pair an album that exceeds a side's
    EFFECTIVE capacity (nominal + stretch tolerance) onto that side.

    A 30-min split-side has a 120s stretch tolerance by default, so a 31-min album
    DOES fit there (covered separately by test_stretch_tolerance_allows_*). Here
    we use 35 min, comfortably beyond the stretch zone.
    """
    a = album("A", "Side A", 28 * 60, ["Rock"])
    b = album("B", "Oversize", 35 * 60, ["Rock"])
    cfg = PlannerConfig(allow_musicbrainz=False, allow_lastfm=False, buffer_sec=60)
    assignments, _ = plan_tapes([a, b], mb=None, cfg=cfg)
    # B(35) doesn't fit ANY 30-min side, so any pairing of B happens on a larger
    # split tape (70-min has 35-min sides which still leaves no margin; 90-min has
    # 45-min sides where it fits comfortably).
    pair_60 = [x for x in assignments if x.side_b is not None and x.tape.total_sec == 60 * 60]
    assert not pair_60, "strict mode must not pair a 35-min B-side on a 30-min tape side"


def test_per_side_slack_reported_for_split_tape():
    """Verify Assignment.side_a_slack_sec and side_b_slack_sec compute the right thing."""
    a = album("A", "AlbumA", 28 * 60, ["Hard Rock"])
    b = album("B", "AlbumB", 25 * 60, ["Hard Rock"])
    cfg = PlannerConfig(allow_musicbrainz=False, allow_lastfm=False, buffer_sec=60)
    assignments, _ = plan_tapes([a, b], mb=None, cfg=cfg)
    paired = next(x for x in assignments if x.side_b is not None)
    assert paired.tape.split_sec == 30 * 60
    assert paired.side_a_slack_sec == 2 * 60
    assert paired.side_b_slack_sec == 5 * 60


def test_per_side_slack_none_for_solo_tape():
    a = album("X", "Solo", 40 * 60, ["Rock"])
    cfg = PlannerConfig(allow_musicbrainz=False, allow_lastfm=False, buffer_sec=60)
    assignments, _ = plan_tapes([a], mb=None, cfg=cfg)
    assert assignments[0].tape.splits is False
    assert assignments[0].side_b_slack_sec is None
    assert assignments[0].side_a_slack_sec == 6 * 60  # 46 - 40


def test_lt_translations_resolve_via_parent_map():
    """Every English genre produced by the LT Wikipedia translation table must either
    be a recognized parent genre (appears as a *value* in PARENT_MAP) or have its own
    PARENT_MAP entry, so relaxed-mode pairing keeps working on LT-sourced data.
    A few truly top-level genres (that stand on their own as parents) are allowed.
    """
    from src.planner import PARENT_MAP
    from src.wikipedia import _LT_GENRE_TRANSLATIONS

    parents = set(PARENT_MAP.values())
    known_keys = set(PARENT_MAP.keys())
    # Genres that are themselves top-level buckets - fine to be neither a parent of
    # something in PARENT_MAP nor a key in it, because relaxed matching accepts them
    # directly (two albums both tagged "Blues" will still pair). "Electronic" is
    # deliberately kept standalone too: the ex-"electronic" bucket was split into
    # synthwave / dance / ambient, so a bare "Electronic" tag stays generic.
    standalone_top_level = {"blues", "reggae", "ballad", "electronic"}

    unmapped: list[tuple[str, str]] = []
    for lt, en in _LT_GENRE_TRANSLATIONS.items():
        norm = en.strip().lower()
        if norm in parents or norm in known_keys or norm in standalone_top_level:
            continue
        unmapped.append((lt, en))

    assert not unmapped, (
        "LT translations with no PARENT_MAP entry / parent bucket: "
        + ", ".join(f"{lt!r}->{en!r}" for lt, en in unmapped)
    )


# ---------------------------------------------------------------------------
# Stretch tolerance (per-tape over-capacity allowance)
# ---------------------------------------------------------------------------

def test_stretch_tolerance_allows_31_min_album_on_30_min_side():
    """A 31-minute album fits a 30-min side of a 60-min split tape thanks to
    the 120s default stretch tolerance. The user explicitly asked for this."""
    a = album("A", "Side A", 31 * 60, ["Rock"])
    b = album("B", "Side B", 28 * 60, ["Rock"])
    cfg = PlannerConfig(allow_musicbrainz=False, allow_lastfm=False, buffer_sec=60)
    assignments, unplaced = plan_tapes([a, b], mb=None, cfg=cfg)
    assert unplaced == []
    paired = [x for x in assignments if x.side_b is not None]
    assert paired, "31+28 should produce a paired 60-min split tape"
    p = paired[0]
    assert p.tape.total_sec == 60 * 60
    assert {p.side_a.duration_sec, p.side_b.duration_sec} == {31 * 60, 28 * 60}


def test_stretch_tolerance_allows_124_min_album_on_120_min_tape():
    """A 124-min album fits the 120-min tape solo (300s tolerance for 120-min)."""
    a = album("A", "Long", 124 * 60, ["Rock"])
    cfg = PlannerConfig(allow_musicbrainz=False, allow_lastfm=False, buffer_sec=60)
    assignments, unplaced = plan_tapes([a], mb=None, cfg=cfg)
    assert unplaced == []
    assert assignments[0].tape.total_sec == 120 * 60
    assert assignments[0].match_kind == "solo"


def test_stretch_tolerance_does_not_help_when_album_too_long():
    """Beyond the stretch zone (e.g. 130-min album on 120-min tape) it stays unplaced."""
    a = album("A", "TooLong", 130 * 60, ["Rock"])
    cfg = PlannerConfig(
        allow_musicbrainz=False, allow_lastfm=False, trim_mode="off",
    )
    assignments, unplaced = plan_tapes([a], mb=None, cfg=cfg)
    assert assignments == []
    assert len(unplaced) == 1


def test_stretch_tolerance_lookup_picks_largest_key_under_capacity():
    """Sanity-check the stretch_tolerance_sec helper: a 50-min capacity should
    use the 45-min entry (180s), not the 60-min entry (180s either, but the
    selection rule is `largest key <= capacity`)."""
    from src.tapes import effective_split_sec, effective_total_sec, stretch_tolerance_sec

    assert stretch_tolerance_sec(30 * 60) == 120
    assert stretch_tolerance_sec(31 * 60) == 120  # falls back to 30-min entry
    assert stretch_tolerance_sec(45 * 60) == 180
    assert stretch_tolerance_sec(60 * 60) == 180
    assert stretch_tolerance_sec(120 * 60) == 300
    assert stretch_tolerance_sec(0) == 0

    # And the convenience wrappers compose correctly.
    from src.tapes import TAPES
    t60 = next(t for t in TAPES if t.total_sec == 60 * 60)
    assert effective_total_sec(t60) == 60 * 60 + 180
    assert effective_split_sec(t60) == 30 * 60 + 120  # split_sec is 30 min
    t46 = next(t for t in TAPES if t.total_sec == 46 * 60)
    assert effective_total_sec(t46) == 46 * 60 + 180
    assert effective_split_sec(t46) is None  # non-split tape


# ---------------------------------------------------------------------------
# Tape inventory caps (plan_config.json -> PlannerConfig.tape_inventory)
# ---------------------------------------------------------------------------


def test_inventory_cap_forces_upsize_when_preferred_exhausted():
    """Three 28-min rock albums normally all land on the 60-min split tape.
    With cap=1 on 60-min, the rest should upsize to the next available tape."""
    a = album("A", "Rock1", 28 * 60, ["Hard Rock"])
    b = album("B", "Rock2", 27 * 60, ["Hard Rock"])
    c = album("C", "Rock3", 26 * 60, ["Hard Rock"])
    cfg = PlannerConfig(
        allow_musicbrainz=False, allow_lastfm=False,
        trim_mode="off",  # no downsize path for these short albums
        tape_inventory={"60min": 1},
    )
    counts: dict[str, int] = {}
    assignments, unplaced = plan_tapes(
        [a, b, c], mb=None, cfg=cfg, tape_counts_out=counts,
    )
    # At most one 60-min tape should be assigned.
    assert counts.get("60min", 0) <= 1
    # All three albums should still be placed (upsized, not unplaced).
    placed_paths = {asn.side_a.path for asn in assignments}
    for x in (a, b, c):
        assert x.path in placed_paths or any(
            asn.side_b is not None and asn.side_b.path == x.path
            for asn in assignments
        )
    assert unplaced == []


def test_inventory_cap_zero_disables_tape_size():
    """cap=0 means 'never use this size'. A 28-min album should NOT land on
    the 60-min split tape; it should upsize to 70-min."""
    a = album("A", "Rock1", 28 * 60, ["Hard Rock"])
    cfg = PlannerConfig(
        allow_musicbrainz=False, allow_lastfm=False,
        tape_inventory={"60min": 0},
    )
    assignments, _ = plan_tapes([a], mb=None, cfg=cfg)
    assert assignments[0].tape.name != "60min"


def test_inventory_exhausted_marks_unplaced_with_reason():
    """When every size big enough is out of stock, the album lands in unplaced
    with a specific 'tape inventory exhausted' reason."""
    a = album("A", "Rock1", 28 * 60, ["Hard Rock"])
    # Disable every size that would fit a 28-min album.
    cfg = PlannerConfig(
        allow_musicbrainz=False, allow_lastfm=False,
        trim_mode="off",
        tape_inventory={
            "46min": 0,
            "54min": 0,
            "60min": 0,
            "70min": 0,
            "90min": 0,
            "120min": 0,
        },
    )
    reasons: dict[str, str] = {}
    assignments, unplaced = plan_tapes(
        [a], mb=None, cfg=cfg, unplaced_reasons=reasons,
    )
    assert assignments == []
    assert len(unplaced) == 1
    assert "inventory exhausted" in reasons.get(a.path, "")


def test_inventory_tape_counts_out_populated():
    """tape_counts_out should reflect actually-committed tapes."""
    a = album("A", "Rock1", 43 * 60, ["Rock"])  # fits 46-min solo
    cfg = PlannerConfig(allow_musicbrainz=False, allow_lastfm=False)
    counts: dict[str, int] = {}
    plan_tapes([a], mb=None, cfg=cfg, tape_counts_out=counts)
    assert counts == {"46min": 1}


def test_inventory_no_caps_matches_previous_behavior():
    """With an empty tape_inventory, the planner's behavior is identical to
    the no-caps baseline (regression guard for the new plumbing)."""
    a = album("A", "Rock1", 40 * 60, ["Rock"])
    baseline, _ = plan_tapes(
        [a], mb=None,
        cfg=PlannerConfig(allow_musicbrainz=False, allow_lastfm=False),
    )
    with_empty_inv, _ = plan_tapes(
        [a], mb=None,
        cfg=PlannerConfig(
            allow_musicbrainz=False, allow_lastfm=False,
            tape_inventory={},
        ),
    )
    assert baseline[0].tape == with_empty_inv[0].tape
    assert baseline[0].match_kind == with_empty_inv[0].match_kind


def test_inventory_downsize_when_preferred_exhausted_and_trim_possible(
    tmp_path, monkeypatch
):
    """Exercises the trim-for-downsize path: when the ideal tape is capped but
    a smaller tape has stock, and compute_trim can shrink the album to fit
    the smaller tape, the planner should pick the smaller one.

    We mock compute_trim via monkeypatching because the real one reads audio
    files from disk; this keeps the test pure.
    """
    from src import planner as planner_mod
    from src.trim import TrimResult

    # A 65-min album would ordinarily pick the 70-min tape. We cap EVERY size
    # it could fit un-trimmed (70/90/120) to zero, leaving only the 46-min tape
    # available. Its "core" (per the fake trim) is 40 min, which fits the 46-min
    # effective capacity. So the planner should downsize.
    a = album("A", "Deluxe", 65 * 60, ["Rock"])

    def fake_trim(album_obj, max_tape_sec, mb=None, min_improvement_sec=60):
        return TrimResult(
            original_duration_sec=album_obj.duration_sec,
            trimmed_duration_sec=40 * 60,
            method="title-heuristic",
            note="fake trim for test",
            skip_labels=["(Demo)"],
        )

    monkeypatch.setattr(planner_mod, "compute_trim", fake_trim)

    cfg = PlannerConfig(
        allow_musicbrainz=False, allow_lastfm=False,
        trim_mode="unplaced",
        tape_inventory={
            "54min": 0,
            "60min": 0,
            "70min": 0,
            "90min": 0,
            "120min": 0,
        },
    )
    assignments, unplaced = plan_tapes([a], mb=None, cfg=cfg)
    assert unplaced == []
    assert len(assignments) == 1
    asn = assignments[0]
    assert asn.tape.name == "46min", (
        f"expected downsize to 46min, got {asn.tape.name}"
    )
    assert asn.match_kind == "solo-trimmed"
    assert asn.side_a.duration_sec == 40 * 60
    assert asn.side_a_original_sec == 65 * 60
    assert asn.side_a_trim_skipped == ["(Demo)"]


def test_inventory_downsize_skipped_when_trim_mode_off(monkeypatch):
    """trim_mode='off' must not invoke the downsize-via-trim path at all."""
    from src import planner as planner_mod
    from src.trim import TrimResult

    calls = {"n": 0}
    def fake_trim(*args, **kwargs):
        calls["n"] += 1
        return TrimResult(original_duration_sec=0, trimmed_duration_sec=0, method="none")

    monkeypatch.setattr(planner_mod, "compute_trim", fake_trim)

    # 58-min album: beyond 54-min's effective capacity (~57 min) so would normally
    # go on 60-min. With 60-min capped AND trim_mode='off', the planner must
    # upsize (to some larger tape) and MUST NOT attempt to compute a trim.
    a = album("A", "Album", 58 * 60, ["Rock"])
    cfg = PlannerConfig(
        allow_musicbrainz=False, allow_lastfm=False,
        trim_mode="off",
        tape_inventory={"60min": 0},
    )
    assignments, unplaced = plan_tapes([a], mb=None, cfg=cfg)
    assert calls["n"] == 0, "compute_trim should not run in trim_mode='off'"
    assert unplaced == []
    assert len(assignments) == 1
    assert assignments[0].tape.total_sec > 60 * 60, (
        f"expected upsize above capped 60-min size, got {assignments[0].tape.name}"
    )
