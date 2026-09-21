# parish-site-builder

A tool for creating and managing small parish websites, built up one
feature at a time. Currently at **v0.4**.

## Quick start

```bash
sudo python3 site_build.py create messiah --domain messiah.example.org
sudo python3 site_build.py create messiah --domain messiah.example.org \
    --git git@github.com:yourorg/messiah-site.git --branch main
sudo python3 site_build.py list
sudo python3 site_build.py status
sudo python3 site_build.py update messiah
```

### Install as a regular command (optional)

```bash
sudo ./install.sh
sudo site-build list
```

This symlinks `site_build.py` to `/usr/local/bin/site-build`, so you can
run `sudo site-build ...` instead of `sudo python3 site_build.py ...`.
It's a symlink, not a copy, so `git pull` keeps it current automatically.

For the full command reference, see [`MANUAL.md`](MANUAL.md) — a man-page
style document covering every command, every safety guarantee, the
on-disk layout, and git/SSH setup in detail.

## Requirements

- Python 3.9+ (uses `pathlib`, no other dependencies)
- `pytest` for running tests (dev only): `pip install pytest --break-system-packages`
- `nginx` installed and running on the target machine
- `git` installed, if using `--git` on `create` or running `update`

## Running tests

```bash
pytest tests/
```

Tests monkeypatch the target directories (`/srv/www`, `/etc/nginx/...`) to
temp folders, and stub out `nginx`/`systemctl`/`git` subprocess calls, so
the suite is safe to run anywhere — it never touches a real system, and
doesn't require nginx or git to actually be installed.

## Roadmap

- [x] v0.1 — create web root directory
- [x] v0.2 — generate nginx config, symlink into sites-enabled, test + reload nginx
- [x] v0.3 — pre-flight validation (site name, domain, nginx status, existing config, domain conflicts)
- [x] v0.3.1 — explicit, path-by-path CLI output for checks and changes
- [x] v0.4 — subcommands (create/list/status/enable/disable/remove/update) + SSH-based git deployment
- [x] v0.5 — SSL via Let's Encrypt (certbot certonly --webroot), enable-ssl command, cert expiry in `status`
- [x] v0.5.1 — `doctor` command (whole-machine readiness check) + `install.sh` for a `site-build` shortcut

## Testing workflow

1. Develop and test against a local Ubuntu/Debian VM.
2. Once confident, point at the real VPS.
