"""
Tests for site_build.py (v0.3.1 — explicit CLI output revision)

Run with:
    pytest tests/
    (or: python3 -m pytest tests/  if the pytest command isn't on your PATH)

These tests never touch real /srv/www or /etc/nginx — directories are
monkeypatched to temp folders, and subprocess calls to `nginx` / `systemctl`
are stubbed out, so they're safe to run on a machine with no nginx at all.

Message assertions check for the important information being present
(paths, PASS/FAIL, check labels) rather than exact formatting, so small
wording tweaks won't break the suite.
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
    """Pretend nginx is installed, every subprocess call succeeds, nginx is
    running, and the site verifies as reachable (no default-page fallthrough)."""
    monkeypatch.setattr(site_build.shutil, "which", lambda cmd: "/usr/sbin/nginx")
    monkeypatch.setattr(site_build, "nginx_is_running", lambda: True)

    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(site_build.subprocess, "run", fake_run)
    monkeypatch.setattr(site_build, "verify_site_serving", lambda domain: [])


@pytest.fixture
def fake_nginx_test_fails(monkeypatch):
    """Pretend nginx is installed and running, but `nginx -t` fails."""
    monkeypatch.setattr(site_build.shutil, "which", lambda cmd: "/usr/sbin/nginx")
    monkeypatch.setattr(site_build, "nginx_is_running", lambda: True)

    def fake_run(cmd, **kwargs):
        if cmd[:2] == ["nginx", "-t"]:
            return subprocess.CompletedProcess(cmd, returncode=1, stdout="", stderr="syntax error")
        return subprocess.CompletedProcess(cmd, returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(site_build.subprocess, "run", fake_run)


# --- validation functions ---

@pytest.mark.parametrize("name,expected", [
    ("messiah", True),
    ("st-marks", True),
    ("st_marks2", True),
    ("StMarks", True),
    ("", False),
    ("-messiah", False),          # can't start with a hyphen
    ("messiah/etc", False),       # no slashes
    ("messiah.example", False),   # no dots in a site name
    ("messiah site", False),      # no spaces
    ("a" * 64, False),            # too long
])
def test_is_valid_site_name(name, expected):
    assert site_build.is_valid_site_name(name) == expected


@pytest.mark.parametrize("domain,expected", [
    ("messiah.example.org", True),
    ("messiah", True),                # bare label, allowed for local testing
    ("st-marks.example.org", True),
    ("", False),
    ("-messiah.example.org", False),  # can't start with a hyphen
    ("messiah..org", False),          # empty label
    ("messiah example.org", False),   # no spaces
    ("messiah.example.org/", False),  # no slashes
])
def test_is_valid_domain(domain, expected):
    assert site_build.is_valid_domain(domain) == expected


def test_find_domain_conflict_detects_existing(fake_dirs):
    (fake_dirs["sites_available"] / "stmarks").write_text(
        "server {\n    server_name stmarks.example.org;\n}\n"
    )
    conflict = site_build.find_domain_conflict("stmarks.example.org")
    assert conflict == "stmarks"


def test_find_domain_conflict_none_when_free(fake_dirs):
    conflict = site_build.find_domain_conflict("unused.example.org")
    assert conflict is None


def test_preflight_all_pass_on_clean_setup(fake_dirs, monkeypatch):
    monkeypatch.setattr(site_build.shutil, "which", lambda cmd: "/usr/sbin/nginx")
    monkeypatch.setattr(site_build, "nginx_is_running", lambda: True)

    checks = site_build.run_preflight_checks("messiah", "messiah.example.org")

    assert all(c["passed"] for c in checks)


def test_preflight_catches_invalid_name(fake_dirs, monkeypatch):
    monkeypatch.setattr(site_build.shutil, "which", lambda cmd: "/usr/sbin/nginx")
    monkeypatch.setattr(site_build, "nginx_is_running", lambda: True)

    checks = site_build.run_preflight_checks("-bad-name", "messiah.example.org")

    results = {c["label"]: c["passed"] for c in checks}
    assert results["Valid site name"] is False


def test_preflight_catches_nginx_not_running(fake_dirs, monkeypatch):
    monkeypatch.setattr(site_build.shutil, "which", lambda cmd: "/usr/sbin/nginx")
    monkeypatch.setattr(site_build, "nginx_is_running", lambda: False)

    checks = site_build.run_preflight_checks("messiah", "messiah.example.org")

    results = {c["label"]: c["passed"] for c in checks}
    assert results["nginx running"] is False


def test_preflight_catches_domain_conflict(fake_dirs, monkeypatch):
    monkeypatch.setattr(site_build.shutil, "which", lambda cmd: "/usr/sbin/nginx")
    monkeypatch.setattr(site_build, "nginx_is_running", lambda: True)
    (fake_dirs["sites_available"] / "otherparish").write_text(
        "server {\n    server_name messiah.example.org;\n}\n"
    )

    checks = site_build.run_preflight_checks("messiah", "messiah.example.org")

    results = {c["label"]: c["passed"] for c in checks}
    assert results["Domain not already in use"] is False


def test_preflight_shows_nginx_path_on_pass(fake_dirs, monkeypatch):
    monkeypatch.setattr(site_build.shutil, "which", lambda cmd: "/usr/sbin/nginx")
    monkeypatch.setattr(site_build, "nginx_is_running", lambda: True)

    checks = site_build.run_preflight_checks("messiah", "messiah.example.org")

    nginx_check = next(c for c in checks if c["label"] == "nginx installed")
    assert nginx_check["detail"] == "/usr/sbin/nginx"


# --- CLI output content (capsys) ---

def test_preflight_output_lists_each_check_with_pass_fail(fake_dirs, fake_nginx_ok, capsys):
    site_build.main(["messiah", "--domain", "messiah.example.org"])
    out = capsys.readouterr().out

    assert "Preflight checks:" in out
    for label in [
        "Valid site name",
        "Valid domain",
        "nginx installed",
        "nginx running",
        "Site directory available",
        "Nginx configuration available",
        "Domain not already in use",
    ]:
        assert label in out
    assert "[PASS]" in out


def test_output_reports_created_directory_path(fake_dirs, fake_nginx_ok, capsys):
    site_build.main(["messiah", "--domain", "messiah.example.org"])
    out = capsys.readouterr().out

    expected_path = str(fake_dirs["web_root"] / "messiah")
    assert expected_path in out
    assert "[OK] Created directory" in out


def test_output_reports_index_file_path(fake_dirs, fake_nginx_ok, capsys):
    site_build.main(["messiah", "--domain", "messiah.example.org"])
    out = capsys.readouterr().out

    expected_path = str(fake_dirs["web_root"] / "messiah" / "index.html")
    assert expected_path in out
    assert "[OK] Created index" in out


def test_output_reports_nginx_config_path(fake_dirs, fake_nginx_ok, capsys):
    site_build.main(["messiah", "--domain", "messiah.example.org"])
    out = capsys.readouterr().out

    expected_path = str(fake_dirs["sites_available"] / "messiah")
    assert expected_path in out
    assert "[OK] Created Nginx config" in out


def test_output_reports_enabled_symlink_path_and_target(fake_dirs, fake_nginx_ok, capsys):
    site_build.main(["messiah", "--domain", "messiah.example.org"])
    out = capsys.readouterr().out

    symlink_path = str(fake_dirs["sites_enabled"] / "messiah")
    target_path = str(fake_dirs["sites_available"] / "messiah")
    assert "[OK] Enabled site" in out
    assert symlink_path in out
    assert target_path in out
    assert "->" in out


def test_output_reports_nginx_test_validation(fake_dirs, fake_nginx_ok, capsys):
    site_build.main(["messiah", "--domain", "messiah.example.org"])
    out = capsys.readouterr().out

    assert "Validating Nginx configuration:" in out
    assert "nginx -t" in out
    assert "[PASS] nginx -t" in out


def test_output_reports_successful_verification(fake_dirs, fake_nginx_ok, capsys):
    site_build.main(["messiah", "--domain", "messiah.example.org"])
    out = capsys.readouterr().out

    assert "Verifying site:" in out
    assert "messiah.example.org" in out
    assert "[PASS]" in out
    assert "is being served correctly" in out


def test_output_summary_lists_all_created_paths(fake_dirs, fake_nginx_ok, capsys):
    site_build.main(["messiah", "--domain", "messiah.example.org"])
    out = capsys.readouterr().out

    assert "Site created successfully." in out
    assert "Created:" in out
    assert str(fake_dirs["web_root"] / "messiah") in out
    assert str(fake_dirs["web_root"] / "messiah" / "index.html") in out
    assert str(fake_dirs["sites_available"] / "messiah") in out
    assert str(fake_dirs["sites_enabled"] / "messiah") in out


def test_failure_output_identifies_failed_preflight_check(fake_dirs, monkeypatch, capsys):
    monkeypatch.setattr(site_build.shutil, "which", lambda cmd: None)  # nginx "not installed"

    site_build.main(["messiah", "--domain", "messiah.example.org"])
    out = capsys.readouterr().out

    assert "[FAIL] nginx installed" in out
    assert "No changes were made." in out


def test_failure_output_identifies_failed_nginx_test(fake_dirs, fake_nginx_test_fails, capsys):
    site_build.main(["messiah", "--domain", "messiah.example.org"])
    out = capsys.readouterr().out

    assert "[FAIL] nginx -t" in out
    assert "Rolled back" in out
    assert str(fake_dirs["sites_available"] / "messiah") in out
    assert str(fake_dirs["sites_enabled"] / "messiah") in out


# --- behavioral correctness (unchanged safety behavior) ---

def test_preflight_failure_makes_no_filesystem_changes(fake_dirs, monkeypatch):
    monkeypatch.setattr(site_build.shutil, "which", lambda cmd: None)

    exit_code = site_build.main(["messiah", "--domain", "messiah.example.org"])

    assert exit_code != 0
    assert not (fake_dirs["web_root"] / "messiah").exists()
    assert not (fake_dirs["sites_available"] / "messiah").exists()


def test_rolls_back_on_failed_nginx_test(fake_dirs, fake_nginx_test_fails):
    exit_code = site_build.main(["messiah", "--domain", "messiah.example.org"])

    assert exit_code != 0
    assert not (fake_dirs["sites_available"] / "messiah").exists()
    assert not (fake_dirs["sites_enabled"] / "messiah").exists()
    # Web root and index are allowed to exist — only the nginx pieces roll back.
    assert (fake_dirs["web_root"] / "messiah").is_dir()


def test_refuses_if_config_already_exists(fake_dirs, fake_nginx_ok):
    (fake_dirs["sites_available"] / "messiah").write_text("existing config")

    exit_code = site_build.main(["messiah", "--domain", "messiah.example.org"])

    assert exit_code != 0


def test_main_creates_dir_and_nginx_site(fake_dirs, fake_nginx_ok):
    exit_code = site_build.main(["messiah", "--domain", "messiah.example.org"])

    assert exit_code == 0
    assert (fake_dirs["web_root"] / "messiah").is_dir()
    assert (fake_dirs["web_root"] / "messiah" / "index.html").is_file()
    assert (fake_dirs["sites_available"] / "messiah").is_file()
    assert (fake_dirs["sites_enabled"] / "messiah").is_symlink()


def test_main_domain_defaults_to_name(fake_dirs, fake_nginx_ok):
    exit_code = site_build.main(["messiah"])

    assert exit_code == 0
    config_text = (fake_dirs["sites_available"] / "messiah").read_text()
    assert "server_name messiah;" in config_text


def test_config_includes_ipv6_listener(fake_dirs, fake_nginx_ok):
    site_build.main(["messiah", "--domain", "messiah.example.org"])

    config_text = (fake_dirs["sites_available"] / "messiah").read_text()
    assert "listen 80;" in config_text
    assert "listen [::]:80;" in config_text


# --- restart prompt behavior (unchanged) ---

def test_prompts_and_restarts_when_default_page_detected(fake_dirs, fake_nginx_ok, monkeypatch):
    calls = {"count": 0}

    def fake_verify(domain):
        calls["count"] += 1
        if calls["count"] == 1:
            return ["IPv6: still serving the default nginx page, not stmarks.example.org"]
        return []

    monkeypatch.setattr(site_build, "verify_site_serving", fake_verify)
    monkeypatch.setattr("builtins.input", lambda prompt: "y")

    restart_calls = []

    def fake_run(cmd, **kwargs):
        if cmd[:2] == ["systemctl", "restart"]:
            restart_calls.append(cmd)
        return subprocess.CompletedProcess(cmd, returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(site_build.subprocess, "run", fake_run)

    exit_code = site_build.main(["stmarks", "--domain", "stmarks.example.org"])

    assert exit_code == 0
    assert len(restart_calls) == 1
    assert calls["count"] == 2  # verified before AND after restart


def test_declines_restart_leaves_site_configured(fake_dirs, fake_nginx_ok, monkeypatch):
    monkeypatch.setattr(
        site_build,
        "verify_site_serving",
        lambda domain: ["IPv6: still serving the default nginx page, not stmarks.example.org"],
    )
    monkeypatch.setattr("builtins.input", lambda prompt: "n")

    restart_calls = []

    def fake_run(cmd, **kwargs):
        if cmd[:2] == ["systemctl", "restart"]:
            restart_calls.append(cmd)
        return subprocess.CompletedProcess(cmd, returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(site_build.subprocess, "run", fake_run)

    exit_code = site_build.main(["stmarks", "--domain", "stmarks.example.org"])

    # Config is valid and in place — this isn't a failure, just an unresolved warning.
    assert exit_code == 0
    assert len(restart_calls) == 0
    config_path = fake_dirs["sites_available"] / "stmarks"
    assert config_path.is_file()


def test_no_prompt_when_site_verifies_clean(fake_dirs, fake_nginx_ok, monkeypatch):
    prompts = []
    monkeypatch.setattr("builtins.input", lambda prompt: prompts.append(prompt) or "n")

    exit_code = site_build.main(["stmarks", "--domain", "stmarks.example.org"])

    assert exit_code == 0
    assert prompts == []  # input() was never called
