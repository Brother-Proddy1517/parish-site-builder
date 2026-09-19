# parish-site-manager

A tool for creating and managing small parish websites, built up one feature
at a time.

## Status: v0.1

Creates a web root directory under `/srv/www`. That's it. No nginx yet.

```bash
python3 site_build.py messiah
```

Creates `/srv/www/messiah`.

If the directory already exists, or `/srv/www` itself doesn't exist, the
script exits with an error and makes **no changes**. This "no changes made"
principle carries through every later version — an admin tool should never
leave things half-done when something's wrong.

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
- [ ] v0.2 — generate nginx config, symlink into sites-enabled, test + reload nginx
- [ ] v0.3 — pre-flight validation (port checks, existing config checks, etc.)
- [ ] v0.4 — subcommands: `create`, `list`, `status`, `enable`, `disable`, `remove`
- [ ] v0.5 — SSL via Let's Encrypt / Certbot, cert expiry in `status`

## Testing workflow

1. Develop and test against a local Ubuntu/Debian VM.
2. Once confident, point at the real VPS.
