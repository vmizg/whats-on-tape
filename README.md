# What's On Tape?

Scan a local music library and plan which albums go onto which cassette / reel-to-reel tape. B-sides are paired by genre using a fallback ladder: local library (tight genre) → local library (relaxed genre, same parent bucket) → MusicBrainz suggestions → Last.fm suggestions → RateYourMusic / Discogs search URLs.

## Tape sizes

| Length | Split | Notes |
| ------ | ----- | ----- |
| 46 min | no    | cassette |
| 54 min | no    | cassette |
| 60 min | 30+30 | cassette / reel |
| 70 min | 35+35 | cassette |
| 90 min | 45+45 | cassette / reel |
| 120 min| 60+60 | reel |

**Strict per-side fit** is the default on split tapes: each side must physically hold its album(s), no spilling across the midpoint (which matches how cassettes actually work). Override with `--allow-overlapping-sides` to let Side B share Side A's remaining budget.

**Stretch tolerance** — each tape has a small per-side over-capacity allowance (typically 120–300 s) so a 31-min album fits a 30-min side, a 47-min album fits a 45-min side, and so on. Tweakable in `src/tapes.py:STRETCH_TOLERANCE_SEC`.

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

By default, the scanner also **enriches album genres** via three cascading providers. Enrichment fires in two cases:

1. **Empty genre tag** — the album has no `GENRE` metadata at all.
2. **Vague genre tag** — the only tags are generic terms like `Rock`, `Pop`, `Electronic`, `Music`, etc. In this case, the clarifying (more specific) tags are **prepended** to the existing list so nothing is lost.

The three providers, tried in order:

1. **MusicBrainz** — no key required (1 req/sec rate limit).
2. **Last.fm** — key optional, see below.
3. **Wikipedia (English, then Lithuanian)** — no key required; parses the `| genre = ...` field from album infoboxes. Strict disambiguation: walks multiple candidate articles and accepts only those whose `{{Infobox album|soundtrack|EP|...}}` template's `artist =` field matches the album's credited artist. For Lithuanian albums with no English article, the client falls back to `lt.wikipedia.org` and its `{{Infolentelė albumas}}` template (field names: `Žanras`, `Atlikėjas`), with known LT genre terms translated to their English equivalents (`Rokas → Rock`, `Thrash metalas → Thrash metal`, etc.) so they merge cleanly with the rest of the library.

All three providers cache responses on disk, so subsequent runs only query albums whose tags still look empty or vague. For a cold scan of a ~500-album library, enrichment adds ~10 minutes. Re-scans are near-instant because the scan cache keeps per-folder records and the provider caches keep lookup results.

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

### Part B — plan tapes

```powershell
# all albums, default settings (trim unplaced, strict side fit, stretch tolerance on)
python -m src plan out/albums.json -o out

# only a curated subset (one album folder path per line, blank/`#` lines ignored)
python -m src plan out/albums.json -o out --candidates my_picks.txt

# trim aggressively so every deluxe/expanded edition gets placed on its smallest
# fitting tape by skipping bonus tracks (see "Trim heuristic" below)
python -m src plan out/albums.json -o out --trim all

# local library only, no MusicBrainz / Last.fm calls
python -m src plan out/albums.json -o out --skip-external
```

Produces:

- `out/plan.md` — one section per tape assignment. Each assignment shows the chosen tape, Side A, Side B (or B-side candidates when no confident partner was found), match kind, and per-side slack.

### Trim heuristic

Deluxe / anniversary / expanded editions often run 2–4× the original album length because of demos, alternate mixes, remastered singles, and bonus discs. By default (`--trim unplaced`), the planner tries to rescue any album that would otherwise be unplaceable by:

1. **MusicBrainz canonical-release lookup** — find the original, shortest pressing of the (artist, album) pair and use its duration.
2. **Track-title heuristic** — scan the album folder's per-track titles for bonus markers: `(Demo)`, `(Live)`, `(Alternate Mix)`, `(Extended Version)`, `(Remaster)`, `(B-Side)`, `(Bonus Track)`, `(BBC Session)`, and ~50 more. Sum durations of the non-bonus tracks.

If either approach produces a duration that fits a tape, the album is placed and `plan.md` shows a `Trim:` sub-bullet explaining what was dropped, plus a `Skip these tracks:` list when track titles were identified.

Modes:

- `--trim off` — never trim; over-length albums land in the unplaced section.
- `--trim unplaced` (default) — only try the expensive MB / tag-reading path for unplaceable albums.
- `--trim all` — trim every over-length deluxe reissue before planning, even if it would already fit a bigger tape. Gives the greedy planner more flexibility (a 2:05:00 deluxe version of a 45-min album can end up on a 46-min cassette) but makes `plan.md` reflect an opinionated "core" for every reissue.

**Compilations and live albums are never trimmed** (`Best Of`, `Greatest Hits`, `Pulse (Live)`, `The Essential ...`, soundtracks, bootleg series). There's no meaningful canonical shorter version. These end up in the unplaced section flagged as "consider manual 2-sided split".

### Slack caps

To avoid pairing suggestions with absurd amounts of unused tape (e.g. a 13-min album as Side B of a 45-min side), the planner enforces per-side slack caps on **pairing** decisions (solo placements are unaffected — a short album alone on the smallest fitting tape is always fine):

- `--max-slack-small-sec` (default 600 = 10 min) — for tape sides up to 45 min.
- `--max-slack-large-sec` (default 900 = 15 min) — for tape sides longer than 45 min.

If no local partner keeps both sides under the cap, the album falls back to solo placement on the next-smaller tape or to external lookups.

### Caches

Four on-disk caches live in `.cache/` by default (tweakable with `--cache-dir`):

- `.scan-cache.json` — per-album record keyed by (path, mtime, size). Near-instant re-scans.
- `.mb-cache.json` — MusicBrainz genre searches, genre-for-album lookups, and canonical-release durations (24 h TTL).
- `.lastfm-cache.json` — Last.fm tag-top-albums and album-info (30 d TTL).
- `.wiki-cache.json` — Wikipedia infobox genre extractions (30 d TTL).

Genre-search keys are bucketed to 30-second precision on the duration bounds so near-identical Side A calculations reuse the same cached result — critical for responsiveness, since MusicBrainz is hard-rate-limited to 1 req/sec.

Delete any cache file to force a refetch for just that layer. Delete `.scan-cache.json` to force a full re-scan from scratch.

### Flags cheat sheet

`plan`:

- `-o, --out` — output directory (default `out/`).
- `--cache-dir` — cache directory (default `.cache/`).
- `--candidates PATH` — restrict to album folders listed in PATH.
- `--no-musicbrainz` / `--no-lastfm` / `--skip-external` — turn off external lookups.
- `--lastfm-key KEY` — Last.fm API key (or via `$LASTFM_API_KEY` / `.env`).
- `--buffer-sec N` — per-tape headroom between albums (default 60 s, applied to pairings).
- `--allow-overlapping-sides` — let Side B spill into Side A's remaining budget on split tapes.
- `--max-slack-small-sec` / `--max-slack-large-sec` — per-side slack caps for pairings (defaults 600 / 900).
- `--trim {off,unplaced,all}` — over-length album handling (default `unplaced`).
- `--no-progress` — disable progress bars.

`scan`:

- `-o, --out`, `--cache-dir`, `--no-progress`, `--workers`, `--no-enrich`, `--lastfm-key` — as above.

## Library layout expected

Leaf album folders named `Artist - Album Name (Year) [Source]`, e.g. `AC-DC - Back In Black (1980) [CD]`. The separator accepts ASCII ` - `, en-dash ` – `, and em-dash ` — `. Multi-disc albums (child folders like `CD1`, `Disc 2`, `LP1`) are summed into one record, tolerating helper folders (`VIDEO_TS`, `Artwork`, etc.) that contain no audio. Some top-level folders are skipped — see `SKIP_TOP_PREFIXES` for the full list.
