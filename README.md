# Music Tape Planner

Scan a local music library and plan which albums go onto which cassette / reel-to-reel tape, pairing B-sides by genre with a fallback ladder: local library (tight genre) → local library (relaxed genre) → MusicBrainz suggestions → Last.fm suggestions → RateYourMusic / Discogs search URLs.

## Tape sizes

| Length | Split | Notes |
| ------ | ----- | ----- |
| 46 min | no    | cassette |
| 54 min | no    | cassette |
| 60 min | 30+30 | cassette / reel |
| 70 min | 35+35 | cassette |
| 90 min | 45+45 | cassette / reel |
| 120 min| 60+60 | reel |

A mid-album side flip is allowed: any album whose total duration is `<= tape length` is a valid single-tape candidate, even if the album crosses the midpoint.

## Install

Python 3.10+ is required (tested with 3.13). Recommended: isolated virtual environment.

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
```

From here on, either keep the venv activated or invoke the interpreter directly with `.\.venv\Scripts\python.exe`.

System dependency: [`ffprobe`](https://ffmpeg.org/download.html) on `PATH` (needed for DFF / ISO / DSF / VOB durations; FLAC / MP3 / WAV fall back to `mutagen`).

## Usage

All commands below assume the venv interpreter (activated venv, or `.\.venv\Scripts\python.exe -m src ...`).

### Part A — scan library

```powershell
python -m src scan H:\Music -o out
```

By default, the scanner also **enriches missing `GENRE` tags** via three cascading providers:

1. **MusicBrainz** — no key required (1 req/sec rate limit).
2. **Last.fm** — key optional, see below.
3. **Wikipedia (English, then Lithuanian)** — no key required; parses the `| genre = ...` field from album infoboxes. Strict disambiguation: walks multiple candidate articles and accepts only those whose `{{Infobox album|soundtrack|EP|...}}` template's `artist =` field matches the album's credited artist. For Lithuanian albums with no English article, the client falls back to `lt.wikipedia.org` and its `{{Infolentelė albumas}}` template (field names: `Žanras`, `Atlikėjas`), with known LT genre terms translated to their English equivalents (`Rokas → Rock`, `Thrash metalas → Thrash metal`, etc.) so they merge cleanly with the rest of the library.

All three providers cache responses on disk, so subsequent runs only query albums whose tags still look empty. For a cold scan of a ~500-album library, enrichment adds ~10 minutes. Re-scans are near-instant because the scan cache keeps per-folder records and the provider caches keep lookup results.

To supply a Last.fm API key (free, register at [last.fm/api/account/create](https://www.last.fm/api/account/create)), use any of these, in order of precedence:

1. CLI flag: `--lastfm-key your-key-here`
2. Shell env var: `$env:LASTFM_API_KEY = "your-key-here"`
3. A `.env` file in the directory you run the script from, containing:

   ```
   LASTFM_API_KEY=your-key-here
   ```

`.env` is gitignored so you won't accidentally commit it. Skip enrichment entirely with `--no-enrich`. Last.fm is also used by `plan` as a fallback for external B-side suggestions if MusicBrainz returns nothing.

Produces:

- `out/albums.json` — canonical album records (path, artist, album, year, genres, duration in seconds, format, warnings).
- `out/report.md` — albums bucketed by smallest tape that fits them, plus a B-side candidates list (≤ 45 min).
- `out/.scan-cache.json` — per-album cache keyed by (path, mtime, size). Delete to force a full re-scan.
- `out/.mb-cache.json`, `out/.lastfm-cache.json`, `out/.wiki-cache.json` — genre-lookup caches (24 h TTL for MB, 30 d for Last.fm and Wikipedia).

### Part B — plan tapes

```powershell
# all albums
python -m src plan out/albums.json -o out

# only a curated subset (one album folder path per line, blank/`#` lines ignored)
python -m src plan out/albums.json -o out --candidates my_picks.txt
```

Produces:

- `out/plan.md` — one section per tape assignment. The pairing algorithm tries, in order, a local tight-genre match, a local relaxed-genre match, MusicBrainz suggestions, Last.fm suggestions, and finally prints RYM / Discogs search URLs.
- `out/.mb-cache.json`, `out/.lastfm-cache.json` — response caches.

Pass `--skip-external` if you want only local library pairings (no MB / Last.fm calls). A `Planning:` progress bar will show which album is currently being looked up externally.

## Library layout expected

Leaf album folders named `Artist - Album Name (Year) [Source]`, e.g. `AC-DC - Back In Black (1980) [Tidal 24-48]`. Multi-disc albums (child folders like `CD1`, `Disc 2`) are summed into one record. These top-level folders are skipped:

- `# Clips`
- `# Mixes and compilations`
- `# Random`
- `# Recordings & transfers`

`# Soundtracks` is kept.
# whats-on-tape
