"""CLI entry point: `python -m src scan ...` / `python -m src plan ...`."""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from .dotenv import load_dotenv
from .lastfm import LastFmClient
from .musicbrainz import MBClient
from .planner import PlannerConfig, load_candidates_filter, plan_tapes
from .report import read_albums_json, write_plan
from .scan import scan_library


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="music",
        description="Scan a music library and plan cassette/reel tape assignments.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    scan = sub.add_parser("scan", help="Scan a music library and emit albums.json + report.md")
    scan.add_argument("root", type=Path, help="Root library directory (e.g. H:\\Music)")
    scan.add_argument("-o", "--out", type=Path, default=Path("out"), help="Output directory")
    scan.add_argument(
        "--cache-dir",
        type=Path,
        default=Path(".cache"),
        help=(
            "Directory for the four on-disk caches (.scan-cache, .mb-cache, "
            ".lastfm-cache, .wiki-cache). Defaults to ./.cache. Sharing a "
            "single cache across runs avoids repeating slow external lookups."
        ),
    )
    scan.add_argument("--no-progress", action="store_true", help="Disable progress bar")
    scan.add_argument("--workers", type=int, default=None, help="Parallel workers (default: 2x CPU, capped at 16)")
    scan.add_argument("--no-enrich", action="store_true", help="Skip online genre enrichment for albums with empty GENRE tags")
    scan.add_argument(
        "--lastfm-key",
        default=None,
        help="Last.fm API key for genre enrichment (falls back to $LASTFM_API_KEY). Optional; MusicBrainz works without a key.",
    )

    plan = sub.add_parser("plan", help="Plan tapes from a previously scanned albums.json")
    plan.add_argument("albums_json", type=Path, help="Path to albums.json produced by scan")
    plan.add_argument("-o", "--out", type=Path, default=Path("out"), help="Output directory")
    plan.add_argument(
        "--cache-dir",
        type=Path,
        default=Path(".cache"),
        help=(
            "Directory for the .mb-cache and .lastfm-cache files. Defaults to "
            "./.cache. Reusing a single cache directory avoids repeating "
            "MusicBrainz and Last.fm lookups across runs (with vs without "
            "--trim, different --max-slack values, etc.)."
        ),
    )
    plan.add_argument("--candidates", type=Path, default=None, help="Optional list of album paths to consider (one per line)")
    plan.add_argument("--no-musicbrainz", action="store_true", help="Skip MusicBrainz lookups for filler suggestions")
    plan.add_argument("--no-lastfm", action="store_true", help="Skip Last.fm lookups for filler suggestions (only used if --lastfm-key or LASTFM_API_KEY is set)")
    plan.add_argument("--lastfm-key", default=None, help="Last.fm API key (falls back to $LASTFM_API_KEY)")
    plan.add_argument("--buffer-sec", type=int, default=60, help="Headroom kept per tape side (seconds)")
    plan.add_argument(
        "--allow-overlapping-sides",
        action="store_true",
        help=(
            "Let Side B share the tape's total budget with Side A instead of fitting "
            "within its own physical side. Default is strict per-side fit, which "
            "matches how cassettes physically work."
        ),
    )
    plan.add_argument(
        "--max-slack-small-sec",
        type=int,
        default=10 * 60,
        help=(
            "Max per-side unused time (seconds) for tape sides up to 45 min. "
            "Pairings that exceed this on either side are refused and the album "
            "falls back to solo placement. Default: 600 (10 minutes)."
        ),
    )
    plan.add_argument(
        "--max-slack-large-sec",
        type=int,
        default=15 * 60,
        help=(
            "Max per-side unused time (seconds) for tape sides longer than 45 min. "
            "Default: 900 (15 minutes)."
        ),
    )
    plan.add_argument(
        "--trim",
        choices=["off", "unplaced", "all"],
        default="unplaced",
        help=(
            "Trim over-length deluxe / expanded / anniversary editions to a "
            "fittable 'core' length. 'off' = never trim. 'unplaced' (default) = "
            "only try to rescue albums that would otherwise be unplaceable. "
            "'all' = trim every over-length reissue before planning, even if it "
            "would fit a bigger tape as-is (gives the planner more flexibility)."
        ),
    )
    plan.add_argument("--no-progress", action="store_true", help="Disable progress bar")
    plan.add_argument("--skip-external", action="store_true", help="Skip all external filler lookups (MB + Last.fm); only use local pairings and search-URL fallback")

    return parser


def cmd_scan(args: argparse.Namespace) -> int:
    root = args.root
    if not root.exists():
        print(f"error: root not found: {root}", file=sys.stderr)
        return 2
    out = args.out
    lastfm_key = args.lastfm_key or os.environ.get("LASTFM_API_KEY", "").strip() or None
    albums = scan_library(
        root,
        out,
        progress=not args.no_progress,
        workers=args.workers,
        enrich=not args.no_enrich,
        lastfm_key=lastfm_key,
        cache_dir=args.cache_dir,
    )
    print(f"Scanned {len(albums)} albums -> {out / 'albums.json'}")
    print(f"Report: {out / 'report.md'}")
    return 0


def cmd_plan(args: argparse.Namespace) -> int:
    albums_path: Path = args.albums_json
    if not albums_path.exists():
        print(f"error: albums.json not found: {albums_path}", file=sys.stderr)
        return 2

    albums = read_albums_json(albums_path)
    if args.candidates:
        if not args.candidates.exists():
            print(f"error: candidates file not found: {args.candidates}", file=sys.stderr)
            return 2
        albums = load_candidates_filter(args.candidates, albums)
        print(f"Filtered to {len(albums)} candidate albums")

    out = args.out
    out.mkdir(parents=True, exist_ok=True)
    cache_dir: Path = args.cache_dir
    cache_dir.mkdir(parents=True, exist_ok=True)
    use_mb = not args.no_musicbrainz and not args.skip_external
    use_lf = not args.no_lastfm and not args.skip_external
    mb = MBClient(cache_path=cache_dir / ".mb-cache.json", enabled=use_mb)
    lastfm_key = args.lastfm_key or os.environ.get("LASTFM_API_KEY", "").strip() or None
    lastfm = None
    if use_lf and lastfm_key:
        lastfm = LastFmClient(api_key=lastfm_key, cache_path=cache_dir / ".lastfm-cache.json")
    cfg = PlannerConfig(
        buffer_sec=args.buffer_sec,
        allow_musicbrainz=use_mb,
        allow_lastfm=use_lf,
        strict_side_fit=not args.allow_overlapping_sides,
        max_slack_small_sec=args.max_slack_small_sec,
        max_slack_large_sec=args.max_slack_large_sec,
        trim_mode=args.trim,
    )

    assignments, unplaced = plan_tapes(
        albums, mb=mb, lastfm=lastfm, cfg=cfg, progress=not args.no_progress
    )
    plan_path = out / "plan.md"
    write_plan(plan_path, assignments, unplaced)

    # Quick summary of how each assignment was resolved.
    counts: dict[str, int] = {}
    for asn in assignments:
        counts[asn.match_kind or "?"] = counts.get(asn.match_kind or "?", 0) + 1
    summary_bits = [f"{k}={v}" for k, v in sorted(counts.items(), key=lambda kv: -kv[1])]
    print(f"Wrote {len(assignments)} assignments, {len(unplaced)} unplaced -> {plan_path}")
    if summary_bits:
        print("By match kind: " + ", ".join(summary_bits))
    return 0


def main(argv: list[str] | None = None) -> int:
    # Load .env from CWD first, so LASTFM_API_KEY (and friends) can live there.
    load_dotenv(Path.cwd() / ".env")
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.cmd == "scan":
        return cmd_scan(args)
    if args.cmd == "plan":
        return cmd_plan(args)
    parser.print_help()
    return 1
