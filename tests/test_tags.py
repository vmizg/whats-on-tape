from src.tags import majority_genres, parse_leaf_folder_name


def test_parse_leaf_basic():
    p = parse_leaf_folder_name("AC-DC - Back In Black (1980) [Tidal 24-48]")
    assert p is not None
    assert p.artist == "AC-DC"
    assert p.album == "Back In Black"
    assert p.year == "1980"
    assert p.source == "Tidal 24-48"


def test_parse_leaf_no_source():
    p = parse_leaf_folder_name("Abba - Voyage (2021)")
    assert p is not None
    assert p.artist == "Abba"
    assert p.album == "Voyage"
    assert p.year == "2021"
    assert p.source == ""


def test_parse_leaf_multi_year_and_trailing_bracket():
    p = parse_leaf_folder_name("Alphaville - Forever Young (1984) [LP 24-192] [P-13065]")
    assert p is not None
    assert p.artist == "Alphaville"
    assert p.album == "Forever Young"
    assert p.year == "1984"
    assert p.source == "LP 24-192"


def test_parse_leaf_year_range():
    p = parse_leaf_folder_name("Alphaville - Afternoons in Utopia (1986, 2021) [Tidal 24-48]")
    assert p is not None
    assert p.year.startswith("1986")


def test_parse_leaf_reject():
    assert parse_leaf_folder_name("Random Folder Name") is None


def test_parse_leaf_en_dash_separator():
    p = parse_leaf_folder_name("Rockets \u2013 Imperception (1984) [LP 24-192]")
    assert p is not None
    assert p.artist == "Rockets"
    assert p.album == "Imperception"
    assert p.year == "1984"
    assert p.source == "LP 24-192"


def test_parse_leaf_em_dash_separator():
    p = parse_leaf_folder_name("Rockets \u2014 Imperception (1984)")
    assert p is not None
    assert p.artist == "Rockets"
    assert p.album == "Imperception"


def test_parse_leaf_replacement_char_separator():
    # U+FFFD is what Windows shows when an en-dash is decoded with the wrong codepage.
    p = parse_leaf_folder_name("Rockets \ufffd Imperception (1984) [LP 24-192]")
    assert p is not None
    assert p.artist == "Rockets"
    assert p.album == "Imperception"


def test_parse_leaf_does_not_split_on_unspaced_dash():
    # Bare hyphens inside names (e.g. "AC-DC") must not count as the separator.
    p = parse_leaf_folder_name("AC-DC\u2013Back In Black (1980)")
    assert p is None


def test_majority_genres():
    vals = ["Hard Rock", "Hard Rock", "Rock", "Rock;Metal", "Metal"]
    g = majority_genres(vals)
    assert g[:2] == ["Hard Rock", "Rock"] or g[:2] == ["Rock", "Hard Rock"]
    assert "Metal" in g
