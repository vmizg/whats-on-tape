"""Tiny zero-dependency .env loader.

Reads KEY=VALUE lines from a .env file and sets them in os.environ (without overwriting
values that are already set in the real environment). Supports:
- `#` comments and blank lines
- Optional `export KEY=VALUE` prefix
- Single- or double-quoted values (quotes are stripped)

Not a full-featured parser \u2014 we only need the basics here.
"""
from __future__ import annotations

import os
from pathlib import Path


def load_dotenv(path: Path, override: bool = False) -> dict[str, str]:
    """Load KEY=VALUE pairs from `path` into os.environ. Returns what was loaded.

    Missing file = no-op (returns {}). If `override` is False (default), pre-existing env
    values win \u2014 useful so CLI args / real env still take precedence.
    """
    loaded: dict[str, str] = {}
    if not path.exists():
        return loaded
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return loaded

    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        loaded[key] = value
        if override or not os.environ.get(key):
            os.environ[key] = value
    return loaded
