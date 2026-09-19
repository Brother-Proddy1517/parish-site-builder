"""
Tests for site_build.py (v0.2)

Run with:
    pytest tests/
    (or: python3 -m pytest tests/  if the pytest command isn't on your PATH)

These tests never touch real /srv/www or /etc/nginx — directories are
monkeypatched to temp folders, and subprocess calls to `nginx` / `systemctl`
are stubbed out, so they're safe to run on a machine with no nginx at all.
"""

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import site_build  # noqa: E402


@pytest.fixture
def fake_dirs(tmp_path, monkeypatch):
    """Point all the real-filesystem paths at temp dirs."""
    web_root = tmp_path / "srv_www"
    web_root.mkdir()
    sites_available = tmp_path / "sites-available"
    sites_available.mkdir()
    sites_enabled = tmp_path / "sites-enabled"
    sites_enabled.mkdir()

    monkeypatch.setattr(site_build, "WEB_ROOT_BASE", web_root)
    monkeypatch.setattr(site_build, "NGINX_SITES_AVAILABLE", sites_available)
    monkeypatch.setattr(site_build, "NGINX_SITES_ENABLED", sites_enabled)

    return {
        "web_root": web_root,
        "sites_available": sites_available,
        "sites_enabled": sites_enabled,
    }


@pytest.fixture
def fake_nginx_ok(monkeypatch):
    """Pretend nginx is installed and every subprocess call succeeds."""
    monkeypatch.setattr(site_build.shutil, "which", lambda cmd: "/usr/sbin/nginx")

    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(site_build.subprocess, "run", fake_run)


@pytest.fixture
def fake_nginx_test_fails(monkeypatch):
    """Pretend nginx is installed, but `nginx -t` fails."""
    monkeypatch.setattr(site_build.shutil, "which", lambda cmd: "/usr/sbin/nginx")

    def fake_run(cmd, **kwargs):
        if cmd[:2] == ["nginx", "-t"]:
            return subprocess.CompletedProcess(cmd, returncode=1, stdout="", stderr="syntax error")
        return subprocess.CompletedProcess(cmd, returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(site_build.subprocess, "run", fake_run)


# --- create_site_directory (unchanged behavior from v0.1) ---

def test_creates_directory(fake_dirs):
    exit_code = site_build.create_site_directory("messiah")
    assert exit_code == 0
    assert (fake_dirs["web_root"] / "messiah").is_dir()


def test_refuses_if_already_exists(fake_dirs):
    (fake_dirs["web_root"] / "messiah").mkdir()
    exit_code = site_build.create_site_directory("messiah")
    assert exit_code != 0
    assert (fake_dirs["web_root"] / "messiah").is_dir()


# --- create_nginx_site (new in v0.2) ---

def test_nginx_site_created_and_enabled(fake_dirs, fake_nginx_ok):
    site_build.create_site_directory("messiah")

    exit_code = site_build.create_nginx_site("messiah", "messiah.example.org")

    assert exit_code == 0
    config_path = fake_dirs["sites_available"] / "messiah"
    symlink_path = fake_dirs["sites_enabled"] / "messiah"
    assert config_path.is_file()
    assert symlink_path.is_symlink()
    config_text = config_path.read_text()
    assert "server_name messiah.example.org;" in config_text
    assert "listen [::]:80;" in config_text  # IPv6 — catches the default-server fallthrough bug


def test_nginx_not_installed(fake_dirs, monkeypatch):
    monkeypatch.setattr(site_build.shutil, "which", lambda cmd: None)

    exit_code = site_build.create_nginx_site("messiah", "messiah.example.org")

    assert exit_code != 0
    assert not (fake_dirs["sites_available"] / "messiah").exists()


def test_rolls_back_on_failed_nginx_test(fake_dirs, fake_nginx_test_fails):
    exit_code = site_build.create_nginx_site("messiah", "messiah.example.org")

    assert exit_code != 0
    # Both the config and the symlink should be cleaned up — nginx was never touched.
    assert not (fake_dirs["sites_available"] / "messiah").exists()
    assert not (fake_dirs["sites_enabled"] / "messiah").exists()


def test_refuses_if_config_already_exists(fake_dirs, fake_nginx_ok):
    (fake_dirs["sites_available"] / "messiah").write_text("existing config")

    exit_code = site_build.create_nginx_site("messiah", "messiah.example.org")

    assert exit_code != 0


# --- main() end-to-end ---

def test_main_creates_dir_and_nginx_site(fake_dirs, fake_nginx_ok):
    exit_code = site_build.main(["messiah", "--domain", "messiah.example.org"])

    assert exit_code == 0
    assert (fake_dirs["web_root"] / "messiah").is_dir()
    assert (fake_dirs["web_root"] / "messiah" / "index.html").is_file()
    assert (fake_dirs["sites_available"] / "messiah").is_file()


def test_main_domain_defaults_to_name(fake_dirs, fake_nginx_ok):
    exit_code = site_build.main(["messiah"])

    assert exit_code == 0
    config_text = (fake_dirs["sites_available"] / "messiah").read_text()
    assert "server_name messiah;" in config_text


def test_main_stops_before_nginx_if_dir_creation_fails(fake_dirs, fake_nginx_ok):
    (fake_dirs["web_root"] / "messiah").mkdir()  # pre-existing, will cause dir creation to fail

    exit_code = site_build.main(["messiah"])

    assert exit_code != 0
    assert not (fake_dirs["sites_available"] / "messiah").exists()
