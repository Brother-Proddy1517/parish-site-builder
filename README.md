# parish-site-builder

A tool for creating and managing small parish websites, built up one feature
at a time.

## Status: v0.3

Runs a full pre-flight check before touching anything, then creates the web
root, nginx config, enables it, tests it, reloads nginx, and verifies the
site is actually reachable.

```bash
sudo python3 site_build.py messiah --domain messiah.example.org
```

Pre-flight checks (all must pass or nothing is created):
- Valid site name (safe as a directory name and nginx config filename)
- Valid domain/hostname
- nginx installed
- nginx running
- No existing web directory for this site
- No existing nginx config for this site
- Domain isn't already claimed by a *different* site's config

If any check fails, you get a full list of what's wrong and **no changes
are made** — nothing partial, nothing to clean up.

`--domain` is optional — if you omit it, the domain defaults to the site
name (useful for quick local/VM testing).

Requires `sudo` since it writes to `/srv/www` and `/etc/nginx`, and reloads
the nginx service.

If anything fails partway through the nginx steps (bad config, `nginx -t`
failing, etc.), the tool **rolls back** — removing the config file and the
symlink it just created — so a broken new site never risks breaking nginx
for sites that were already running.

After reload, the tool also verifies the site is actually reachable over
both IPv4 and IPv6 (catches a real nginx quirk where `reload` doesn't
always rebind newly added listen sockets). If it's not, you'll be prompted
to restart nginx — which fixes it, but briefly disconnects every site on
the box, not just the new one.

This "no changes made" principle carries through every version — an admin
tool should never leave things half-done when something's wrong.

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
- [x] v0.3 — pre-flight validation (site name, domain, nginx status, existing config, domain conflicts)
- [ ] v0.4 — subcommands: `create`, `list`, `status`, `enable`, `disable`, `remove`
- [ ] v0.5 — SSL via Let's Encrypt / Certbot, cert expiry in `status`

## Testing workflow

1. Develop and test against a local Ubuntu/Debian VM.
2. Once confident, point at the real VPS.
