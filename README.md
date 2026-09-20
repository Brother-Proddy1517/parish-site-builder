# parish-site-builder

A tool for creating and managing small parish websites, built up one feature
at a time.

## Status: v0.3.1

Same behavior as v0.3 — pre-flight validation, nginx site creation,
rollback on failure, post-reload verification — with much clearer CLI
output. Every check and every change is reported explicitly with a
PASS/FAIL/OK indicator and a full path, instead of terse one-line status
messages.

```bash
sudo python3 site_build.py messiah --domain messiah.example.org
```

Example output:

```
Site:   messiah
Domain: messiah.example.org

Preflight checks:
  [PASS] Valid site name
  [PASS] Valid domain
  [PASS] nginx installed: /usr/sbin/nginx
  [PASS] nginx running
  [PASS] Site directory available
  [PASS] Nginx configuration available
  [PASS] Domain not already in use

Creating site:
  [OK] Created directory: /srv/www/messiah
  [OK] Created index: /srv/www/messiah/index.html
  [OK] Created Nginx config: /etc/nginx/sites-available/messiah
  [OK] Enabled site: /etc/nginx/sites-enabled/messiah -> /etc/nginx/sites-available/messiah

Validating Nginx configuration:
  [PASS] nginx -t
  [OK] Reloaded nginx

Verifying site:
  [PASS] messiah.example.org is being served correctly

Site created successfully.

Site:   messiah
Domain: messiah.example.org

Created:
  /srv/www/messiah
  /srv/www/messiah/index.html
  /etc/nginx/sites-available/messiah
  /etc/nginx/sites-enabled/messiah
```

If any pre-flight check fails, you get the full list with `[FAIL]` and a
reason, and **no changes are made**.

`--domain` is optional — if you omit it, the domain defaults to the site
name (useful for quick local/VM testing).

Requires `sudo` since it writes to `/srv/www` and `/etc/nginx`, and reloads
the nginx service.

If `nginx -t` fails, the tool **rolls back** — removing the config file and
the symlink it just created — so a broken new site never risks breaking
nginx for sites that were already running.

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
- [x] v0.3.1 — explicit, path-by-path CLI output for checks and changes
- [ ] v0.4 — subcommands: `create`, `list`, `status`, `enable`, `disable`, `remove`
- [ ] v0.5 — SSL via Let's Encrypt / Certbot, cert expiry in `status`

## Testing workflow

1. Develop and test against a local Ubuntu/Debian VM.
2. Once confident, point at the real VPS.
