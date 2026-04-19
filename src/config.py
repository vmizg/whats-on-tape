"""Optional JSON config file for `scan` and `plan`.

The config file is a single JSON object. All keys are optional; anything you
don't set keeps its built-in default.

Schema (current version)
------------------------
{
  "tape_inventory": {
    "46min":  5,
    "54min":  3,
    "60min":  10,
    "70min":  2,
    "90min":  4,
    "120min": 1
  },
  "skip_dirs": ["# clips*", "# mixes and compilations*", "**/Demos"]
}

- `tape_inventory` caps how many tapes of each size the planner may use.
  Keys match `Tape.name` exactly ("46min", "54min", ..., "120min"). As a
  convenience a bare-number key like "70" is also accepted. Missing size =
  unlimited. `0` = don't use this size at all.
- `skip_dirs`, when present, FULLY REPLACES the built-in `SKIP_DIRS` globs
  used by the library walker (it does NOT merge). Patterns use shell-style
  fnmatch semantics: bare patterns without '/' match basenames at any depth
  ("# clips*"), patterns with '/' match the full relative path from the
  library root ("jazz/**/demos"). Case-insensitive. Leave it out to keep
  the defaults. Use `[]` to scan everything.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .tapes import TAPES


@dataclass
class PlanConfig:
    """Parsed contents of a plan_config.json. All fields are optional.

    `tape_inventory` is keyed by canonical `Tape.name`; the loader normalizes
    shortcut keys (e.g. "70" -> "70min") so downstream code never has to
    worry about alternate spellings.
    """
    tape_inventory: dict[str, int] = field(default_factory=dict)
    skip_dirs: tuple[str, ...] | None = None  # None => keep defaults
    source_path: Path | None = None

    @property
    def has_inventory(self) -> bool:
        return bool(self.tape_inventory)


class ConfigError(ValueError):
    pass


def _resolve_tape_key(key: str) -> str | None:
    """Map a user-supplied tape key to the canonical `Tape.name`, or None.

    Accepts:
      - canonical tape name ("70min")       -> itself (matches TAPES)
      - bare minutes ("70")                 -> "70min" (looked up in TAPES)
    Returns None when no tape matches; callers surface that as a ConfigError.
    """
    key = key.strip()
    for t in TAPES:
        if t.name == key:
            return t.name
    stripped = key.lower().removesuffix("min").strip()
    if stripped.isdigit():
        minutes = int(stripped)
        matches = [t for t in TAPES if t.total_sec == minutes * 60]
        if len(matches) == 1:
            return matches[0].name
    return None


def load_config(path: Path | None) -> PlanConfig:
    """Load `path` into a PlanConfig. `None` or missing file -> empty defaults.

    Raises ConfigError on JSON syntax errors, unknown tape keys, or obviously
    wrong value shapes (negative counts, non-string prefixes, etc.). Prefer
    failing loudly over silently dropping the user's intended caps.
    """
    if path is None:
        return PlanConfig()
    if not path.exists():
        # A missing file is fine when the user didn't ask for one; they did,
        # so complain. Default path (see cli.py) handles the "no file at all"
        # case by passing None instead.
        raise ConfigError(f"config file not found: {path}")

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ConfigError(f"{path}: invalid JSON ({e})") from e
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: top-level must be a JSON object")

    cfg = PlanConfig(source_path=path)

    inv_raw = raw.get("tape_inventory", {})
    if not isinstance(inv_raw, dict):
        raise ConfigError(f"{path}: 'tape_inventory' must be an object")
    normalized: dict[str, int] = {}
    for key, val in inv_raw.items():
        canonical = _resolve_tape_key(str(key))
        if canonical is None:
            known = ", ".join(t.name for t in TAPES)
            raise ConfigError(
                f"{path}: unknown tape length '{key}' in tape_inventory. Known tape lengths: {known}"
            )
        if not isinstance(val, int) or isinstance(val, bool):
            raise ConfigError(
                f"{path}: tape_inventory['{key}'] must be an integer, got {type(val).__name__}"
            )
        if val < 0:
            raise ConfigError(f"{path}: tape_inventory['{key}'] must be >= 0 (got {val})")
        normalized[canonical] = val
    cfg.tape_inventory = normalized

    patterns = raw.get("skip_dirs")
    if patterns is not None:
        if not isinstance(patterns, list) or not all(isinstance(p, str) for p in patterns):
            raise ConfigError(f"{path}: 'skip_dirs' must be a list of strings")
        # Normalize to lowercase so matching is case-insensitive in a predictable way.
        # Also swap backslashes to forward slashes so Windows-style paths work.
        cfg.skip_dirs = tuple(
            p.strip().lower().replace("\\", "/") for p in patterns
        )

    # Reject unknown keys so typos surface instead of being silently ignored.
    known_keys = {"tape_inventory", "skip_dirs"}
    extra = set(raw.keys()) - known_keys
    if extra:
        raise ConfigError(
            f"{path}: unknown top-level key(s): {sorted(extra)}. Known: {sorted(known_keys)}"
        )

    return cfg


def tape_inventory_usage_summary(
    inventory: dict[str, int],
    counts: dict[str, int],
) -> list[tuple[str, int, int]]:
    """Build an ordered (tape_name, used, cap) list for reporting.

    Only tapes that have a configured cap OR have been used are included.
    Cap of -1 in the output means "unlimited" (easier for formatting than None).
    """
    out: list[tuple[str, int, int]] = []
    for t in TAPES:
        cap = inventory.get(t.name)
        used = counts.get(t.name, 0)
        if cap is None and used == 0:
            continue
        out.append((t.name, used, cap if cap is not None else -1))
    return out
