import os
from pathlib import Path

from src.dotenv import load_dotenv


def test_loads_basic(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("MTP_FOO", raising=False)
    p = tmp_path / ".env"
    p.write_text("MTP_FOO=bar\n")
    loaded = load_dotenv(p)
    assert loaded == {"MTP_FOO": "bar"}
    assert os.environ.get("MTP_FOO") == "bar"


def test_respects_existing_env_without_override(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("MTP_FOO", "real")
    p = tmp_path / ".env"
    p.write_text("MTP_FOO=fromfile\n")
    load_dotenv(p)
    assert os.environ["MTP_FOO"] == "real"


def test_strips_quotes_and_comments(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("MTP_A", raising=False)
    monkeypatch.delenv("MTP_B", raising=False)
    p = tmp_path / ".env"
    p.write_text("# comment\nMTP_A=\"quoted\"\nexport MTP_B='other'\n\n")
    load_dotenv(p)
    assert os.environ["MTP_A"] == "quoted"
    assert os.environ["MTP_B"] == "other"


def test_missing_file_is_noop(tmp_path: Path):
    assert load_dotenv(tmp_path / "nope") == {}
