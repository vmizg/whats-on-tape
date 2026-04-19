"""Markdown and JSON emitters."""
from __future__ import annotations

import json
from pathlib import Path

from .models import Album, Assignment, SideCandidate
from .tapes import TAPES, bucket_label, format_hms, smallest_fitting_tape


def write_albums_json(path: Path, albums: list[Album]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = [a.to_dict() for a in albums]
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def read_albums_json(path: Path) -> list[Album]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return [Album.from_dict(x) for x in data]


def write_scan_report(path: Path, albums: list[Album]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    bucket_order = [bucket_label(t.total_sec) for t in TAPES]
    bucket_order.append(">120 (won't fit)")
    buckets: dict[str, list[Album]] = {b: [] for b in bucket_order}
    for a in albums:
        buckets.setdefault(bucket_label(a.duration_sec), []).append(a)

    lines: list[str] = []
    lines.append("# Music library tape-fit report")
    lines.append("")
    lines.append(f"Total albums: **{len(albums)}**")
    total_sec = sum(a.duration_sec for a in albums)
    lines.append(f"Total runtime: **{format_hms(total_sec)}**")
    lines.append("")

    for bucket in bucket_order:
        group = buckets.get(bucket, [])
        if not group:
            continue
        lines.append(f"## Bucket {bucket} ({len(group)} albums)")
        lines.append("")
        lines.append("| Duration | Artist | Album | Year | Genres | Format |")
        lines.append("|---------:|--------|-------|------|--------|--------|")
        for a in sorted(group, key=lambda x: -x.duration_sec):
            genres = ", ".join(a.genres[:3])
            lines.append(
                f"| {format_hms(a.duration_sec)} | {a.artist} | {a.album} | {a.year} | {genres} | {a.format} |"
            )
        lines.append("")

    # Extra: B-side candidates <= 45 minutes sorted by length
    short_cut = 45 * 60
    short = sorted([a for a in albums if 0 < a.duration_sec <= short_cut], key=lambda x: -x.duration_sec)
    if short:
        lines.append(f"## B-side candidates (<= 45:00, sorted long-to-short) \u2014 {len(short)} albums")
        lines.append("")
        lines.append("| Duration | Artist | Album | Year | Genres |")
        lines.append("|---------:|--------|-------|------|--------|")
        for a in short:
            genres = ", ".join(a.genres[:3])
            lines.append(f"| {format_hms(a.duration_sec)} | {a.artist} | {a.album} | {a.year} | {genres} |")
        lines.append("")

    # Warnings summary
    with_warn = [a for a in albums if a.warnings]
    if with_warn:
        lines.append(f"## Warnings ({len(with_warn)} albums)")
        lines.append("")
        for a in with_warn:
            lines.append(f"- **{a.display}** \u2014 {a.path}")
            for w in a.warnings:
                lines.append(f"  - {w}")
        lines.append("")

    path.write_text("\n".join(lines), encoding="utf-8")


def _format_side_duration(album: Album, original_sec: int) -> str:
    """Render an album's length, noting the original (pre-trim) length when trimmed."""
    if original_sec and original_sec > album.duration_sec:
        return f"{format_hms(album.duration_sec)} (trimmed from {format_hms(original_sec)})"
    return format_hms(album.duration_sec)


def _format_skip_labels(labels: list[str], max_shown: int = 8) -> str:
    """Compact human-readable list of skipped track labels."""
    if not labels:
        return ""
    shown = labels[:max_shown]
    rest = len(labels) - len(shown)
    body = "; ".join(shown)
    if rest > 0:
        body += f"; \u2026 ({rest} more)"
    return body


def _format_inventory_section(
    inventory: dict[str, int],
    counts: dict[str, int],
) -> list[str]:
    """Render the 'Tape inventory usage' section for plan.md.

    Shows one row per tape size that was either configured (has a cap) or
    actually used. '-' in the Cap column means "unlimited" (no cap configured).
    """
    rows: list[tuple[str, int, str, str]] = []
    # Iterate TAPES so order matches the rest of the report (smallest -> largest).
    for t in TAPES:
        cap = inventory.get(t.name)
        used = counts.get(t.name, 0)
        if cap is None and used == 0:
            continue
        cap_str = "-" if cap is None else str(cap)
        if cap is None:
            status = ""
        elif cap == 0:
            status = "disabled"
        elif used > cap:
            status = "OVER CAP"
        elif used == cap:
            status = "at cap"
        else:
            status = f"{cap - used} left"
        rows.append((t.name, used, cap_str, status))
    if not rows:
        return []
    lines: list[str] = []
    lines.append("## Tape inventory usage")
    lines.append("")
    lines.append("| Tape | Used | Cap | Status |")
    lines.append("|------|-----:|----:|--------|")
    for name, used, cap_str, status in rows:
        lines.append(f"| {name} | {used} | {cap_str} | {status} |")
    lines.append("")
    return lines


def _format_side_candidate(c: SideCandidate) -> str:
    if c.source == "library" and c.album is not None:
        return f"{c.album.display} \u2014 {format_hms(c.album.duration_sec)} \u2014 {c.genre or c.album.primary_genre or '?'} [library]"
    if c.source in ("musicbrainz", "lastfm"):
        bits = [c.label]
        if c.duration_sec:
            bits.append(format_hms(c.duration_sec))
        if c.genre:
            bits.append(c.genre)
        bits.append(f"[{c.source}]")
        return " \u2014 ".join(bits)
    if c.source == "search-url":
        return f"{c.label}: {c.url}"
    return c.label


def write_plan(
    path: Path,
    assignments: list[Assignment],
    unplaced: list[Album],
    tape_inventory: dict[str, int] | None = None,
    tape_counts: dict[str, int] | None = None,
    unplaced_reasons: dict[str, str] | None = None,
) -> None:
    """Emit plan.md.

    `tape_inventory` + `tape_counts` are optional and, when both given with any
    content, render an Inventory usage table at the top. `unplaced_reasons`
    maps album.path -> reason string; paths not in the dict fall back to the
    heuristic reason logic (same as before).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    lines.append("# Tape plan")
    lines.append("")
    lines.append(f"Assignments: **{len(assignments)}**  \nUnplaced albums: **{len(unplaced)}**")
    lines.append("")

    if tape_counts is not None and (tape_inventory or tape_counts):
        lines.extend(_format_inventory_section(tape_inventory or {}, tape_counts))

    for i, asn in enumerate(assignments, 1):
        split = f" ({asn.tape.split_sec // 60}+{asn.tape.split_sec // 60} split)" if asn.tape.splits else ""
        lines.append(f"## #{i} Tape: {asn.tape.name}{split}")
        side_a = asn.side_a
        lines.append(
            f"- Side A: {side_a.display} \u2014 {_format_side_duration(side_a, asn.side_a_original_sec)} "
            f"\u2014 {side_a.primary_genre or '?'} [library]"
        )
        if asn.side_a_trim_note:
            lines.append(f"  - Trim: {asn.side_a_trim_note}")
        if asn.side_a_trim_skipped:
            lines.append(f"  - Skip these tracks: {_format_skip_labels(asn.side_a_trim_skipped)}")
        if asn.side_b is not None:
            side_b = asn.side_b
            lines.append(
                f"- Side B: {side_b.display} \u2014 {_format_side_duration(side_b, asn.side_b_original_sec)} "
                f"\u2014 {side_b.primary_genre or '?'} [library]"
            )
            if asn.side_b_trim_note:
                lines.append(f"  - Trim: {asn.side_b_trim_note}")
            if asn.side_b_trim_skipped:
                lines.append(f"  - Skip these tracks: {_format_skip_labels(asn.side_b_trim_skipped)}")
        elif asn.match_kind in {"solo", "solo-trimmed"}:
            lines.append("- Side B: \u2014 (album fills tape by itself)")
        else:
            lines.append("- Side B options:")
            for c in asn.b_candidates:
                lines.append(f"  - {_format_side_candidate(c)}")
        lines.append(f"- Match: {asn.match_kind or 'unresolved'}")
        if asn.note:
            lines.append(f"- Note: {asn.note}")
        # Per-side slack is only meaningful for split tapes where Side A actually
        # fits on its own physical side. For a 2-hour album filling a 120-min tape
        # that crosses the midpoint, or for solo-on-a-solo-tape, report total slack.
        show_per_side = (
            asn.tape.splits
            and asn.tape.split_sec is not None
            and asn.side_a.duration_sec <= asn.tape.split_sec
        )
        if show_per_side:
            b_label = (
                format_hms(asn.side_b_slack_sec or 0)
                if asn.side_b is not None
                else f"{format_hms(asn.side_b_slack_sec or 0)} (unassigned)"
            )
            lines.append(
                f"- Slack: A {format_hms(asn.side_a_slack_sec)} | B {b_label}"
            )
        else:
            lines.append(f"- Slack: {format_hms(asn.slack_sec)}")
        lines.append("")

    if unplaced:
        from .trim import is_compilation_title
        lines.append(f"## Unplaced albums ({len(unplaced)})")
        lines.append("")
        lines.append("| Duration | Artist | Album | Reason |")
        lines.append("|---------:|--------|-------|--------|")
        for a in sorted(unplaced, key=lambda x: -x.duration_sec):
            # Prefer an explicit reason from the planner (e.g. "tape inventory
            # exhausted for ..."). Otherwise fall back to the length-based
            # heuristic reasons.
            override = (unplaced_reasons or {}).get(a.path)
            if override:
                reason = override
            else:
                t = smallest_fitting_tape(a.duration_sec)
                if t is None:
                    if is_compilation_title(a.album):
                        reason = "compilation/live, whole-album only; consider manual 2-sided split"
                    else:
                        reason = "exceeds all tape sizes; trim heuristic could not shrink to fit"
                else:
                    reason = "no compatible pairing slot"
            lines.append(f"| {format_hms(a.duration_sec)} | {a.artist} | {a.album} | {reason} |")
        lines.append("")

    path.write_text("\n".join(lines), encoding="utf-8")
