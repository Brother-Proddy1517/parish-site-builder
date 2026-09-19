# parish-site-builder

A tool for creating and managing small parish websites, built up one feature
at a time.

## Status: v0.2

Creates a web root directory under `/srv/www`, generates an nginx config for
it, enables the site, tests the config, and reloads nginx.

```bash
sudo python3 site_build.py messiah --domain messiah.example.org
```

This:
- creates `/srv/www/messiah` with a placeholder `index.html`
- writes `/etc/nginx/sites-available/messiah` from a template
- symlinks it into `/etc/nginx/sites-enabled/messiah`
- runs `nginx -t` to validate the config
- reloads nginx if valid

`--domain` is optional — if you omit it, the domain defaults to the site
name (useful for quick local/VM testing).

Requires `sudo` since it writes to `/srv/www` and `/etc/nginx`, and reloads
the nginx service.

If anything fails partway through the nginx steps (bad config, `nginx -t`
failing, etc.), the tool **rolls back** — removing the config file and the
symlink it just created — so a broken new site never risks breaking nginx
for sites that were already running. This "no changes made" principle
carries through every later version — an admin tool should never leave
things half-done when something's wrong.

## Requirements

- Python 3.9+ (uses `pathlib`, no other dependencies)
- `pytest` for running tests (dev only): `pip install pytest --break-system-packages`

## Running tests

```bash
pytest tests/
```

Tests monkeypatch the target directory to a temp folder, so they're safe to
run anywhere — they never touch a real `/srv/www`.

## Roadmap

- [x] v0.1 — create web root directory
- [x] v0.2 — generate nginx config, symlink into sites-enabled, test + reload nginx
- [ ] v0.3 — pre-flight validation (port checks, existing config checks, etc.)
- [ ] v0.4 — subcommands: `create`, `list`, `status`, `enable`, `disable`, `remove`
- [ ] v0.5 — SSL via Let's Encrypt / Certbot, cert expiry in `status`

## Testing workflow

1. Develop and test against a local Ubuntu/Debian VM.
2. Once confident, point at the real VPS.
