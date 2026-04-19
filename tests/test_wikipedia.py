from src.wikipedia import (
    EN_CONFIG,
    LT_CONFIG,
    _artist_token_set,
    _artist_tokens_overlap,
    _clean_album_title,
    _clean_artist_name,
    _common_prefix_len,
    _dedupe_preserve,
    _extract_album_from_prefixed,
    _is_album_infobox,
    _title_has_album_qualifier,
    _title_matches,
    _title_plausible,
    _translate_lt_genre,
    parse_infobox_artist,
    parse_infobox_genres,
)


# --- _title_matches -----------------------------------------------------------


def test_title_matches_exact_with_qualifier():
    assert _title_matches("ABBA", "Voyage", "Voyage (ABBA album)")
    assert _title_matches("A Flock of Seagulls", "A Flock of Seagulls", "A Flock of Seagulls (album)")
    # Eponymous album articles sometimes use the self-titled form "Artist (album)".
    assert _title_matches("A Flock of Seagulls", "A Flock of Seagulls", "A Flock of Seagulls")


def test_title_matches_accepts_exact_album_name_as_title():
    # When the Wikipedia article is un-disambiguated ("Tattoo You", "Skinty Fia", etc.),
    # the title is just the album name. We accept it here; the downstream infobox check
    # will reject band / song articles that happen to share the title.
    assert _title_matches("The Rolling Stones", "Tattoo You", "Tattoo You")
    assert _title_matches("AC/DC", "If You Want Blood You've Got It", "If You Want Blood You've Got It")
    assert _title_matches("Fontaines DC", "Skinty Fia", "Skinty Fia")


def test_title_matches_rejects_unrelated_without_qualifier_or_artist():
    # Still reject titles that merely start with the album's first word but are clearly unrelated.
    assert not _title_matches("ABBA", "Voyage", "Voyager")
    assert not _title_matches("Some Artist", "Voyage", "Voyage: A Travel Magazine")


def test_title_matches_rejects_unrelated():
    assert not _title_matches("ABBA", "Voyage", "Star Trek: Voyager")
    assert not _title_matches("Some Artist", "Their Album", "Something Entirely Different")


def test_title_matches_accepts_album_qualifier_alone():
    # Title lacks artist but has exact album + qualifier: acceptable under strict rules.
    assert _title_matches("Some Artist", "Voyage", "Voyage (album)")


# --- _is_album_infobox --------------------------------------------------------


def test_is_album_infobox_variants():
    assert _is_album_infobox("{{Infobox album\n| name = X\n}}")
    assert _is_album_infobox("{{Infobox soundtrack\n| name = X\n}}")
    assert _is_album_infobox("{{Infobox extended play | name = X }}")
    assert _is_album_infobox("{{Infobox Album\n| name = X }}")  # case-insensitive
    assert not _is_album_infobox("{{Infobox musical artist\n| name = X\n}}")
    assert not _is_album_infobox("no infobox here")


# --- parse_infobox_genres -----------------------------------------------------


SAMPLE_WIKITEXT_BASIC = """{{Infobox album
| name = Back in Black
| artist = AC/DC
| released = 25 July 1980
| genre = [[Hard rock]], [[heavy metal]]
| length = 42:11
| producer = [[Robert John Lange|Robert John "Mutt" Lange]]
}}
Some article body here."""


def test_parse_basic_links_and_display():
    g = parse_infobox_genres(SAMPLE_WIKITEXT_BASIC)
    assert g == ["Hard rock", "heavy metal"]


SAMPLE_WIKITEXT_HLIST = """{{Infobox album
| name = Foo
| genre = {{hlist|[[Synth-pop]]|[[New wave music|new wave]]|[[Post-punk]]}}
| label = EMI
}}"""


def test_parse_hlist_template():
    g = parse_infobox_genres(SAMPLE_WIKITEXT_HLIST)
    assert g == ["Synth-pop", "new wave", "Post-punk"]


SAMPLE_WIKITEXT_FLATLIST = """{{Infobox album
| name = Bar
| genre = {{flatlist|
* [[Indie rock]]
* [[Dream pop]]
}}
| label = 4AD
}}"""


def test_parse_flatlist_bullets():
    g = parse_infobox_genres(SAMPLE_WIKITEXT_FLATLIST)
    assert "Indie rock" in g and "Dream pop" in g


SAMPLE_WIKITEXT_REFS = """{{Infobox album
| name = Baz
| genre = [[Electronic music|Electronic]]<ref name="pitchfork"/><ref>cite</ref>, [[Ambient]]
| label = ABC
}}"""


def test_parse_strips_refs():
    g = parse_infobox_genres(SAMPLE_WIKITEXT_REFS)
    assert g == ["Electronic", "Ambient"]


SAMPLE_WIKITEXT_NO_GENRE = """{{Infobox album
| name = No Genres
| label = X
}}"""


def test_parse_returns_empty_when_no_genre_field():
    assert parse_infobox_genres(SAMPLE_WIKITEXT_NO_GENRE) == []


SAMPLE_WIKITEXT_MULTILINE = """{{Infobox album
| name = Foo
| genre =
* [[Synth-pop]]
* [[New wave music|new wave]]
| label = EMI
}}"""


def test_parse_multiline_bullets():
    g = parse_infobox_genres(SAMPLE_WIKITEXT_MULTILINE)
    assert g == ["Synth-pop", "new wave"]


# --- _clean_album_title -------------------------------------------------------


def test_clean_strips_edition_parens_and_empty_parens():
    assert _clean_album_title("Tattoo You (Remastered)") == "Tattoo You"
    assert _clean_album_title("A Flock Of Seagulls (Expanded Edition)") == "A Flock Of Seagulls"
    assert _clean_album_title("Getz-Gilberto (Expanded Edition)") == "Getz-Gilberto"
    assert _clean_album_title("The Sign (Remastered)") == "The Sign"


def test_clean_strips_bracketed_source_tags():
    assert _clean_album_title("Chet Baker (Hi Res [192-24])") == "Chet Baker"
    assert _clean_album_title("Imperception (1984) [LP 24-192]") == "Imperception"


def test_clean_strips_soundtrack_suffix_parens():
    # "Original Soundtrack" inside parens is a qualifier, not part of identity.
    assert _clean_album_title("Top Gun: Maverick (Music From The Motion Picture)") == "Top Gun: Maverick"
    assert _clean_album_title("Blade Runner 2049 (Original Motion Picture Soundtrack)") == "Blade Runner 2049"
    assert _clean_album_title("Radio, Vol. 2 (Original Soundtrack)") == "Radio, Vol. 2"


def test_clean_preserves_soundtrack_when_not_in_parens():
    # Soundtrack descriptors that are part of the album's own name survive if not in parens.
    # "(PA Version)" is an edition qualifier and is stripped; "Original Soundtrack" stays.
    assert _clean_album_title("Kill Bill Vol. 1 Original Soundtrack (PA Version)") == "Kill Bill Vol. 1 Original Soundtrack"


def test_clean_strips_disc_markers():
    assert _clean_album_title("Teisingos dainos (Diskas 1)") == "Teisingos dainos"
    assert _clean_album_title("Behaviour - Disc 2") == "Behaviour"


def test_clean_strips_trailing_bootleg_series():
    assert _clean_album_title(
        "Miles at The Fillmore - The Bootleg Series, Vol. 3"
    ) == "Miles at The Fillmore"


# --- _clean_artist_name -------------------------------------------------------


def test_clean_artist_takes_first_of_collaboration():
    assert _clean_artist_name("Brian Eno, Daniel Lanois, Roger Eno") == "Brian Eno"
    assert _clean_artist_name("Ben Webster, Oscar Peterson") == "Ben Webster"
    assert _clean_artist_name("Lady Gaga, OneRepublic, Lorne Balfe") == "Lady Gaga"


def test_clean_artist_drops_trailing_album_suffix():
    # Folder-regex misfire that put "Artist - Album" in the artist field.
    assert _clean_artist_name("Foje - Kitoks Pasaulis (1992)") == "Foje"


def test_clean_artist_recovers_from_generic_when_album_has_prefix():
    # "Music" is a generic placeholder; the real artist is embedded in the album name.
    assert _clean_artist_name(
        "Music", album="Rockets \u2013 Imperception (1984) [LP 24-192]"
    ) == "Rockets"
    assert _clean_artist_name(
        "Music", album="The Orb's Adventures Beyond The Ultraworld (1991)"
    ) == "Music"  # no " - " separator \u2192 fall back to generic


def test_extract_album_from_prefixed():
    assert _extract_album_from_prefixed("Rockets \u2013 Imperception (1984)") == "Imperception (1984)"
    assert _extract_album_from_prefixed("No Prefix Here") == "No Prefix Here"


# --- _title_plausible ---------------------------------------------------------


def test_title_plausible_accepts_qualified_titles():
    assert _title_plausible("The Rolling Stones", "Tattoo You", "Tattoo You (Rolling Stones album)")
    assert _title_plausible("Quentin Tarantino", "Kill Bill Vol. 1", "Kill Bill Vol. 1 (soundtrack)")
    assert _title_plausible("Hans Zimmer", "Blade Runner 2049", "Blade Runner 2049 (soundtrack)")


def test_title_plausible_accepts_exact_album_name():
    assert _title_plausible("Fontaines DC", "Skinty Fia", "Skinty Fia")
    assert _title_plausible("Yes", "Tales from Topographic Oceans", "Tales from Topographic Oceans")


def test_title_plausible_accepts_ambiguous_plain_title():
    # Plain "Apollo" is a candidate that the caller will validate via infobox check.
    assert _title_plausible("Brian Eno", "Apollo", "Apollo")


def test_title_plausible_accepts_extra_title_content():
    # Miles Davis' Fillmore bootleg: article title has more words than the album.
    assert _title_plausible(
        "Miles Davis",
        "Miles at The Fillmore",
        "Miles at the Fillmore \u2013 Miles Davis 1970: The Bootleg Series Vol. 3",
    )


def test_title_plausible_rejects_clearly_unrelated():
    assert not _title_plausible("Miles Davis", "Miles at The Fillmore", "Fillmore East")
    assert not _title_plausible("ABBA", "Voyage", "Voyager")
    # First album word not present at all.
    assert not _title_plausible("Some Artist", "Voyage", "Atlas of the World")


# --- _clean_album_title compound deluxe parens --------------------------------


def test_clean_strips_compound_deluxe_edition_parens():
    assert _clean_album_title("Giant Steps (60th Anniversary Super Deluxe Edition)") == "Giant Steps"
    assert _clean_album_title("Kind of Blue (50th Anniversary Collectors Edition)") == "Kind of Blue"
    assert _clean_album_title("OK Computer (OKNOTOK 1997 2017)") == "OK Computer"


# --- _artist_token_set --------------------------------------------------------


def test_artist_token_set_overlap():
    a = _artist_token_set("Brian Eno, Daniel Lanois, Roger Eno")
    b = _artist_token_set("Brian Eno")
    assert a & b == {"brian", "eno"}

    # They Might Be Giants shouldn't overlap with Brian Eno.
    assert not (_artist_token_set("They Might Be Giants") & _artist_token_set("Brian Eno"))


def test_artist_token_set_drops_stopwords():
    toks = _artist_token_set("The The")
    assert toks == set()
    toks = _artist_token_set("The Rolling Stones")
    assert toks == {"rolling", "stones"}


# --- parse_infobox_artist -----------------------------------------------------


SAMPLE_WITH_ARTIST = """{{Infobox album
| name = Apollo
| type = studio
| artist = [[Brian Eno]], [[Daniel Lanois]] and [[Roger Eno]]
| genre = [[Ambient]]
}}"""


def test_parse_infobox_artist_extracts_wiki_link_display():
    a = parse_infobox_artist(SAMPLE_WITH_ARTIST)
    assert "Brian Eno" in a
    assert "Daniel Lanois" in a
    assert "Roger Eno" in a


SAMPLE_WITH_PLAIN_ARTIST = """{{Infobox album
| name = Foo
| artist = They Might Be Giants
| genre = [[Alternative rock]]
}}"""


def test_parse_infobox_artist_extracts_plain_text():
    a = parse_infobox_artist(SAMPLE_WITH_PLAIN_ARTIST)
    assert a.strip() == "They Might Be Giants"


# --- multi-language: _artist_tokens_overlap -----------------------------------


def test_artist_tokens_overlap_exact():
    assert _artist_tokens_overlap({"metallica"}, {"metallica"})
    assert _artist_tokens_overlap({"brian", "eno"}, {"brian", "eno"})
    assert not _artist_tokens_overlap({"metallica"}, {"slayer"})


def test_artist_tokens_overlap_lt_declensions():
    # LT: "Antis" (nominative) vs "Anties" (genitive) must be treated as the same name.
    assert _artist_tokens_overlap({"antis"}, {"anties"})
    # "Foje" vs "Foj\u0117" / "Fojes" / etc.
    assert _artist_tokens_overlap({"foje"}, {"fojes"})


def test_artist_tokens_overlap_rejects_short_false_positives():
    # Short (< 4 char) tokens don't activate the fuzzy check.
    assert not _artist_tokens_overlap({"foo"}, {"football"})
    # Distinct stems stay distinct even if one letter happens to match.
    assert not _artist_tokens_overlap({"metallica"}, {"megadeth"})


def test_common_prefix_len():
    assert _common_prefix_len("antis", "anties") == 4
    assert _common_prefix_len("foje", "fojes") == 4
    assert _common_prefix_len("abc", "xyz") == 0
    assert _common_prefix_len("same", "same") == 4


# --- multi-language: _title_has_album_qualifier -------------------------------


def test_title_qualifier_en():
    assert _title_has_album_qualifier("Tattoo You (album)", EN_CONFIG)
    assert _title_has_album_qualifier("Kill Bill Vol. 1 (soundtrack)", EN_CONFIG)
    assert not _title_has_album_qualifier("Blade Runner", EN_CONFIG)


def test_title_qualifier_lt_static_and_year():
    # Static "(albumas)".
    assert _title_has_album_qualifier("Antis (albumas)", LT_CONFIG)
    # Year-qualified "(1987 albumas)" \u2014 the LT convention.
    assert _title_has_album_qualifier("Antis (1987 albumas)", LT_CONFIG)
    assert _title_has_album_qualifier("Geltoni krantai (1989 albumas)", LT_CONFIG)
    # Not an album qualifier \u2014 LT band-disambig form.
    assert not _title_has_album_qualifier("Antis (roko grup\u0117)", LT_CONFIG)


# --- multi-language: LT infobox parsing ---------------------------------------


LT_INFOBOX = """{{Infolentel\u0117 albumas
| Pavadinimas = Antis
| Atlik\u0117jas = [[Antis]]
| Fonas = studijinis
| Formatas = [[studijinis albumas]]
| I\u0161leistas = 1987
| \u017danras = [[Rokas]], [[Pankrokas]]
| Trukm\u0117 = 45:00
}}"""


def test_lt_is_album_infobox():
    assert _is_album_infobox(LT_INFOBOX, LT_CONFIG)
    # EN cfg shouldn't recognize the LT template.
    assert not _is_album_infobox(LT_INFOBOX, EN_CONFIG)


def test_lt_parse_genres():
    g = parse_infobox_genres(LT_INFOBOX, LT_CONFIG)
    assert g == ["Rokas", "Pankrokas"]


def test_lt_parse_artist():
    a = parse_infobox_artist(LT_INFOBOX, LT_CONFIG)
    assert a.strip() == "Antis"


# --- multi-language: LT -> EN genre translation -------------------------------


def test_translate_lt_genre_common_cases():
    assert _translate_lt_genre("Rokas") == "Rock"
    assert _translate_lt_genre("rokas") == "Rock"
    assert _translate_lt_genre("Thrash metalas") == "Thrash metal"
    assert _translate_lt_genre("Hip hopas") == "Hip hop"
    assert _translate_lt_genre("Pankrokas") == "Punk rock"


def test_translate_lt_genre_unknown_passes_through():
    # Unknown tokens stay as-is.
    assert _translate_lt_genre("Sraigta\u0161raktis") == "Sraigta\u0161raktis"


def test_dedupe_preserve_keeps_first_case():
    assert _dedupe_preserve(["Rock", "rock", "ROCK"]) == ["Rock"]
    assert _dedupe_preserve(["Rock", "Jazz", "Rock"]) == ["Rock", "Jazz"]
