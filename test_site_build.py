"""
Tests for v0.1 of site_build.py

Run with:
    pytest tests/

These tests never touch the real /srv/www — they monkeypatch
WEB_ROOT_BASE to a temporary directory so they're safe to run
on your dev machine, not just the VM.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import site_build  # noqa: E402


@pytest.fixture
def fake_web_root(tmp_path, monkeypatch):
    """Point WEB_ROOT_BASE at a temp dir that already exists, like /srv/www would."""
    fake_root = tmp_path / "srv_www"
    fake_root.mkdir()
    monkeypatch.setattr(site_build, "WEB_ROOT_BASE", fake_root)
    return fake_root


def test_creates_directory(fake_web_root):
    exit_code = site_build.create_site_directory("messiah")
    assert exit_code == 0
    assert (fake_web_root / "messiah").is_dir()


def test_refuses_if_already_exists(fake_web_root):
    (fake_web_root / "messiah").mkdir()

    exit_code = site_build.create_site_directory("messiah")

    assert exit_code != 0
    # Directory should still be there, untouched — not deleted or recreated.
    assert (fake_web_root / "messiah").is_dir()


def test_errors_if_web_root_base_missing(tmp_path, monkeypatch):
    missing_root = tmp_path / "does_not_exist"
    monkeypatch.setattr(site_build, "WEB_ROOT_BASE", missing_root)

    exit_code = site_build.create_site_directory("messiah")

    assert exit_code != 0
    assert not missing_root.exists()


def test_main_parses_positional_name(fake_web_root):
    exit_code = site_build.main(["stmarks"])
    assert exit_code == 0
    assert (fake_web_root / "stmarks").is_dir()


def test_main_requires_name_argument(fake_web_root):
    with pytest.raises(SystemExit):
        site_build.main([])
