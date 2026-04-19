"""Walk the library and yield leaf album folders.

A leaf album folder is one that either:
  - directly contains audio files, or
  - whose subfolders are all "disc" folders (CD1, Disc 2, ...) that contain audio files.

Some top-level folders are skipped, check and adjust SKIP_TOP_PREFIXES.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

AUDIO_EXTS = {".flac", ".mp3", ".wav", ".wv", ".dsf", ".dff", ".m4a", ".aac", ".ogg", ".opus", ".vob", ".iso", ".ape"}

SKIP_TOP_PREFIXES = (
    "# clips",
    "# mixes and compilations",
    "# random",
    "# recordings & transfers",
)

_DISC_RE = re.compile(r"^(cd|disc|disk|lp|vinyl|side)[\s_-]*\d+", re.IGNORECASE)


@dataclass
class AlbumFolder:
    """A folder identified as an album, plus all of its audio files (flattened across discs)."""
    root: Path
    audio_files: list[Path]
    is_multi_disc: bool
    disc_folders: list[Path]


def is_disc_folder_name(name: str) -> bool:
    return bool(_DISC_RE.match(name.strip()))


def _audio_children(folder: Path) -> list[Path]:
    try:
        return [p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in AUDIO_EXTS]
    except (PermissionError, OSError):
        return []


def _subfolders(folder: Path) -> list[Path]:
    try:
        return [p for p in folder.iterdir() if p.is_dir()]
    except (PermissionError, OSError):
        return []


def _disc_audio_files(disc: Path) -> list[Path]:
    """Audio files belonging to a disc folder.

    Normally these are direct children. Some releases store the disc payload one level
    deeper (e.g. a DVD disc whose audio sits inside `VIDEO_TS/`); when the disc folder
    has no direct audio but exactly one audio-bearing subfolder, we use that instead.
    """
    direct = _audio_children(disc)
    if direct:
        return direct
    subs = _subfolders(disc)
    audio_subs = [s for s in subs if _audio_children(s)]
    if len(audio_subs) == 1:
        return _audio_children(audio_subs[0])
    return []


def _has_any_audio(folder: Path) -> bool:
    """Does `folder` contain any audio file, recursively? Cheap early-exit walk."""
    try:
        stack = [folder]
        while stack:
            current = stack.pop()
            for p in current.iterdir():
                if p.is_file() and p.suffix.lower() in AUDIO_EXTS:
                    return True
                if p.is_dir():
                    stack.append(p)
    except (PermissionError, OSError):
        return False
    return False


def _is_multi_disc_container(folder: Path) -> tuple[bool, list[Path]]:
    """Detect a multi-disc album root.

    A container qualifies when:
      - it has no direct audio children itself, and
      - it has at least 2 disc-named subfolders that carry audio, and
      - every other subfolder is either an empty/missing disc folder (e.g. a
        `CD5 ... (missing)` placeholder) or a non-disc helper folder that
        contains no audio anywhere (e.g. `Artwork/`, `Scans/`, `Booklet/`).

    The last point is what lets real-world box sets like Pink Floyd's Immersion
    series be recognized even though they ship with Artwork/booklet subfolders
    alongside the CDn discs.
    """
    if _audio_children(folder):
        return False, []
    subs = _subfolders(folder)
    if not subs:
        return False, []

    disc_with_audio: list[Path] = []
    for sub in subs:
        is_disc = is_disc_folder_name(sub.name)
        has_audio_here = bool(_disc_audio_files(sub))
        if is_disc and has_audio_here:
            disc_with_audio.append(sub)
            continue
        if is_disc and not has_audio_here:
            # Distinguish an empty/missing disc (allowed) from one whose audio
            # we couldn't safely resolve (ambiguous - refuse to guess).
            if _has_any_audio(sub):
                return False, []
            continue
        if _has_any_audio(sub):
            return False, []

    if disc_with_audio:
        return True, disc_with_audio
    return False, []


def _should_skip_top_level(name: str) -> bool:
    lower = name.strip().lower()
    return any(lower.startswith(prefix) for prefix in SKIP_TOP_PREFIXES)


def walk_library(root: Path) -> list[AlbumFolder]:
    """Return all album folders under `root`.

    Implementation note: we walk recursively and treat any folder with direct audio as a leaf
    album (not descending into it further). Multi-disc containers are detected one level up.
    """
    root = root.resolve()
    results: list[AlbumFolder] = []

    def visit(folder: Path, depth: int) -> None:
        if depth == 1 and _should_skip_top_level(folder.name):
            return

        multi, disc_folders = _is_multi_disc_container(folder)
        if multi:
            files: list[Path] = []
            for disc in sorted(disc_folders):
                files.extend(sorted(_disc_audio_files(disc)))
            if files:
                results.append(AlbumFolder(root=folder, audio_files=files, is_multi_disc=True, disc_folders=sorted(disc_folders)))
            return

        direct = _audio_children(folder)
        if direct and depth > 0:
            results.append(AlbumFolder(root=folder, audio_files=sorted(direct), is_multi_disc=False, disc_folders=[]))
            return

        for sub in sorted(_subfolders(folder)):
            visit(sub, depth + 1)

    visit(root, depth=0)
    return results
