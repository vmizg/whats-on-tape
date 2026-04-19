"""Minimal Wikipedia client for album-genre lookup.

Strategy:
  1. Build a set of candidate article titles using Wikipedia's opensearch + search APIs.
  2. Accept only titles that look like an album article \u2014 contains the artist name
     and either the album name or a qualifier like "(album)", "(soundtrack)", etc.
  3. Fetch the article's wikitext and extract the `genre = ...` line from the album infobox.
  4. Parse `[[Link target|display]]` wiki-link syntax and strip citations / HTML tags.

The client is multi-lingual: by default it tries English first, then Lithuanian, so
albums with only an LT article (most 1980s\u20132020s Baltic releases) can still be tagged.
Each language plugs in its own infobox template name and field-name localisations.

No new dependencies; uses urllib only. Results are cached on disk with a 30-day TTL.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

USER_AGENT = "MusicTapePlanner/0.1 (+https://github.com/local/music-tape-planner; for personal cassette planning)"
CACHE_TTL_SEC = 30 * 24 * 60 * 60
MIN_INTERVAL_SEC = 0.1  # Wikipedia asks for politeness; 10 req/sec is comfortably below limits


@dataclass(frozen=True)
class LangConfig:
    """Per-language Wikipedia dialect: API endpoint, infobox / field localisations.

    Identifier conventions vary by language edition:
      - English: `{{Infobox album ...}}`, `| genre = `, `| artist = `
      - Lithuanian: `{{Infolentel\u0117 albumas ...}}`, `| \u017danras = `, `| Atlik\u0117jas = `
    """
    code: str
    api_url: str
    # Infobox header regex: matches the opening template declaration. Capture group 1
    # is the template-body name, used to decide whether it's an album-ish infobox.
    infobox_header_re: re.Pattern[str]
    # Lowercased names that identify an album-family infobox (each is prefix-matched).
    album_infobox_names: tuple[str, ...]
    # Lowercased field names to look up for `genre` / `artist`.
    genre_fields: tuple[str, ...]
    artist_fields: tuple[str, ...]
    # Disambiguator words Wikipedia uses in article titles (e.g. "album", "soundtrack").
    album_qualifiers: tuple[str, ...]
    # Extra album-qualifier regex predicates (e.g. LT's "(1987 albumas)").
    # Returns True if the lowercase title string is qualified as an album article.
    qualifier_predicate: Any = None  # Optional[Callable[[str], bool]]
    # Query-suffix words to combine with album name in search, e.g. ("album",) for EN,
    # ("albumas",) for LT.
    album_search_suffixes: tuple[str, ...] = ("album",)


EN_CONFIG = LangConfig(
    code="en",
    api_url="https://en.wikipedia.org/w/api.php",
    infobox_header_re=re.compile(r"\{\{\s*Infobox\s+([^\n|}]+)", re.IGNORECASE),
    album_infobox_names=(
        "album",
        "soundtrack",
        "extended play",
        "ep",
        "mixtape",
        "compilation",
        "live album",
        "studio album",
    ),
    genre_fields=("genre", "genres"),
    artist_fields=("artist",),
    album_qualifiers=(
        "album",
        "soundtrack",
        "ep",
        "mixtape",
        "compilation album",
        "live album",
        "studio album",
    ),
    album_search_suffixes=("album",),
)

# Lithuanian: article titles often disambiguate as "(<year> albumas)", so we accept
# any "(<digits> albumas...)" shape in addition to the flat "(albumas)" form.
_LT_QUALIFIER_RE = re.compile(r"\(\s*\d{4}\s+albumas[^)]*\)", re.IGNORECASE)


def _lt_qualifier_predicate(t_lower: str) -> bool:
    return bool(_LT_QUALIFIER_RE.search(t_lower))


# Small dictionary of common Lithuanian genre names mapped to English equivalents so
# LT-tagged albums can match EN-tagged ones in the planner. Values are the preferred
# English form. Only entries with high confidence / ambiguity-free translations are
# included \u2014 when in doubt the raw LT string is preserved.
_LT_GENRE_TRANSLATIONS: dict[str, str] = {
    "rokas": "Rock",
    "rokenrolas": "Rock and roll",
    "roko muzika": "Rock",
    "sunkusis rokas": "Hard rock",
    "thrash metalas": "Thrash metal",
    "hevi metalas": "Heavy metal",
    "heavy metalas": "Heavy metal",
    "metalas": "Metal",
    "pankrokas": "Punk rock",
    "punk rokas": "Punk rock",
    "popmuzika": "Pop",
    "pop muzika": "Pop",
    "popsas": "Pop",
    "klasikin\u0117 muzika": "Classical",
    "d\u017eazas": "Jazz",
    "bliuzas": "Blues",
    "folkas": "Folk",
    "liaudies muzika": "Folk",
    "repas": "Rap",
    "hiphopas": "Hip hop",
    "hip hopas": "Hip hop",
    "elektronin\u0117 muzika": "Electronic",
    "elektronika": "Electronic",
    "disko": "Disco",
    "regis": "Reggae",
    "reg\u011b": "Reggae",
    "alternatyvusis rokas": "Alternative rock",
    "postrokas": "Post-rock",
    "indie rokas": "Indie rock",
    "progresyvusis rokas": "Progressive rock",
    "progrokas": "Progressive rock",
    "psichodelinis rokas": "Psychedelic rock",
    "grand\u017eas": "Grunge",
    "grundzh": "Grunge",
    "kantri": "Country",
    "kantri muzika": "Country",
    "baladi\u0173": "Ballad",
    "baladin\u0117": "Ballad",
    "daina": "Song",
    "\u0161ansonas": "Chanson",
    "bardai": "Folk",  # LT "bardai" \u2014 bard/acoustic songwriters
    "bard\u0173 muzika": "Folk",
    "autorin\u0117 daina": "Folk",
}


def _dedupe_preserve(items: list[str]) -> list[str]:
    """Return `items` with case-insensitive duplicates removed, preserving first-seen case."""
    seen: set[str] = set()
    out: list[str] = []
    for x in items:
        k = x.strip().lower()
        if k and k not in seen:
            seen.add(k)
            out.append(x)
    return out


def _translate_lt_genre(g: str) -> str:
    """Map a raw LT genre label to its EN equivalent when we have a confident mapping.

    Returns the translated English form, or the original label unchanged otherwise.
    """
    key = g.strip().lower()
    return _LT_GENRE_TRANSLATIONS.get(key, g)


LT_CONFIG = LangConfig(
    code="lt",
    api_url="https://lt.wikipedia.org/w/api.php",
    # LT wraps in "Infolentel\u0117 albumas" rather than "Infobox album". The first word is
    # the literal "Infolentel\u0117"; what varies and matters is what follows.
    infobox_header_re=re.compile(
        r"\{\{\s*Infolentel\u0117\s+([^\n|}]+)", re.IGNORECASE
    ),
    album_infobox_names=(
        "albumas",
        "muzikinis albumas",
        "garso takelis",
        "studijinis albumas",
        "koncertinis albumas",
    ),
    genre_fields=("\u017eanras", "\u017eanrai", "stilius", "stiliai"),
    artist_fields=("atlik\u0117jas", "atlik\u0117jai", "grup\u0117"),
    album_qualifiers=(
        "albumas",
        "roko albumas",
        "studijinis albumas",
        "koncertinis albumas",
        "garso takelis",
    ),
    qualifier_predicate=_lt_qualifier_predicate,
    album_search_suffixes=("albumas",),
)

LANGUAGE_CONFIGS: dict[str, LangConfig] = {
    "en": EN_CONFIG,
    "lt": LT_CONFIG,
}
DEFAULT_LANG_ORDER: tuple[str, ...] = ("en", "lt")

# Backwards-compat: code reading API_URL expects a string. Default to EN.
API_URL = EN_CONFIG.api_url

# The old module-level constants still exist for callers / tests that imported them.
_ALBUM_QUALIFIERS = EN_CONFIG.album_qualifiers
_ALBUM_INFOBOX_NAMES = EN_CONFIG.album_infobox_names


class WikipediaClient:
    def __init__(
        self,
        cache_path: Path,
        enabled: bool = True,
        langs: tuple[str, ...] = DEFAULT_LANG_ORDER,
    ):
        self.cache_path = cache_path
        self.enabled = enabled
        self.langs = tuple(lang for lang in langs if lang in LANGUAGE_CONFIGS)
        if not self.langs:
            self.langs = DEFAULT_LANG_ORDER
        self._cache: dict[str, dict[str, Any]] = self._load_cache()
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

    def _get(self, params: dict[str, str], cfg: LangConfig | None = None) -> Any:
        """Return raw parsed JSON (dict or list) from the Wikipedia API, or None on error."""
        cfg = cfg or EN_CONFIG
        qp = {"format": "json", "formatversion": "2", **params}
        url = cfg.api_url + "?" + urlencode(qp)
        self._throttle()
        req = Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urlopen(req, timeout=20) as resp:
                data = resp.read()
            return json.loads(data.decode("utf-8"))
        except Exception:
            return None

    def _opensearch(self, query: str, limit: int = 5, cfg: LangConfig | None = None) -> list[str]:
        # opensearch returns a JSON array: [query, titles[], descriptions[], urls[]]
        # It only matches article-title prefixes \u2014 useful for well-formed titles but
        # misses nearly every free-text query. See _search() for the full-text version.
        res = self._get({"action": "opensearch", "search": query, "limit": str(limit)}, cfg=cfg)
        if isinstance(res, list) and len(res) >= 2 and isinstance(res[1], list):
            return [t for t in res[1] if isinstance(t, str)]
        return []

    def _search(self, query: str, limit: int = 10, cfg: LangConfig | None = None) -> list[str]:
        """Full-text page search (list=search). Finds articles regardless of title prefix."""
        res = self._get(
            {"action": "query", "list": "search", "srsearch": query, "srlimit": str(limit)},
            cfg=cfg,
        )
        if not isinstance(res, dict):
            return []
        hits = res.get("query", {}).get("search", [])
        if not isinstance(hits, list):
            return []
        return [h.get("title", "") for h in hits if isinstance(h, dict) and h.get("title")]

    def _fetch_wikitext(self, title: str, cfg: LangConfig | None = None) -> str:
        res = self._get(
            {
                "action": "parse",
                "page": title,
                "prop": "wikitext",
                "redirects": "1",
            },
            cfg=cfg,
        )
        if not isinstance(res, dict):
            return ""
        parsed = res.get("parse", {})
        wt = parsed.get("wikitext", "") if isinstance(parsed, dict) else ""
        if isinstance(wt, dict):
            wt = wt.get("*", "") or ""
        return wt if isinstance(wt, str) else ""

    def genres_for_album(self, artist: str, album: str, year: str = "") -> list[str]:
        """Return parsed album genres from Wikipedia, or [] on miss.

        Tries each configured language in order (default: English, then Lithuanian).
        Within a language, walks candidate article titles from opensearch + full-text
        search and accepts the first whose wikitext exposes an album infobox with a
        non-empty genre field. A single cache entry records the final outcome across
        all languages so we don't re-query the web on repeat runs.
        """
        if not self.enabled or not artist or not album:
            return []
        key = f"wiki|{artist.lower()}|{album.lower()}|{year[:4] if year else ''}"
        cached = self._cache_get(key)
        if cached is not None:
            return list(cached)

        # Generic placeholder artists give no useful signal for verification.
        normalized_artist = _clean_artist_name(artist, album)
        verification_artist = (
            normalized_artist if normalized_artist.lower() not in _GENERIC_ARTIST_WORDS else ""
        )
        artist_tokens = _artist_token_set(verification_artist)

        for lang in self.langs:
            cfg = LANGUAGE_CONFIGS[lang]
            genres = self._lookup_in_lang(cfg, artist, album, artist_tokens)
            if genres:
                self._cache_put(key, genres)
                return genres

        self._cache_put(key, [])
        return []

    def _lookup_in_lang(
        self,
        cfg: LangConfig,
        artist: str,
        album: str,
        artist_tokens: set[str],
    ) -> list[str]:
        """Walk candidate titles for one language and return genres if any candidate hits."""
        candidates = self._candidate_article_titles(artist, album, cfg)
        for title in candidates[:12]:
            wt = self._fetch_wikitext(title, cfg=cfg)
            if not wt or not _is_album_infobox(wt, cfg):
                continue
            if not self._title_passes_artist_check(title, artist_tokens, wt, cfg):
                continue
            genres = parse_infobox_genres(wt, cfg)
            if genres:
                # Translate known LT genre names into their EN equivalents so they
                # merge with the rest of the library's English-tagged albums.
                if cfg.code == "lt":
                    genres = _dedupe_preserve([_translate_lt_genre(g) for g in genres])
                return genres
        return []

    @staticmethod
    def _title_passes_artist_check(
        title: str,
        artist_tokens: set[str],
        wt: str,
        cfg: LangConfig,
    ) -> bool:
        """Return True iff this candidate should be accepted for our query artist.

        Logic:
          - If we have no artist tokens (e.g. query artist was blank or generic),
            accept unconditionally \u2014 title plausibility is our only safeguard.
          - If the article's title contains an album-ish qualifier that signals a
            compilation-style work (soundtrack / mixtape / compilation album / ost /
            live album), skip artist verification: these infoboxes typically list a
            composer or "Various artists" rather than the credited folder-artist.
          - Otherwise require at least one token overlap between our artist and the
            infobox's `artist =` field. If the infobox has no artist field we accept
            (no evidence either way; title plausibility must carry the decision).
        """
        if not artist_tokens:
            return True
        t_lower = title.lower()
        soundtrack_markers = (
            "soundtrack",
            "compilation",
            "mixtape",
            "ost",
            "live album",
            "garso takelis",  # LT: "soundtrack"
        )
        if any(f"({m}" in t_lower or t_lower.endswith(f"({m})") for m in soundtrack_markers):
            return True
        infobox_artist = parse_infobox_artist(wt, cfg)
        if not infobox_artist:
            return True
        ib_tokens = _artist_token_set(infobox_artist)
        if not ib_tokens:
            return True
        return _artist_tokens_overlap(artist_tokens, ib_tokens)

    def _candidate_article_titles(
        self, artist: str, album: str, cfg: LangConfig
    ) -> list[str]:
        """Return an ordered list of candidate article titles to try, for one language.

        Ordering priority (most to least specific):
          1. Titles whose article name contains an album-ish qualifier like
             "(album)" / "(soundtrack)" / "(1987 albumas)" \u2014 these are unambiguous
             album articles.
          2. Titles that contain both the artist name and the album's first word.
          3. Titles that exactly match the album name (punctuation-insensitive).

        Ambiguous plain titles (e.g. "Apollo" which could be deity/film/album) are
        still included but at the bottom so the caller can iterate and pick the one
        that actually has an album infobox.
        """
        clean_artist = _clean_artist_name(artist, album)
        # If "Music / Rockets \u2013 Imperception (1984)" style, strip the leading "Artist - "
        # out of the album field so we search for the real album name, not the artist.
        album_for_clean = album
        if artist.lower() in _GENERIC_ARTIST_WORDS:
            album_for_clean = _extract_album_from_prefixed(album)
        clean_album = _clean_album_title(album_for_clean)
        primary_album = clean_album or album
        primary_artist = clean_artist or artist

        collected: list[str] = []
        seen: set[str] = set()

        def _add(title: str) -> None:
            if not title or title in seen:
                return
            seen.add(title)
            collected.append(title)

        # Stage 1: opensearch album name with each lang-specific suffix ("album" / "albumas").
        os_queries = [primary_album] + [
            f"{primary_album} ({sfx})" for sfx in cfg.album_search_suffixes
        ]
        for q in os_queries:
            for t in self._opensearch(q, limit=5, cfg=cfg):
                if _title_plausible(primary_artist, primary_album, t, cfg) or _title_plausible(
                    artist, album, t, cfg
                ):
                    _add(t)

        # Stage 2: full-text search with lang-specific "album" suffix.
        suffix = cfg.album_search_suffixes[0] if cfg.album_search_suffixes else ""
        queries = [
            f"{primary_album} {primary_artist}",
            f"{primary_artist} {primary_album} {suffix}".rstrip(),
            f"{primary_album} {suffix}".rstrip(),
        ]
        seen_queries: set[str] = set()
        for q in queries:
            qkey = re.sub(r"\s+", " ", q.strip().lower())
            if qkey in seen_queries or not qkey:
                continue
            seen_queries.add(qkey)
            for t in self._search(q, limit=8, cfg=cfg):
                if _title_plausible(primary_artist, primary_album, t, cfg) or _title_plausible(
                    artist, album, t, cfg
                ):
                    _add(t)

        # Re-order: prefer titles that look most like "this artist's album by this name".
        alb_norm = _strip_punct(primary_album)
        art_norm = _strip_punct(primary_artist)
        album_tokens = {_strip_punct(w) for w in primary_album.split() if _strip_punct(w)}

        def _rank(title: str) -> tuple[int, int]:
            """Lower tuple sorts earlier."""
            t_lower = title.lower()
            t_norm = _strip_punct(title)
            title_word_tokens = {_strip_punct(w) for w in title.split() if _strip_punct(w)}
            has_qualifier = _title_has_album_qualifier(title, cfg)
            has_artist = bool(art_norm) and art_norm in t_norm
            exact_album = bool(alb_norm) and alb_norm == t_norm

            # Count extra numeric tokens in the title that weren't in our album name.
            # Penalizes "Apollo 18" / "Apollo 11" when we're looking for "Apollo".
            extra_numbers = sum(
                1 for t in title_word_tokens
                if t and t.isdigit() and t not in album_tokens
            )

            if has_qualifier and has_artist:
                return (0, extra_numbers)
            if has_artist and exact_album:
                return (1, extra_numbers)
            if exact_album:
                return (2, extra_numbers)
            if has_qualifier:
                return (3, extra_numbers)
            return (4, extra_numbers)

        collected.sort(key=_rank)
        return collected


# ---------------------------------------------------------------------------
# Pure helpers (tested directly)
# ---------------------------------------------------------------------------


_WIKI_LINK_RE = re.compile(r"\[\[([^\]|]+?)(?:\|([^\]]+))?\]\]")
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_REF_TAG_RE = re.compile(r"<ref\b[^>]*>.*?</ref>", re.IGNORECASE | re.DOTALL)
_REF_SELFCLOSE_RE = re.compile(r"<ref\b[^/]*/\s*>", re.IGNORECASE)
_TEMPLATE_RE = re.compile(r"\{\{[^{}]*?\}\}", re.DOTALL)
_BRACKETED_EXTRA_RE = re.compile(r"\[([^\[\]]+)\]")


def _strip_punct(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", s.lower())


# Parenthetical qualifiers that describe the edition/remaster rather than the album's
# identity. We strip the entire `(...)` group when its inner text matches these.
_PAREN_QUALIFIER_INNER = (
    r"remaster(?:ed)?(?:\s*\d{2,4})?",
    r"\d{2,4}\s+remaster(?:ed)?",
    r"hi\s?res[^)]*",
    r"expanded\s+edition",
    r"deluxe(?:\s+edition)?",
    r"super\s+deluxe(?:\s+edition)?",
    r"anniversary(?:\s+edition)?",
    r"\d+(?:st|nd|rd|th)\s+anniversary(?:\s+edition)?",
    r"special\s+edition",
    r"collector[''\u2019]?s\s+edition",
    r"bonus\s+(?:tracks?|disc)",
    r"limited\s+edition",
    r"remixed",
    r"reissue",
    r"extended\s+edition",
    r"single\s+version",
    r"album\s+version",
    r"radio\s+edit",
    r"live",
    r"mono",
    r"stereo",
    r"explicit(?:\s+content)?",
    r"clean(?:\s+content)?",
    r"further\s+listening",
    r"pa\s+version",
    r"original\s+motion\s+picture\s+soundtrack",
    r"music\s+from\s+the\s+motion\s+picture",
    r"original\s+soundtrack",
    r"hi[\s-]?res[^)]*",
    r"\d{2,4}[\s-]+\d{2,4}",
)
_PAREN_QUALIFIER_RE = re.compile(
    r"\s*\((?:" + "|".join(_PAREN_QUALIFIER_INNER) + r")\)", re.IGNORECASE
)

# Qualifier keywords \u2014 parens whose inner text is *dominated* by these are stripped
# wholesale. Catches compounds like "60th Anniversary Super Deluxe Edition" that the
# fixed patterns above miss.
_QUALIFIER_KEYWORDS = {
    "remaster", "remastered", "remasters",
    "edition", "deluxe", "super", "anniversary", "expanded",
    "collector", "collectors", "collector's",
    "bonus", "tracks", "track", "disc",
    "limited", "reissue", "remixed", "extended",
    "special", "live", "mono", "stereo", "explicit", "clean",
    "hires", "hi-res", "hi", "res",
    "original", "motion", "picture", "soundtrack", "score", "music", "from", "the",
    "version", "pa", "radio", "single", "album",
    "further", "listening",
    "ost", "inst", "instrumental", "bootleg", "series",
}
_QUALIFIER_NUMBER_SUFFIXES = {"st", "nd", "rd", "th"}
_BRACKETED_TAG_RE = re.compile(r"\s*\[[^\]]+\]")


def _parens_are_qualifiers(inner: str) -> bool:
    """Return True if the parenthesized text is composed mostly of edition/qualifier words."""
    tokens = re.findall(r"[A-Za-z0-9']+", inner.lower())
    if not tokens:
        return False
    quals = 0
    for tok in tokens:
        if tok in _QUALIFIER_KEYWORDS:
            quals += 1
            continue
        # "60th", "50th", etc.
        m = re.fullmatch(r"(\d+)(st|nd|rd|th)", tok)
        if m and m.group(2) in _QUALIFIER_NUMBER_SUFFIXES:
            quals += 1
            continue
        # Pure year like "1984" or resolution tag "192", "24", etc.
        if tok.isdigit():
            quals += 1
            continue
    return quals / len(tokens) >= 0.6


_PARENS_CAPTURE_RE = re.compile(r"\s*\(([^()]*)\)")
_TRAILING_PHRASE_RES = (
    # "- The Bootleg Series, Vol. 3" / similar ornament after a dash or comma.
    re.compile(r"\s*[-\u2013\u2014,]\s+the\s+bootleg\s+series.*$", re.IGNORECASE),
    re.compile(r"\s*[-\u2013\u2014,]\s+further\s+listening.*$", re.IGNORECASE),
    # Note: we *don't* strip trailing "Original Soundtrack" etc. because those are
    # part of the album's identity when not enclosed in parentheses (e.g. "Kill Bill
    # Vol. 1 Original Soundtrack"). The parens form is handled by _PAREN_QUALIFIER_RE.
)
_DISC_SUFFIX_RE = re.compile(
    r"\s*(?:\(|[-\u2013])\s*(?:disc|disk|diskas|cd|lp|side)\s*\d+\)?\s*$",
    re.IGNORECASE,
)
_EMBEDDED_DISC_RE = re.compile(r"\((?:disc|disk|diskas|cd|lp|side)\s*\d+\)", re.IGNORECASE)


def _clean_album_title(album: str) -> str:
    """Strip non-identifying edition/disc/bracket suffixes from an album title.

    Leaves the album's intrinsic name untouched. Empty `()` groups produced by the
    strip are removed so they don't pollute downstream search queries.
    """
    s = album
    # Remove parenthesized edition qualifiers and [bracketed] source tags.
    s = _PAREN_QUALIFIER_RE.sub("", s)
    # Catch compound qualifier parens ("60th Anniversary Super Deluxe Edition", etc.)
    # by stripping any `(...)` group whose content is mostly qualifier keywords.
    s = _PARENS_CAPTURE_RE.sub(
        lambda m: "" if _parens_are_qualifiers(m.group(1)) else m.group(0),
        s,
    )
    s = _BRACKETED_TAG_RE.sub("", s)
    # Remove trailing year-in-parentheses markers like " (1984)".
    s = re.sub(r"\s*\(\d{4}(?:,\s*\d{4})?\)\s*", " ", s)
    # Remove trailing ornamentation phrases ("- The Bootleg Series, Vol. 3", etc.).
    for rx in _TRAILING_PHRASE_RES:
        s = rx.sub("", s)
    # Remove disc markers.
    s = _EMBEDDED_DISC_RE.sub(" ", s)
    s = _DISC_SUFFIX_RE.sub("", s)
    s = re.sub(r"\(\s*\)", "", s)
    s = re.sub(r"\s+", " ", s).strip(" -\u2013\u2014,")
    return s


# When the scan puts "Music" / "VA" / "Various Artists" in the artist field, the real
# artist often appears inside the album name before a " - " separator.
_GENERIC_ARTIST_WORDS = {"music", "va", "various", "various artists", "unknown", "unknown artist"}


def _clean_artist_name(artist: str, album: str = "") -> str:
    """Normalize an artist string for Wikipedia search.

    - For "Artist A, Artist B, Artist C" collaborations, keep only Artist A.
    - Strip a stray " - Album Name" suffix that sometimes leaks into the artist field
      when a folder regex misfires.
    - If the artist is a generic placeholder ("Music", "VA", etc.) and the album name
      has an "Artist - Album" pattern, extract the real artist from there.
    """
    parts = re.split(r"[,/&]| and | feat\.? | with ", artist, flags=re.IGNORECASE)
    first = parts[0].strip() if parts else artist
    first = re.sub(r"\s[-\u2013\u2014]\s.*$", "", first).strip()

    if first.lower() in _GENERIC_ARTIST_WORDS and album:
        m = re.match(r"^\s*(?P<art>[^-\u2013\u2014]+?)\s[-\u2013\u2014]\s", album)
        if m:
            extracted = m.group("art").strip()
            if extracted:
                return extracted
    return first


_STOPWORDS = {"the", "a", "an", "and", "of", "in", "on", "for", "de", "la", "el", "le", "los", "las"}


def _artist_tokens_overlap(a: set[str], b: set[str]) -> bool:
    """Return True if two artist token sets share a name, allowing morphological variation.

    Direct set intersection catches exact matches. For languages with declension (e.g.
    Lithuanian "Antis" / genitive "Anties"; "Foje" / "Foj\u0117") we additionally accept
    token pairs that share a common prefix long enough to identify the same stem. The
    threshold \u2014 4 characters AND at least 70% of the shorter token \u2014 is tight enough
    to avoid "Foo" / "Football" matching but loose enough for typical LT inflections.
    """
    if a & b:
        return True
    for x in a:
        if len(x) < 4:
            continue
        for y in b:
            if len(y) < 4:
                continue
            common = _common_prefix_len(x, y)
            shorter = min(len(x), len(y))
            if common >= 4 and common / shorter >= 0.7:
                return True
    return False


def _common_prefix_len(a: str, b: str) -> int:
    n = min(len(a), len(b))
    i = 0
    while i < n and a[i] == b[i]:
        i += 1
    return i


def _artist_token_set(artist: str) -> set[str]:
    """Break an artist field into a lowercase token set suitable for overlap testing.

    Splits on commas / slashes / ampersands / " and " / " feat. " etc., then tokenizes
    each component by whitespace and punctuation. Drops common stopwords and 1-char
    tokens. Used to verify a Wikipedia infobox's artist matches our query artist.
    """
    if not artist:
        return set()
    components = re.split(r"[,/&]| and | feat\.? | with |\s+featuring\s+", artist, flags=re.IGNORECASE)
    toks: set[str] = set()
    for c in components:
        for t in re.findall(r"[A-Za-z0-9]+", c.lower()):
            if len(t) <= 1 or t in _STOPWORDS:
                continue
            toks.add(t)
    return toks


def _extract_album_from_prefixed(album: str) -> str:
    """If the album string is "Artist - Album ..." return the "Album ..." portion, else input."""
    m = re.match(r"^\s*[^-\u2013\u2014]+?\s[-\u2013\u2014]\s(?P<rest>.+)$", album)
    return m.group("rest").strip() if m else album


def _title_matches(
    artist: str, album: str, title: str, cfg: LangConfig | None = None
) -> bool:
    """Strict check: the article title should look like an album article for (artist, album).

    Accept rules (any one is sufficient):
      - title contains the artist AND (an album qualifier OR the exact album name).
      - title is *identical* to the album name, up to punctuation \u2014 this happens for
        un-disambiguated Wikipedia articles like "Tattoo You" or "Skinty Fia".

    The downstream `_is_album_infobox` check is the safety net that rejects bands / songs
    that happen to share a title with the sought album.

    For collaboration credits like "Brian Eno, Daniel Lanois, Roger Eno" we consider the
    artist to match if *any* of the comma-separated names appears in the title.
    """
    cfg = cfg or EN_CONFIG
    alb_norm = _strip_punct(album)
    t_norm = _strip_punct(title)

    first_album_word = _strip_punct(album.split()[0]) if album.split() else ""
    title_tokens = {_strip_punct(w) for w in title.split() if _strip_punct(w)}
    title_tokens |= {
        _strip_punct(w) for w in re.split(r"[\s()\[\],:;/\-\u2013\u2014]+", title) if _strip_punct(w)
    }
    if first_album_word and first_album_word not in title_tokens:
        return False

    if alb_norm and alb_norm == t_norm:
        return True

    artist_candidates = [a.strip() for a in re.split(r"[,/&]| and | feat\.? | with ", artist, flags=re.IGNORECASE) if a.strip()]
    if not artist_candidates:
        artist_candidates = [artist]
    has_artist = any(_strip_punct(a) and _strip_punct(a) in t_norm for a in artist_candidates)

    has_qualifier = _title_has_album_qualifier(title, cfg)
    exact_album = bool(alb_norm) and alb_norm in t_norm

    if has_artist and (has_qualifier or exact_album):
        return True
    if exact_album and has_qualifier:
        return True
    return False


def _title_plausible(
    artist: str, album: str, title: str, cfg: LangConfig | None = None
) -> bool:
    """Looser gate than `_title_matches`: pick candidates worth fetching wikitext for.

    The caller verifies each candidate's wikitext contains an album-style infobox, so we
    can afford to be permissive here and let a few false positives through.

    Accept a title if ANY of:
      - it exactly matches the album (punctuation-insensitive).
      - it ends with a language-appropriate album qualifier ("(album)", "(soundtrack)",
        "(1987 albumas)", etc.).
      - it contains both the album's first significant word AND the artist (or one of
        the comma-split artist components).
    """
    cfg = cfg or EN_CONFIG
    if not title:
        return False
    alb_norm = _strip_punct(album)
    t_norm = _strip_punct(title)
    if not t_norm:
        return False

    album_tokens = [_strip_punct(w) for w in album.split() if _strip_punct(w)]
    if not album_tokens:
        return False
    first_album_word = album_tokens[0]

    # Album's first significant word must appear as a *token* in the title (not just a
    # substring): we don't want "Voyage" to match "Voyager", but we do want "Apollo" to
    # match "Apollo: Atmospheres and Soundtracks".
    title_tokens = {_strip_punct(w) for w in title.split() if _strip_punct(w)}
    # Also allow parenthesized split: "Tattoo You (Rolling Stones album)" \u2192 tokens include "tattoo", "you".
    title_tokens |= {_strip_punct(w) for w in re.split(r"[\s()\[\],:;/\-\u2013\u2014]+", title) if _strip_punct(w)}
    if first_album_word not in title_tokens:
        return False

    if alb_norm and alb_norm == t_norm:
        return True

    if _title_has_album_qualifier(title, cfg):
        return True

    artist_candidates = [
        _strip_punct(a) for a in re.split(r"[,/&]| and | feat\.? | with ", artist, flags=re.IGNORECASE)
        if _strip_punct(a)
    ]
    if not artist_candidates:
        artist_candidates = [_strip_punct(artist)]
    has_artist = any(a and a in t_norm for a in artist_candidates)

    # "Miles at the Fillmore \u2013 Miles Davis 1970: The Bootleg Series Vol. 3" \u2014 title contains
    # both the artist and several album tokens, so accept.
    if has_artist:
        tokens_in_title = sum(1 for tok in album_tokens if tok and tok in t_norm)
        if tokens_in_title >= min(2, len(album_tokens)):
            return True

    if alb_norm and alb_norm in t_norm:
        return True

    return False


def _title_has_album_qualifier(title: str, cfg: LangConfig) -> bool:
    """Return True if `title` ends with a language-appropriate album qualifier.

    Catches both the static `(album)` / `(soundtrack)` / `(albumas)` forms and the
    Lithuanian dynamic form `(1987 albumas)` via `cfg.qualifier_predicate`.
    """
    t_lower = title.lower()
    for q in cfg.album_qualifiers:
        if f"({q}" in t_lower or t_lower.endswith(f"({q})"):
            return True
    if cfg.qualifier_predicate and cfg.qualifier_predicate(t_lower):
        return True
    return False


def _is_album_infobox(wt: str, cfg: LangConfig | None = None) -> bool:
    """Return True iff the wikitext opens with an album-family infobox template.

    The template name is language-specific (`Infobox album` vs `Infolentel\u0117 albumas`),
    as is the set of accepted variants (studio / live / soundtrack / EP / mixtape).
    """
    cfg = cfg or EN_CONFIG
    m = cfg.infobox_header_re.search(wt)
    if not m:
        return False
    name = m.group(1).strip().lower()
    return any(name.startswith(x) for x in cfg.album_infobox_names)


def _split_top_level_pipes(s: str) -> list[str]:
    """Split on `|` but only at the top level (not inside [[...]], {{...}}, <...>)."""
    out: list[str] = []
    depth_sq = 0  # [[ ]]
    depth_br = 0  # {{ }}
    depth_ang = 0  # < >
    buf: list[str] = []
    i = 0
    while i < len(s):
        ch = s[i]
        nxt = s[i + 1] if i + 1 < len(s) else ""
        if ch == "[" and nxt == "[":
            depth_sq += 1
            buf.append(ch)
            buf.append(nxt)
            i += 2
            continue
        if ch == "]" and nxt == "]":
            if depth_sq > 0:
                depth_sq -= 1
            buf.append(ch)
            buf.append(nxt)
            i += 2
            continue
        if ch == "{" and nxt == "{":
            depth_br += 1
            buf.append(ch)
            buf.append(nxt)
            i += 2
            continue
        if ch == "}" and nxt == "}":
            if depth_br > 0:
                depth_br -= 1
            buf.append(ch)
            buf.append(nxt)
            i += 2
            continue
        if ch == "<":
            depth_ang += 1
            buf.append(ch)
            i += 1
            continue
        if ch == ">":
            if depth_ang > 0:
                depth_ang -= 1
            buf.append(ch)
            i += 1
            continue
        if ch == "|" and depth_sq == 0 and depth_br == 0 and depth_ang == 0:
            out.append("".join(buf))
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    out.append("".join(buf))
    return out


def _expand_templates(s: str) -> str:
    """Collapse `{{hlist|a|b}}`, `{{flatlist|...}}`, `{{nowrap|x}}`, etc. into a comma-separated string.

    Runs repeatedly to handle nested templates from the inside out.
    """
    template_re = re.compile(r"\{\{([^{}]*?)\}\}", re.DOTALL)

    def _replace(m: re.Match[str]) -> str:
        inner = m.group(1)
        parts = _split_top_level_pipes(inner)
        if not parts:
            return ""
        name = parts[0].strip().lower()
        values = [p.strip() for p in parts[1:] if p.strip()]
        if name in ("hlist", "flatlist", "ublist", "unbulleted list", "plainlist", "bulleted list"):
            return ", ".join(values)
        if name in ("nowrap", "small", "italic title", "avoid wrap", "nobr"):
            return ", ".join(values)
        # Unknown template: keep inner values as a best-effort fallback.
        return ", ".join(values)

    prev: str | None = None
    while prev != s:
        prev = s
        s = template_re.sub(_replace, s)
    return s


def _clean_genre_field(raw: str) -> str:
    s = _REF_TAG_RE.sub(" ", raw)
    s = _REF_SELFCLOSE_RE.sub(" ", s)
    s = _expand_templates(s)
    s = _HTML_TAG_RE.sub(" ", s)
    s = _BRACKETED_EXTRA_RE.sub(lambda m: "" if m.group(1).strip().isdigit() else m.group(0), s)
    return s


def _extract_infobox_field(
    wt: str,
    field_names: tuple[str, ...],
    cfg: LangConfig | None = None,
) -> str:
    """Return the verbatim value string for the first matching infobox field.

    Walks from the opening template header (language-specific, e.g. `{{Infobox ...}}`
    or `{{Infolentel\u0117 ...}}`) character-by-character, tracking brace depth so we don't
    bleed into later templates. Stops at the next `| <name> =` at the infobox level.
    """
    cfg = cfg or EN_CONFIG
    infobox_start = cfg.infobox_header_re.search(wt)
    if not infobox_start:
        return ""

    # Walk characters tracking {{ }} depth so we capture just this template.
    start = infobox_start.start()
    depth = 0
    end = len(wt)
    i = start
    while i < len(wt):
        if wt[i:i + 2] == "{{":
            depth += 1
            i += 2
            continue
        if wt[i:i + 2] == "}}":
            depth -= 1
            i += 2
            if depth == 0:
                end = i
                break
            continue
        i += 1
    body = wt[start:end]

    # Split into fields on top-level '|'. The first segment is the template name, rest are fields.
    parts = _split_top_level_pipes(body.strip("{}"))
    for part in parts[1:]:
        if "=" not in part:
            continue
        k, _, v = part.partition("=")
        key = k.strip().lower()
        if key in field_names:
            return v.strip()
    return ""


def parse_infobox_genres(wt: str, cfg: LangConfig | None = None) -> list[str]:
    """Extract and parse the genre field from an album infobox.

    Field name is language-specific (`genre` in EN, `\u017eanras` in LT). Returns a
    de-duplicated list of genres, original case preserved.
    """
    cfg = cfg or EN_CONFIG
    raw = _extract_infobox_field(wt, cfg.genre_fields, cfg=cfg)
    if not raw:
        return []

    cleaned = _clean_genre_field(raw)

    def _split(s: str) -> list[str]:
        parts = re.split(r"[,\n]+|\s\*\s|\s\u2022\s|\u2022", s)
        return [p.strip() for p in parts if p and p.strip()]

    pieces = _split(cleaned)
    out: list[str] = []
    seen: set[str] = set()
    for piece in pieces:
        displayed: list[str] = []
        for m2 in _WIKI_LINK_RE.finditer(piece):
            target = m2.group(1).strip()
            disp = (m2.group(2) or target).strip()
            if disp:
                displayed.append(disp)
        if displayed:
            for name in displayed:
                cleaned_name = _clean_name(name)
                if cleaned_name and cleaned_name.lower() not in seen:
                    seen.add(cleaned_name.lower())
                    out.append(cleaned_name)
        else:
            cleaned_name = _clean_name(piece)
            if cleaned_name and cleaned_name.lower() not in seen:
                seen.add(cleaned_name.lower())
                out.append(cleaned_name)
    return out[:6]


def parse_infobox_artist(wt: str, cfg: LangConfig | None = None) -> str:
    """Extract the artist from an album infobox as plain text, or "" if absent.

    Field name is language-specific (`artist` in EN, `Atlik\u0117jas` in LT). Used to
    disambiguate candidate articles with shared titles: if we're looking for Brian
    Eno's "Apollo" and a candidate's infobox artist is "They Might Be Giants", we
    reject it even though the infobox is an album infobox.
    """
    cfg = cfg or EN_CONFIG
    raw = _extract_infobox_field(wt, cfg.artist_fields, cfg=cfg)
    if not raw:
        return ""
    cleaned = _clean_genre_field(raw)
    # Collect both wiki-link display text and any leftover plain text.
    displayed: list[str] = []
    for m in _WIKI_LINK_RE.finditer(cleaned):
        target = m.group(1).strip()
        disp = (m.group(2) or target).strip()
        if disp:
            displayed.append(disp)
    if not displayed:
        # No links: treat whole cleaned string as the artist.
        displayed = [cleaned]
    text = " ".join(displayed)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _clean_name(name: str) -> str:
    s = name.strip()
    # Drop leading/trailing punctuation and stray brackets.
    s = re.sub(r"^[\s\*\-\u2022\(\[\|]+|[\s\*\-\u2022\)\]\|]+$", "", s)
    # Collapse whitespace.
    s = re.sub(r"\s+", " ", s)
    # Drop anything that's pure punctuation / too short / obviously not a genre.
    if len(s) < 2:
        return ""
    if s.lower() in {"etc.", "etc", "various", "and", "&"}:
        return ""
    return s
