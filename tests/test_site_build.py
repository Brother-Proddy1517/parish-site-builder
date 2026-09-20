"""
Tests for site_build.py (v0.4 — subcommand CLI + SSH-based git deployment)

Run with:
    pytest tests/
    (or: python3 -m pytest tests/  if the pytest command isn't on your PATH)

These tests never touch real /srv/www, /etc/nginx, or an actual git remote.
Directories are monkeypatched to temp folders, and subprocess calls to
`nginx`, `systemctl`, and `git` are stubbed out, so this suite is safe to
run on a machine with none of those installed.
"""

import json
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
    """Pretend nginx and git are installed, every subprocess call succeeds,
    nginx is running, and the site verifies as reachable."""
    monkeypatch.setattr(site_build.shutil, "which", lambda cmd: f"/usr/bin/{cmd}")
    monkeypatch.setattr(site_build, "nginx_is_running", lambda: True)

    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(site_build.subprocess, "run", fake_run)
    monkeypatch.setattr(site_build, "verify_site_serving", lambda domain: [])


@pytest.fixture
def fake_nginx_test_fails(monkeypatch):
    """Pretend nginx/git are installed and running, but `nginx -t` fails."""
    monkeypatch.setattr(site_build.shutil, "which", lambda cmd: f"/usr/bin/{cmd}")
    monkeypatch.setattr(site_build, "nginx_is_running", lambda: True)

    def fake_run(cmd, **kwargs):
        if cmd[:2] == ["nginx", "-t"]:
            return subprocess.CompletedProcess(cmd, returncode=1, stdout="", stderr="syntax error")
        return subprocess.CompletedProcess(cmd, returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(site_build.subprocess, "run", fake_run)


def make_site(dirs, name, domain=None, enabled=True, git_repo=None, git_branch="main"):
    """Helper: hand-craft a site's files + metadata directly, skipping the CLI."""
    domain = domain or name
    config_path = dirs["sites_available"] / name
    config_path.write_text(f"server {{\n    server_name {domain};\n}}\n")
    if enabled:
        (dirs["sites_enabled"] / name).symlink_to(config_path)

    site_dir = dirs["web_root"] / name
    site_dir.mkdir()
    meta = {"name": name, "domain": domain}
    if git_repo:
        meta["git"] = {"repo": git_repo, "branch": git_branch}
        repo_dir = site_dir / "repo"
        (repo_dir / ".git").mkdir(parents=True)
        (repo_dir / "public").mkdir()
    (site_dir / site_build.METADATA_FILENAME).write_text(json.dumps(meta))

    return site_dir, config_path


# --- validation functions ---

@pytest.mark.parametrize("name,expected", [
    ("messiah", True),
    ("st-marks", True),
    ("st_marks2", True),
    ("", False),
    ("-messiah", False),
    ("messiah/etc", False),
    ("messiah site", False),
    ("a" * 64, False),
])
def test_is_valid_site_name(name, expected):
    assert site_build.is_valid_site_name(name) == expected


@pytest.mark.parametrize("domain,expected", [
    ("messiah.example.org", True),
    ("messiah", True),
    ("", False),
    ("-messiah.example.org", False),
    ("messiah example.org", False),
])
def test_is_valid_domain(domain, expected):
    assert site_build.is_valid_domain(domain) == expected


@pytest.mark.parametrize("url,expected", [
    ("https://github.com/parish/site.git", True),
    ("git@github.com:parish/site.git", True),
    ("ssh://git@github.com/parish/site.git", True),
    ("not-a-url", False),
    ("", False),
])
def test_is_valid_git_url(url, expected):
    assert site_build.is_valid_git_url(url) == expected


def test_redact_url_masks_credentials_if_present():
    # SSH URLs (git@host:...) don't have an embedded credential to redact —
    # this only matters as a defense-in-depth fallback for https://user@ URLs.
    assert site_build.redact_url("https://token@github.com/x/y.git") == "https://***@github.com/x/y.git"
    assert site_build.redact_url("git@github.com:parish/site.git") == "git@github.com:parish/site.git"


def test_find_domain_conflict_detects_existing(fake_dirs):
    (fake_dirs["sites_available"] / "stmarks").write_text(
        "server {\n    server_name stmarks.example.org;\n}\n"
    )
    assert site_build.find_domain_conflict("stmarks.example.org") == "stmarks"


def test_find_domain_conflict_none_when_free(fake_dirs):
    assert site_build.find_domain_conflict("unused.example.org") is None


# --- SiteMetadata paths ---

def test_metadata_paths_non_git():
    site_build.WEB_ROOT_BASE = Path("/srv/www")
    meta = site_build.SiteMetadata(name="messiah", domain="messiah.example.org")
    assert meta.site_dir == Path("/srv/www/messiah")
    assert meta.nginx_root == meta.site_dir  # non-git serves directly from site_dir


def test_metadata_paths_git_linked():
    site_build.WEB_ROOT_BASE = Path("/srv/www")
    meta = site_build.SiteMetadata(
        name="messiah", domain="messiah.example.org",
        git_repo="git@github.com:parish/x.git", git_branch="main",
    )
    assert meta.repo_dir == Path("/srv/www/messiah/repo")
    assert meta.public_dir == Path("/srv/www/messiah/repo/public")
    assert meta.nginx_root == meta.public_dir  # git-linked serves from repo/public, NOT repo/


def test_metadata_to_dict_and_from_dict_roundtrip():
    meta = site_build.SiteMetadata(
        name="messiah", domain="messiah.example.org",
        git_repo="git@github.com:parish/x.git", git_branch="main",
    )
    restored = site_build.SiteMetadata.from_dict(meta.to_dict())
    assert restored == meta


# --- create: preflight ---

def test_create_all_preflight_checks_pass_on_clean_setup(fake_dirs, fake_nginx_ok):
    exit_code = site_build.main(["create", "messiah", "--domain", "messiah.example.org"])
    assert exit_code == 0


def test_create_preflight_failure_makes_no_changes(fake_dirs, monkeypatch):
    monkeypatch.setattr(site_build.shutil, "which", lambda cmd: None)

    exit_code = site_build.main(["create", "messiah", "--domain", "messiah.example.org"])

    assert exit_code != 0
    assert not (fake_dirs["web_root"] / "messiah").exists()
    assert not (fake_dirs["sites_available"] / "messiah").exists()


def test_create_preflight_includes_git_checks_only_when_git_given(fake_dirs, fake_nginx_ok):
    meta_no_git = site_build.SiteMetadata(name="messiah", domain="messiah.example.org")
    checks_no_git = site_build.run_preflight_checks(meta_no_git)
    labels_no_git = [c["label"] for c in checks_no_git]
    assert "git installed" not in labels_no_git
    assert "Valid git URL" not in labels_no_git

    meta_git = site_build.SiteMetadata(
        name="messiah", domain="messiah.example.org",
        git_repo="git@github.com:parish/x.git", git_branch="main",
    )
    checks_git = site_build.run_preflight_checks(meta_git)
    labels_git = [c["label"] for c in checks_git]
    assert "git installed" in labels_git
    assert "Valid git URL" in labels_git


def test_create_rejects_invalid_git_url(fake_dirs, fake_nginx_ok):
    exit_code = site_build.main([
        "create", "messiah", "--domain", "messiah.example.org", "--git", "not-a-url",
    ])
    assert exit_code != 0
    assert not (fake_dirs["web_root"] / "messiah").exists()


def test_create_rolls_back_on_failed_nginx_test(fake_dirs, fake_nginx_test_fails):
    exit_code = site_build.main(["create", "messiah", "--domain", "messiah.example.org"])

    assert exit_code != 0
    assert not (fake_dirs["sites_available"] / "messiah").exists()
    assert not (fake_dirs["sites_enabled"] / "messiah").exists()
    assert (fake_dirs["web_root"] / "messiah").is_dir()  # site dir allowed to remain


# --- create: non-git path ---

def test_create_writes_placeholder_index_without_git(fake_dirs, fake_nginx_ok):
    site_build.main(["create", "messiah", "--domain", "messiah.example.org"])

    index_path = fake_dirs["web_root"] / "messiah" / "index.html"
    assert index_path.is_file()


def test_create_non_git_nginx_root_is_site_dir_directly(fake_dirs, fake_nginx_ok):
    site_build.main(["create", "messiah", "--domain", "messiah.example.org"])

    config_text = (fake_dirs["sites_available"] / "messiah").read_text()
    expected_root = str(fake_dirs["web_root"] / "messiah")
    assert f"root {expected_root};" in config_text


def test_create_config_includes_ipv6_listener(fake_dirs, fake_nginx_ok):
    site_build.main(["create", "messiah", "--domain", "messiah.example.org"])

    config_text = (fake_dirs["sites_available"] / "messiah").read_text()
    assert "listen 80;" in config_text
    assert "listen [::]:80;" in config_text


def test_create_domain_defaults_to_name(fake_dirs, fake_nginx_ok):
    exit_code = site_build.main(["create", "messiah"])

    assert exit_code == 0
    config_text = (fake_dirs["sites_available"] / "messiah").read_text()
    assert "server_name messiah;" in config_text


def test_create_writes_metadata_file(fake_dirs, fake_nginx_ok):
    site_build.main(["create", "messiah", "--domain", "messiah.example.org"])

    meta_path = fake_dirs["web_root"] / "messiah" / site_build.METADATA_FILENAME
    assert meta_path.is_file()
    meta = json.loads(meta_path.read_text())
    assert meta["name"] == "messiah"
    assert meta["domain"] == "messiah.example.org"
    assert "git" not in meta


# --- create --git ---

def test_create_with_git_clones_into_repo_subdir(fake_dirs, fake_nginx_ok, monkeypatch):
    def fake_run(cmd, cwd=None, env=None, **kwargs):
        if cmd[0] == "git" and cmd[1] == "clone":
            dest = Path(cmd[-1])
            (dest / "public").mkdir(parents=True)
            (dest / ".git").mkdir()
            return subprocess.CompletedProcess(cmd, returncode=0, stdout="Cloning...\n", stderr="")
        return subprocess.CompletedProcess(cmd, returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(site_build.subprocess, "run", fake_run)

    exit_code = site_build.main([
        "create", "messiah", "--domain", "messiah.example.org",
        "--git", "git@github.com:parish/messiah-site.git", "--branch", "main",
    ])

    assert exit_code == 0
    repo_dir = fake_dirs["web_root"] / "messiah" / "repo"
    assert (repo_dir / ".git").exists()
    assert (repo_dir / "public").exists()


def test_create_with_git_nginx_root_is_repo_public_not_repo(fake_dirs, fake_nginx_ok, monkeypatch):
    """Critical security property: .git must never be inside nginx's served root."""
    def fake_run(cmd, cwd=None, env=None, **kwargs):
        if cmd[0] == "git" and cmd[1] == "clone":
            dest = Path(cmd[-1])
            (dest / "public").mkdir(parents=True)
            (dest / ".git").mkdir()
            return subprocess.CompletedProcess(cmd, returncode=0, stdout="ok", stderr="")
        return subprocess.CompletedProcess(cmd, returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(site_build.subprocess, "run", fake_run)

    site_build.main([
        "create", "messiah", "--domain", "messiah.example.org",
        "--git", "git@github.com:parish/messiah-site.git",
    ])

    config_text = (fake_dirs["sites_available"] / "messiah").read_text()
    repo_dir = fake_dirs["web_root"] / "messiah" / "repo"
    public_dir = repo_dir / "public"
    assert f"root {public_dir};" in config_text
    assert f"root {repo_dir};" not in config_text  # would expose .git if this were the root


def test_create_with_git_clone_uses_branch_flag(fake_dirs, fake_nginx_ok, monkeypatch):
    clone_calls = []

    def fake_run(cmd, cwd=None, env=None, **kwargs):
        if cmd[0] == "git" and cmd[1] == "clone":
            clone_calls.append(cmd)
            dest = Path(cmd[-1])
            (dest / "public").mkdir(parents=True)
            (dest / ".git").mkdir()
            return subprocess.CompletedProcess(cmd, returncode=0, stdout="ok", stderr="")
        return subprocess.CompletedProcess(cmd, returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(site_build.subprocess, "run", fake_run)

    site_build.main([
        "create", "messiah", "--domain", "messiah.example.org",
        "--git", "git@github.com:parish/messiah-site.git", "--branch", "develop",
    ])

    assert clone_calls
    assert "--branch" in clone_calls[0]
    assert "develop" in clone_calls[0]


def test_create_with_git_rolls_back_entire_site_dir_on_clone_failure(fake_dirs, fake_nginx_ok, monkeypatch):
    def fake_run(cmd, cwd=None, env=None, **kwargs):
        if cmd[0] == "git" and cmd[1] == "clone":
            return subprocess.CompletedProcess(cmd, returncode=128, stdout="", stderr="Permission denied (publickey).")
        return subprocess.CompletedProcess(cmd, returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(site_build.subprocess, "run", fake_run)

    exit_code = site_build.main([
        "create", "messiah", "--domain", "messiah.example.org",
        "--git", "git@github.com:parish/private-repo.git",
    ])

    assert exit_code != 0
    assert not (fake_dirs["web_root"] / "messiah").exists()  # metadata + everything rolled back
    assert not (fake_dirs["sites_available"] / "messiah").exists()


def test_git_env_sets_accept_new_host_key(fake_dirs):
    env = site_build.git_env_for_ssh()
    assert "StrictHostKeyChecking=accept-new" in env["GIT_SSH_COMMAND"]


# --- update ---

def test_update_fails_when_metadata_missing(fake_dirs):
    exit_code = site_build.main(["update", "ghost"])
    assert exit_code != 0


def test_update_fails_when_not_git_linked(fake_dirs):
    make_site(fake_dirs, "plainsite")  # no git_repo
    exit_code = site_build.main(["update", "plainsite"])
    assert exit_code != 0


def test_update_runs_fetch_reset_hard_and_clean(fake_dirs, monkeypatch):
    site_dir, _ = make_site(
        fake_dirs, "messiah", domain="messiah.example.org",
        git_repo="git@github.com:parish/messiah-site.git", git_branch="main",
    )
    repo_dir = site_dir / "repo"
    calls = []

    def fake_run(cmd, cwd=None, env=None, **kwargs):
        calls.append((tuple(cmd), str(cwd) if cwd else None))
        if cmd[:2] == ["git", "rev-parse"]:
            return subprocess.CompletedProcess(cmd, returncode=0, stdout="a1b2c3d\n", stderr="")
        return subprocess.CompletedProcess(cmd, returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(site_build.subprocess, "run", fake_run)
    monkeypatch.setattr(site_build, "verify_site_serving", lambda d: [])

    exit_code = site_build.main(["update", "messiah"])

    assert exit_code == 0
    cmd_list = [c for c, _cwd in calls]
    assert ("git", "fetch", "origin") in cmd_list
    assert ("git", "reset", "--hard", "origin/main") in cmd_list
    assert ("git", "clean", "-fd") in cmd_list
    for cmd, cwd in calls:
        if cmd[1] in ("fetch", "reset", "clean"):
            assert cwd == str(repo_dir)


def test_update_uses_correct_branch_from_metadata(fake_dirs, monkeypatch):
    make_site(
        fake_dirs, "messiah", git_repo="git@github.com:parish/x.git", git_branch="develop",
    )
    calls = []

    def fake_run(cmd, cwd=None, env=None, **kwargs):
        calls.append(tuple(cmd))
        return subprocess.CompletedProcess(cmd, returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(site_build.subprocess, "run", fake_run)
    monkeypatch.setattr(site_build, "verify_site_serving", lambda d: [])

    site_build.main(["update", "messiah"])

    assert ("git", "reset", "--hard", "origin/develop") in calls


def test_update_fails_cleanly_when_fetch_fails(fake_dirs, monkeypatch):
    make_site(fake_dirs, "messiah", git_repo="git@github.com:parish/x.git")

    def fake_run(cmd, cwd=None, env=None, **kwargs):
        if cmd[:2] == ["git", "fetch"]:
            return subprocess.CompletedProcess(cmd, returncode=1, stdout="", stderr="Could not resolve host")
        return subprocess.CompletedProcess(cmd, returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(site_build.subprocess, "run", fake_run)

    exit_code = site_build.main(["update", "messiah"])

    assert exit_code != 0


def test_update_no_nginx_reload_needed(fake_dirs, monkeypatch, capsys):
    """Static file updates shouldn't touch nginx at all."""
    make_site(fake_dirs, "messiah", git_repo="git@github.com:parish/x.git")

    reload_calls = []

    def fake_run(cmd, cwd=None, env=None, **kwargs):
        if cmd[:2] == ["systemctl", "reload"]:
            reload_calls.append(cmd)
        return subprocess.CompletedProcess(cmd, returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(site_build.subprocess, "run", fake_run)
    monkeypatch.setattr(site_build, "verify_site_serving", lambda d: [])

    site_build.main(["update", "messiah"])

    assert reload_calls == []


# --- list ---

def test_list_shows_no_sites_when_empty(fake_dirs, capsys):
    exit_code = site_build.main(["list"])
    out = capsys.readouterr().out

    assert exit_code == 0
    assert "No sites found." in out


def test_list_shows_domain_from_metadata_and_git_status(fake_dirs, capsys):
    make_site(fake_dirs, "messiah", domain="messiah.example.org", enabled=True,
              git_repo="git@github.com:parish/x.git")
    make_site(fake_dirs, "stmarks", domain="stmarks.example.org", enabled=False)

    exit_code = site_build.main(["list"])
    out = capsys.readouterr().out

    assert exit_code == 0
    assert "messiah" in out and "messiah.example.org" in out
    assert "stmarks" in out and "stmarks.example.org" in out


def test_list_falls_back_to_nginx_config_when_metadata_missing(fake_dirs, capsys):
    """Sites created before v0.4 (no metadata file) should still show up."""
    (fake_dirs["sites_available"] / "legacy").write_text(
        "server {\n    server_name legacy.example.org;\n}\n"
    )

    exit_code = site_build.main(["list"])
    out = capsys.readouterr().out

    assert exit_code == 0
    assert "legacy" in out and "legacy.example.org" in out


# --- status ---

def test_status_single_unknown_site(fake_dirs, capsys):
    exit_code = site_build.main(["status", "nonexistent"])
    out = capsys.readouterr().out

    assert exit_code != 0
    assert "No such site" in out


def test_status_single_shows_git_info_when_linked(fake_dirs, monkeypatch, capsys):
    make_site(fake_dirs, "messiah", domain="messiah.example.org", enabled=True,
              git_repo="git@github.com:parish/messiah-site.git", git_branch="main")
    monkeypatch.setattr(site_build, "verify_site_serving", lambda d: [])

    def fake_run(cmd, cwd=None, env=None, **kwargs):
        if cmd[:2] == ["git", "rev-parse"]:
            return subprocess.CompletedProcess(cmd, returncode=0, stdout="a1b2c3d\n", stderr="")
        return subprocess.CompletedProcess(cmd, returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(site_build.subprocess, "run", fake_run)

    exit_code = site_build.main(["status", "messiah"])
    out = capsys.readouterr().out

    assert exit_code == 0
    assert "a1b2c3d" in out
    assert "git@github.com:parish/messiah-site.git" in out
    assert "main" in out


def test_status_single_shows_not_linked_when_no_git(fake_dirs, monkeypatch, capsys):
    make_site(fake_dirs, "messiah", domain="messiah.example.org", enabled=True)
    monkeypatch.setattr(site_build, "verify_site_serving", lambda d: [])
    monkeypatch.setattr(site_build.subprocess, "run",
                         lambda cmd, **kw: subprocess.CompletedProcess(cmd, returncode=0, stdout="ok", stderr=""))

    exit_code = site_build.main(["status", "messiah"])
    out = capsys.readouterr().out

    assert exit_code == 0
    assert "not linked" in out


def test_status_all_shows_table(fake_dirs, monkeypatch, capsys):
    make_site(fake_dirs, "messiah", domain="messiah.example.org", enabled=True)
    make_site(fake_dirs, "stmarks", domain="stmarks.example.org", enabled=False)
    monkeypatch.setattr(site_build, "verify_site_serving", lambda d: [])
    monkeypatch.setattr(site_build.subprocess, "run",
                         lambda cmd, **kw: subprocess.CompletedProcess(cmd, returncode=0, stdout="ok", stderr=""))

    exit_code = site_build.main(["status"])
    out = capsys.readouterr().out

    assert exit_code == 0
    assert "messiah" in out
    assert "stmarks" in out


# --- enable / disable ---

def test_enable_unknown_site_fails(fake_dirs):
    exit_code = site_build.main(["enable", "nonexistent"])
    assert exit_code != 0


def test_enable_already_enabled_is_a_noop_success(fake_dirs, fake_nginx_ok):
    make_site(fake_dirs, "messiah", enabled=True)
    exit_code = site_build.main(["enable", "messiah"])
    assert exit_code == 0


def test_enable_creates_symlink_and_reloads(fake_dirs, fake_nginx_ok):
    make_site(fake_dirs, "messiah", enabled=False)
    exit_code = site_build.main(["enable", "messiah"])

    assert exit_code == 0
    assert (fake_dirs["sites_enabled"] / "messiah").is_symlink()


def test_disable_removes_symlink_and_reloads(fake_dirs, fake_nginx_ok):
    make_site(fake_dirs, "messiah", enabled=True)
    exit_code = site_build.main(["disable", "messiah"])

    assert exit_code == 0
    assert not (fake_dirs["sites_enabled"] / "messiah").exists()
    assert (fake_dirs["sites_available"] / "messiah").exists()  # config stays


def test_disable_already_disabled_is_a_noop_success(fake_dirs, fake_nginx_ok):
    make_site(fake_dirs, "messiah", enabled=False)
    exit_code = site_build.main(["disable", "messiah"])
    assert exit_code == 0


# --- remove ---

def test_remove_unknown_site_fails(fake_dirs):
    exit_code = site_build.main(["remove", "nonexistent", "--yes"])
    assert exit_code != 0


def test_remove_with_yes_deletes_everything_including_repo_and_metadata(fake_dirs, fake_nginx_ok):
    make_site(fake_dirs, "messiah", enabled=True, git_repo="git@github.com:parish/x.git")

    exit_code = site_build.main(["remove", "messiah", "--yes"])

    assert exit_code == 0
    assert not (fake_dirs["sites_available"] / "messiah").exists()
    assert not (fake_dirs["sites_enabled"] / "messiah").exists()
    assert not (fake_dirs["web_root"] / "messiah").exists()  # includes repo/ and metadata


def test_remove_keep_files_preserves_site_dir(fake_dirs, fake_nginx_ok):
    make_site(fake_dirs, "messiah", enabled=True)

    exit_code = site_build.main(["remove", "messiah", "--yes", "--keep-files"])

    assert exit_code == 0
    assert not (fake_dirs["sites_available"] / "messiah").exists()
    assert (fake_dirs["web_root"] / "messiah").exists()


def test_remove_without_yes_prompts_and_respects_no(fake_dirs, fake_nginx_ok, monkeypatch):
    make_site(fake_dirs, "messiah", enabled=True)
    monkeypatch.setattr("builtins.input", lambda prompt: "n")

    exit_code = site_build.main(["remove", "messiah"])

    assert exit_code != 0
    assert (fake_dirs["sites_available"] / "messiah").exists()


def test_remove_without_yes_prompts_and_respects_yes(fake_dirs, fake_nginx_ok, monkeypatch):
    make_site(fake_dirs, "messiah", enabled=True)
    monkeypatch.setattr("builtins.input", lambda prompt: "y")

    exit_code = site_build.main(["remove", "messiah"])

    assert exit_code == 0
    assert not (fake_dirs["sites_available"] / "messiah").exists()


# --- restart prompt behavior (unchanged, via create) ---

def test_prompts_and_restarts_when_default_page_detected(fake_dirs, fake_nginx_ok, monkeypatch):
    calls = {"count": 0}

    def fake_verify(domain):
        calls["count"] += 1
        if calls["count"] == 1:
            return ["IPv6: still serving the default nginx page"]
        return []

    monkeypatch.setattr(site_build, "verify_site_serving", fake_verify)
    monkeypatch.setattr("builtins.input", lambda prompt: "y")

    restart_calls = []

    def fake_run(cmd, **kwargs):
        if cmd[:2] == ["systemctl", "restart"]:
            restart_calls.append(cmd)
        return subprocess.CompletedProcess(cmd, returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(site_build.subprocess, "run", fake_run)

    exit_code = site_build.main(["create", "stmarks", "--domain", "stmarks.example.org"])

    assert exit_code == 0
    assert len(restart_calls) == 1
    assert calls["count"] == 2


def test_declines_restart_leaves_site_configured(fake_dirs, fake_nginx_ok, monkeypatch):
    monkeypatch.setattr(site_build, "verify_site_serving", lambda domain: ["IPv6: default page"])
    monkeypatch.setattr("builtins.input", lambda prompt: "n")

    restart_calls = []

    def fake_run(cmd, **kwargs):
        if cmd[:2] == ["systemctl", "restart"]:
            restart_calls.append(cmd)
        return subprocess.CompletedProcess(cmd, returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(site_build.subprocess, "run", fake_run)

    exit_code = site_build.main(["create", "stmarks", "--domain", "stmarks.example.org"])

    assert exit_code == 0
    assert len(restart_calls) == 0
    assert (fake_dirs["sites_available"] / "stmarks").is_file()
