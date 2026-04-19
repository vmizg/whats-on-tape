from src.enrich import enrich_albums
from src.models import Album


class FakeMB:
    """Stand-in for MBClient with a pre-canned lookup dict."""

    def __init__(self, data: dict[tuple[str, str], list[str]]):
        self.data = data
        self.enabled = True

    def genres_for_album(self, artist: str, album: str, year: str = "") -> list[str]:
        return list(self.data.get((artist.lower(), album.lower()), []))


class FakeLF:
    def __init__(self, data: dict[tuple[str, str], list[str]], enabled: bool = True):
        self.data = data
        self.enabled = enabled

    def genres_for_album(self, artist: str, album: str) -> list[str]:
        return list(self.data.get((artist.lower(), album.lower()), []))


class FakeWiki:
    def __init__(self, data: dict[tuple[str, str], list[str]], enabled: bool = True):
        self.data = data
        self.enabled = enabled

    def genres_for_album(self, artist: str, album: str, year: str = "") -> list[str]:
        return list(self.data.get((artist.lower(), album.lower()), []))


def _alb(artist: str, title: str, genres=None) -> Album:
    return Album(
        path=f"/m/{artist}-{title}",
        artist=artist,
        album=title,
        duration_sec=2000,
        genres=list(genres or []),
    )


def test_enrich_skips_album_with_specific_tags():
    """An album that already has at least one non-vague tag shouldn't be re-enriched."""
    tagged = _alb("A", "1", genres=["Post-Punk"])
    summary = enrich_albums([tagged], mb=FakeMB({}), lastfm=None, progress=False)
    assert summary["candidates"] == 0
    assert tagged.genres == ["Post-Punk"]


def test_enrich_skips_album_with_specific_primary_and_vague_secondary():
    """Mixed tags count as specific enough; only ALL-vague albums go for clarification."""
    tagged = _alb("A", "1", genres=["Psychedelic Rock", "Rock"])
    summary = enrich_albums([tagged], mb=FakeMB({}), lastfm=None, progress=False)
    assert summary["candidates"] == 0
    assert tagged.genres == ["Psychedelic Rock", "Rock"]


def test_enrich_from_mb_only():
    empty = _alb("Kraftwerk", "Autobahn")
    mb = FakeMB({("kraftwerk", "autobahn"): ["Electronic", "Krautrock"]})
    summary = enrich_albums([empty], mb=mb, lastfm=None, progress=False)
    assert summary["enriched_mb"] == 1
    assert summary["enriched_lastfm"] == 0
    assert empty.genres == ["Electronic", "Krautrock"]


def test_enrich_falls_back_to_lastfm():
    empty = _alb("Obscure", "Thing")
    mb = FakeMB({})
    lf = FakeLF({("obscure", "thing"): ["Dream Pop"]})
    summary = enrich_albums([empty], mb=mb, lastfm=lf, progress=False)
    assert summary["enriched_mb"] == 0
    assert summary["enriched_lastfm"] == 1
    assert empty.genres == ["Dream Pop"]


def test_enrich_merges_mb_and_lastfm_dedup_case_insensitive():
    empty = _alb("Band", "Album")
    mb = FakeMB({("band", "album"): ["Rock"]})
    lf = FakeLF({("band", "album"): ["rock", "Post-Punk"]})
    summary = enrich_albums([empty], mb=mb, lastfm=lf, progress=False)
    assert summary["enriched_mb"] == 1  # MB credited as primary source
    assert empty.genres == ["Rock", "Post-Punk"]


def test_enrich_still_empty_on_miss():
    empty = _alb("Unknown", "Nothing")
    mb = FakeMB({})
    lf = FakeLF({})
    summary = enrich_albums([empty], mb=mb, lastfm=lf, progress=False)
    assert summary["still_empty"] == 1
    assert empty.genres == []


def test_enrich_ignores_disabled_lastfm():
    empty = _alb("Band", "Album")
    mb = FakeMB({})
    lf = FakeLF({("band", "album"): ["Pop"]}, enabled=False)
    summary = enrich_albums([empty], mb=mb, lastfm=lf, progress=False)
    assert summary["still_empty"] == 1
    assert empty.genres == []


def test_enrich_falls_back_to_wikipedia_when_mb_and_lastfm_miss():
    empty = _alb("Flock", "Seagulls")
    mb = FakeMB({})
    lf = FakeLF({})
    wiki = FakeWiki({("flock", "seagulls"): ["New wave", "synth-pop"]})
    summary = enrich_albums([empty], mb=mb, lastfm=lf, wiki=wiki, progress=False)
    assert summary["enriched_wiki"] == 1
    assert summary["enriched_mb"] == 0
    assert summary["enriched_lastfm"] == 0
    assert empty.genres == ["New wave", "synth-pop"]


def test_enrich_skips_wiki_if_mb_already_found_specific():
    """When MB returns a specific genre, Wikipedia isn't consulted."""
    empty = _alb("Band", "Album")
    mb = FakeMB({("band", "album"): ["Post-Punk"]})
    lf = FakeLF({})
    wiki = FakeWiki({("band", "album"): ["WrongGenre"]})
    summary = enrich_albums([empty], mb=mb, lastfm=lf, wiki=wiki, progress=False)
    assert summary["enriched_mb"] == 1
    assert summary["enriched_wiki"] == 0
    assert empty.genres == ["Post-Punk"]


def test_enrich_consults_wiki_when_mb_only_returns_vague():
    """If MB and Last.fm only supply a vague tag, Wikipedia is still consulted."""
    empty = _alb("Band", "Album")
    mb = FakeMB({("band", "album"): ["Rock"]})
    lf = FakeLF({})
    wiki = FakeWiki({("band", "album"): ["Post-Punk", "New Wave"]})
    summary = enrich_albums([empty], mb=mb, lastfm=lf, wiki=wiki, progress=False)
    # MB was the first non-empty answer, but wiki was needed to get something specific.
    # For an empty album, the summary credits only the first provider.
    assert summary["enriched_mb"] == 1
    assert empty.genres[0] == "Rock"
    assert "Post-Punk" in empty.genres
    assert "New Wave" in empty.genres


# --- Vague-clarification path --------------------------------------------------

def test_clarify_replaces_vague_primary_with_specific():
    """Album already tagged just 'Rock' gets a proper sub-genre from MB."""
    vague = _alb("A Flock of Seagulls", "A Flock of Seagulls", genres=["Rock"])
    mb = FakeMB({
        ("a flock of seagulls", "a flock of seagulls"): ["New Wave", "Synth-pop"],
    })
    summary = enrich_albums([vague], mb=mb, lastfm=None, progress=False)
    assert summary["candidates_vague"] == 1
    assert summary["clarified_mb"] == 1
    # Specific tags first, vague original kept as fallback at the end.
    assert vague.genres[0] == "New Wave"
    assert "Synth-pop" in vague.genres
    assert "Rock" in vague.genres


def test_clarify_falls_back_to_wikipedia_when_mb_and_lastfm_also_vague():
    """MB and Last.fm only confirm the vague tag; Wikipedia provides specifics."""
    vague = _alb("Obscure", "Album", genres=["Electronic"])
    mb = FakeMB({("obscure", "album"): ["Electronic"]})
    lf = FakeLF({("obscure", "album"): ["electronic"]})
    wiki = FakeWiki({("obscure", "album"): ["IDM", "Ambient"]})
    summary = enrich_albums([vague], mb=mb, lastfm=lf, wiki=wiki, progress=False)
    assert summary["clarified_wiki"] == 1
    assert vague.genres[0] == "IDM"
    assert "Ambient" in vague.genres
    assert "Electronic" in vague.genres


def test_clarify_leaves_album_untouched_when_everyone_is_vague():
    """If no provider can do better than the vague tag, original is preserved."""
    vague = _alb("A", "1", genres=["Rock"])
    mb = FakeMB({("a", "1"): ["rock"]})
    lf = FakeLF({("a", "1"): ["Rock"]})
    wiki = FakeWiki({("a", "1"): ["Rock"]})
    summary = enrich_albums([vague], mb=mb, lastfm=lf, wiki=wiki, progress=False)
    assert summary["still_vague"] == 1
    assert summary["clarified_mb"] + summary["clarified_lastfm"] + summary["clarified_wiki"] == 0
    assert vague.genres == ["Rock"]


def test_clarify_leaves_album_untouched_when_no_provider_matches():
    """No online hit at all: keep the vague tag as-is rather than dropping it."""
    vague = _alb("Nobody", "Knows", genres=["Jazz"])
    summary = enrich_albums(
        [vague], mb=FakeMB({}), lastfm=FakeLF({}), wiki=FakeWiki({}), progress=False
    )
    assert summary["still_vague"] == 1
    assert vague.genres == ["Jazz"]


def test_clarify_does_not_duplicate_existing_specific_tags():
    """If MB returns both the vague tag and a specific one we already knew, no dupes."""
    vague = _alb("A", "1", genres=["Rock"])
    mb = FakeMB({("a", "1"): ["Rock", "Post-Punk"]})
    summary = enrich_albums([vague], mb=mb, lastfm=None, progress=False)
    assert summary["clarified_mb"] == 1
    # Specific first, vague kept; no repeated entries even though MB echoed "Rock".
    assert vague.genres == ["Post-Punk", "Rock"]
