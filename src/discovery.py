"""Walk the library and yield leaf album folders.

A leaf album folder is one that either:
  - directly contains audio files, or
  - whose subfolders are all "disc" folders (CD1, Disc 2, ...) that contain audio files.

Directories whose path matches any pattern in SKIP_DIRS are pruned during the
walk. Patterns use pathlib-style globs matched against the folder's path
relative to the library root (case-insensitive). See `_should_skip` for
semantics and `walk_library` for per-call overrides.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path, PurePosixPath

AUDIO_EXTS = {".flac", ".mp3", ".wav", ".wv", ".dsf", ".dff", ".m4a", ".aac", ".ogg", ".opus", ".vob", ".iso", ".ape"}

# Default globs for folders to prune from the walk. Matched against the
# relative path from the library root (POSIX separators, lowercased).
#
# Patterns use shell-style fnmatch semantics: bare patterns (no '/') match
# against the folder's basename at any depth, so "# clips*" prunes any folder
# whose name starts with "# clips". Patterns containing '/' match against
# the full relative path, so "jazz/**/demos" only prunes demos folders under
# a top-level "jazz".
SKIP_DIRS: tuple[str, ...] = (
    "# clips*",
    "# mixes and compilations*",
    "# random*",
    "# recordings & transfers*",
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


def _should_skip(rel_path: PurePosixPath, patterns: tuple[str, ...]) -> bool:
    """Return True when `rel_path` matches any pattern.

    - Patterns without '/' match against the basename only (at any depth).
    - Patterns with '/' match against the full relative path from the library
      root, using fnmatch (case-insensitive; already lowercased by callers).
    """
    if not patterns:
        return False
    name = rel_path.name
    full = str(rel_path)
    for pat in patterns:
        if "/" in pat:
            if fnmatchcase(full, pat):
                return True
        else:
            if fnmatchcase(name, pat):
                return True
    return False


def matches_skip_patterns(
    folder_path: str | Path,
    patterns: tuple[str, ...],
    library_root: str | Path | None = None,
) -> bool:
    """Public companion to `_should_skip` usable with absolute paths.

    Callers that already have an absolute `folder_path` and (optionally) the
    library root can use this to apply the same skip-dir semantics the
    library walker does, without re-traversing the filesystem. `plan` uses
    this to filter albums.json entries by the same globs `scan` would have
    pruned.

    When `library_root` is None or the folder is not under it, we match
    against the folder's basename only (bare patterns still work; path-
    anchored patterns simply won't match -- which is the honest outcome,
    since we can't compute a relative path without a root).
    """
    if not patterns:
        return False
    folder = Path(folder_path)
    rel: PurePosixPath
    if library_root is not None:
        try:
            rel = PurePosixPath(
                folder.relative_to(Path(library_root)).as_posix().lower()
            )
        except ValueError:
            # Path isn't under the declared root. Fall back to basename match.
            rel = PurePosixPath(folder.name.lower())
    else:
        rel = PurePosixPath(folder.name.lower())
    lowered = tuple(p.lower() for p in patterns)
    return _should_skip(rel, lowered)


def infer_library_root(album_paths: list[str]) -> Path | None:
    """Best-effort shared root across absolute album paths.

    `albums.json` doesn't currently store the library root, but it's recoverable
    as the common ancestor of all album folders. If we got 3 album paths
    `H:\\Music\\X`, `H:\\Music\\Y\\Z`, `H:\\Music\\W`, the library root is
    `H:\\Music`. Used by `plan` so that path-anchored skip patterns resolve
    the same relative paths scan would have computed.

    Returns None on mixed-drive paths (Windows) or a single empty list.
    """
    paths = [p for p in album_paths if p]
    if not paths:
        return None
    try:
        import os as _os
        common = _os.path.commonpath([str(Path(p)) for p in paths])
        if not common:
            return None
        # `commonpath` returns the deepest shared component, which for a
        # single album would be the album itself. In that case its parent is
        # a better stand-in for the library root.
        root = Path(common)
        if len(paths) == 1:
            return root.parent
        return root
    except (ValueError, OSError):
        return None


def walk_library(
    root: Path,
    skip_dirs: tuple[str, ...] | None = None,
) -> list[AlbumFolder]:
    """Return all album folders under `root`.

    `skip_dirs` overrides the module-level `SKIP_DIRS` globs when given
    (including `()` to disable skipping entirely). `None` keeps the defaults.

    Implementation note: we walk recursively and treat any folder with direct audio as a leaf
    album (not descending into it further). Multi-disc containers are detected one level up.
    """
    root = root.resolve()
    results: list[AlbumFolder] = []
    effective_patterns = SKIP_DIRS if skip_dirs is None else skip_dirs
    # Normalize to lowercase once so matching is case-insensitive without
    # paying for re-casing per-folder.
    effective_patterns = tuple(p.lower() for p in effective_patterns)

    def visit(folder: Path, depth: int) -> None:
        if depth > 0:
            # Relative path with POSIX separators, lowercased, for stable glob
            # matching on Windows ("jazz/demos" works the same everywhere).
            rel = PurePosixPath(folder.relative_to(root).as_posix().lower())
            if _should_skip(rel, effective_patterns):
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
