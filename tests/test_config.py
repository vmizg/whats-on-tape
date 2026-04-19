"""Tests for src/config.py (JSON-backed plan_config.json)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.config import ConfigError, PlanConfig, load_config, tape_inventory_usage_summary


def _write(tmp_path: Path, payload: dict) -> Path:
    p = tmp_path / "plan_config.json"
    p.write_text(json.dumps(payload), encoding="utf-8")
    return p


def test_load_none_returns_empty_config():
    cfg = load_config(None)
    assert isinstance(cfg, PlanConfig)
    assert cfg.tape_inventory == {}
    assert cfg.skip_dirs is None
    assert cfg.source_path is None
    assert cfg.has_inventory is False


def test_load_missing_file_raises(tmp_path: Path):
    missing = tmp_path / "does_not_exist.json"
    with pytest.raises(ConfigError, match="not found"):
        load_config(missing)


def test_load_inventory_with_exact_names(tmp_path: Path):
    p = _write(tmp_path, {
        "tape_inventory": {
            "46min": 3,
            "70min": 2,
        }
    })
    cfg = load_config(p)
    assert cfg.tape_inventory == {"46min": 3, "70min": 2}
    assert cfg.has_inventory is True
    assert cfg.source_path == p


def test_load_inventory_accepts_minute_shortcut(tmp_path: Path):
    """Bare numbers should resolve too: '70' -> the 70-min tape."""
    p = _write(tmp_path, {"tape_inventory": {"70": 2, "120": 1}})
    cfg = load_config(p)
    assert cfg.tape_inventory == {"70min": 2, "120min": 1}


def test_load_rejects_unknown_tape_length(tmp_path: Path):
    p = _write(tmp_path, {"tape_inventory": {"99min": 1}})
    with pytest.raises(ConfigError, match="unknown tape length"):
        load_config(p)


def test_load_rejects_invalid_tape_length(tmp_path: Path):
    p = _write(tmp_path, {"tape_inventory": {"reel of 70min": 1}})
    with pytest.raises(ConfigError, match="unknown tape length"):
        load_config(p)


def test_load_rejects_negative_count(tmp_path: Path):
    p = _write(tmp_path, {"tape_inventory": {"70min": -1}})
    with pytest.raises(ConfigError, match=">= 0"):
        load_config(p)


def test_load_rejects_non_integer_count(tmp_path: Path):
    p = _write(tmp_path, {"tape_inventory": {"70min": "two"}})
    with pytest.raises(ConfigError, match="must be an integer"):
        load_config(p)


def test_load_rejects_boolean_count(tmp_path: Path):
    # bool is a subclass of int in Python; we shouldn't accept True/False as 1/0.
    p = _write(tmp_path, {"tape_inventory": {"70min": True}})
    with pytest.raises(ConfigError, match="must be an integer"):
        load_config(p)


def test_load_zero_count_is_allowed(tmp_path: Path):
    # Zero = "disable this size entirely", which is a legitimate and useful setting.
    p = _write(tmp_path, {"tape_inventory": {"70min": 0}})
    cfg = load_config(p)
    assert cfg.tape_inventory == {"70min": 0}


def test_load_skip_dirs_lowercased(tmp_path: Path):
    p = _write(tmp_path, {"skip_dirs": ["# Clips*", "**/Demos"]})
    cfg = load_config(p)
    assert cfg.skip_dirs == ("# clips*", "**/demos")


def test_load_skip_dirs_normalizes_backslashes(tmp_path: Path):
    """Users on Windows sometimes write paths with backslashes; we store
    patterns in POSIX form to match how the walker compares them."""
    p = _write(tmp_path, {"skip_dirs": ["Jazz\\Demos"]})
    cfg = load_config(p)
    assert cfg.skip_dirs == ("jazz/demos",)


def test_load_empty_skip_dirs_keeps_empty_override(tmp_path: Path):
    """Empty list should mean "scan EVERYTHING" (override with nothing), not
    "fall back to defaults"."""
    p = _write(tmp_path, {"skip_dirs": []})
    cfg = load_config(p)
    assert cfg.skip_dirs == ()


def test_load_rejects_non_string_pattern(tmp_path: Path):
    p = _write(tmp_path, {"skip_dirs": ["# clips*", 42]})
    with pytest.raises(ConfigError, match="list of strings"):
        load_config(p)


def test_load_rejects_unknown_top_level_key(tmp_path: Path):
    p = _write(tmp_path, {"tape_inventory": {}, "typo_key": 1})
    with pytest.raises(ConfigError, match="unknown top-level key"):
        load_config(p)


def test_load_rejects_malformed_json(tmp_path: Path):
    p = tmp_path / "bad.json"
    p.write_text("{ not valid json", encoding="utf-8")
    with pytest.raises(ConfigError, match="invalid JSON"):
        load_config(p)


def test_load_rejects_non_object_root(tmp_path: Path):
    p = tmp_path / "array.json"
    p.write_text("[1,2,3]", encoding="utf-8")
    with pytest.raises(ConfigError, match="must be a JSON object"):
        load_config(p)


def test_usage_summary_skips_unused_uncapped_tapes():
    summary = tape_inventory_usage_summary(
        inventory={"70min": 2},
        counts={"90min": 3},
    )
    names = [row[0] for row in summary]
    # 70min appears even with 0 used because it has a cap.
    assert "70min" in names
    # 90min appears because it's been used, with cap -1 (unlimited).
    assert ("90min", 3, -1) in summary
    # A tape with neither cap nor usage stays out.
    assert "54min" not in names
