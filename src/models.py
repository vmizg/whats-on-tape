"""Core dataclasses shared across the scan and plan subcommands."""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any


@dataclass
class Album:
    path: str
    artist: str
    album: str
    year: str = ""
    source: str = ""
    duration_sec: int = 0
    track_count: int = 0
    genres: list[str] = field(default_factory=list)
    format: str = ""
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Album":
        return cls(
            path=data["path"],
            artist=data.get("artist", ""),
            album=data.get("album", ""),
            year=data.get("year", ""),
            source=data.get("source", ""),
            duration_sec=int(data.get("duration_sec", 0)),
            track_count=int(data.get("track_count", 0)),
            genres=list(data.get("genres", [])),
            format=data.get("format", ""),
            warnings=list(data.get("warnings", [])),
        )

    @property
    def primary_genre(self) -> str:
        return self.genres[0] if self.genres else ""

    @property
    def display(self) -> str:
        year = f" ({self.year})" if self.year else ""
        if self.artist:
            return f"{self.artist} - {self.album}{year}"
        return f"{self.album}{year}"


@dataclass(frozen=True)
class Tape:
    name: str
    total_sec: int
    split_sec: int | None  # None => no mid-tape side split supported

    @property
    def splits(self) -> bool:
        return self.split_sec is not None


@dataclass
class SideCandidate:
    """A proposed B-side. Either a local album or an external suggestion."""
    source: str  # "library" | "musicbrainz" | "search-url"
    label: str
    duration_sec: int = 0
    genre: str = ""
    album: Album | None = None
    url: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class Assignment:
    tape: Tape
    side_a: Album
    side_b: Album | None = None
    b_candidates: list[SideCandidate] = field(default_factory=list)
    match_kind: str = ""  # "solo" | "solo-trimmed" | "tight-local" | "relaxed-local" | "musicbrainz" | "musicbrainz+lastfm" | "lastfm" | "search-url" | "unresolved"
    note: str = ""
    # If side_a and/or side_b were trimmed (e.g. bonus tracks skipped to fit the tape),
    # these carry the human-readable notes. Empty = no trim applied to that side.
    side_a_trim_note: str = ""
    side_b_trim_note: str = ""
    # Skipped-track labels for the trim heuristic. Empty when trim came from MB (which
    # doesn't tell us WHICH tracks to skip) or when no trim was applied.
    side_a_trim_skipped: list[str] = field(default_factory=list)
    side_b_trim_skipped: list[str] = field(default_factory=list)
    # Original (on-disk) duration for each side. 0 means "no trim" (use side_x.duration_sec).
    side_a_original_sec: int = 0
    side_b_original_sec: int = 0

    @property
    def slack_sec(self) -> int:
        used = self.side_a.duration_sec + (self.side_b.duration_sec if self.side_b else 0)
        return max(0, self.tape.total_sec - used)

    @property
    def side_a_slack_sec(self) -> int:
        """Unused time on Side A. For solo tapes this is just the whole-tape slack."""
        side_cap = self.tape.split_sec if self.tape.split_sec is not None else self.tape.total_sec
        return max(0, side_cap - self.side_a.duration_sec)

    @property
    def side_b_slack_sec(self) -> int | None:
        """Unused time on Side B, or None for non-split (solo) tapes.

        If no concrete Side B is assigned yet, the slack is the whole other side.
        """
        if self.tape.split_sec is None:
            return None
        b_used = self.side_b.duration_sec if self.side_b else 0
        return max(0, self.tape.split_sec - b_used)
