#!/usr/bin/env python3
"""
Parish Site Builder
v0.2 — create a web root directory AND a working nginx site.

Usage:
    site_build.py <name> [--domain DOMAIN]

Example:
    site_build.py messiah --domain messiah.example.org
        -> creates /srv/www/messiah
        -> writes /etc/nginx/sites-available/messiah
        -> symlinks it into /etc/nginx/sites-enabled/messiah
        -> tests nginx config, reloads nginx if valid

    site_build.py messiah
        -> same, but domain defaults to "messiah"
"""

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

WEB_ROOT_BASE = Path("/srv/www")
NGINX_SITES_AVAILABLE = Path("/etc/nginx/sites-available")
NGINX_SITES_ENABLED = Path("/etc/nginx/sites-enabled")

NGINX_TEMPLATE = """server {{
    listen 80;
    listen [::]:80;
    server_name {domain};

    root {web_root};
    index index.html;

    location / {{
        try_files $uri $uri/ =404;
    }}
}}
"""

PLACEHOLDER_INDEX = """<!DOCTYPE html>
<html>
<head><title>{domain}</title></head>
<body>
<h1>{domain}</h1>
<p>This site was created by parish-site-builder. Replace this page with your real content.</p>
</body>
</html>
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="site_build.py",
        description="Parish Site Builder — create and manage parish website directories and nginx configs.",
    )
    parser.add_argument(
        "name",
        help="Site name, e.g. 'messiah'. Used as the directory name under /srv/www "
             "and as the nginx config filename.",
    )
    parser.add_argument(
        "--domain",
        help="Domain for the nginx server_name, e.g. 'messiah.example.org'. "
             "Defaults to the site name if omitted.",
    )
    return parser


def create_site_directory(name: str) -> int:
    """
    Create /srv/www/<name> and drop in a placeholder index.html.

    Returns an exit code: 0 on success, non-zero on failure.
    Follows the "no changes made" principle — if anything is wrong,
    we bail out before touching the filesystem.
    """
    site_path = WEB_ROOT_BASE / name

    # --- Validation before any changes ---
    if site_path.exists():
        print(f"ERROR: {site_path} already exists. No changes were made.", file=sys.stderr)
        return 1

    if not WEB_ROOT_BASE.exists():
        print(
            f"ERROR: {WEB_ROOT_BASE} does not exist. Create it first (e.g. sudo mkdir -p {WEB_ROOT_BASE}). "
            "No changes were made.",
            file=sys.stderr,
        )
        return 1

    # --- Do the thing ---
    try:
        site_path.mkdir(parents=False, exist_ok=False)
    except PermissionError:
        print(
            f"ERROR: permission denied creating {site_path}. "
            "Try running with sudo. No changes were made.",
            file=sys.stderr,
        )
        return 1
    except OSError as e:
        print(f"ERROR: failed to create {site_path}: {e}", file=sys.stderr)
        return 1

    # TODO(v0.3+): set ownership to www-data:www-data.
    print(f"Created {site_path}")
    return 0


def write_placeholder_index(name: str, domain: str) -> None:
    """Best-effort: drop a placeholder index.html so the site isn't a bare 404."""
    index_path = WEB_ROOT_BASE / name / "index.html"
    try:
        index_path.write_text(PLACEHOLDER_INDEX.format(domain=domain))
        print(f"Created {index_path}")
    except OSError as e:
        # Not fatal — the site still works, it'll just 404 until real content is added.
        print(f"WARNING: could not write placeholder index.html: {e}", file=sys.stderr)


def nginx_installed() -> bool:
    return shutil.which("nginx") is not None


def test_nginx_config() -> tuple[bool, str]:
    """Run `nginx -t`. Returns (success, output)."""
    try:
        result = subprocess.run(
            ["nginx", "-t"], capture_output=True, text=True, check=False
        )
        output = (result.stdout or "") + (result.stderr or "")
        return result.returncode == 0, output
    except FileNotFoundError:
        return False, "nginx command not found"


def reload_nginx() -> tuple[bool, str]:
    """Run `systemctl reload nginx`. Returns (success, output)."""
    try:
        result = subprocess.run(
            ["systemctl", "reload", "nginx"], capture_output=True, text=True, check=False
        )
        output = (result.stdout or "") + (result.stderr or "")
        return result.returncode == 0, output
    except FileNotFoundError:
        return False, "systemctl command not found"


def create_nginx_site(name: str, domain: str) -> int:
    """
    Generate an nginx config for <name>, enable it, test it, and reload nginx.

    Rolls back (removes config + symlink) if the nginx test fails, so a bad
    config for this site never gets a chance to break nginx for others.
    """
    if not nginx_installed():
        print("ERROR: nginx is not installed. No changes were made.", file=sys.stderr)
        return 1

    config_path = NGINX_SITES_AVAILABLE / name
    symlink_path = NGINX_SITES_ENABLED / name
    web_root = WEB_ROOT_BASE / name

    # --- Validation before any changes ---
    if config_path.exists():
        print(
            f"ERROR: {config_path} already exists. No changes were made.",
            file=sys.stderr,
        )
        return 1

    if symlink_path.exists():
        print(
            f"ERROR: {symlink_path} already exists. No changes were made.",
            file=sys.stderr,
        )
        return 1

    # --- Write the config ---
    config_text = NGINX_TEMPLATE.format(domain=domain, web_root=web_root)
    try:
        config_path.write_text(config_text)
    except PermissionError:
        print(
            f"ERROR: permission denied writing {config_path}. Try running with sudo. "
            "No changes were made.",
            file=sys.stderr,
        )
        return 1
    except OSError as e:
        print(f"ERROR: failed to write {config_path}: {e}", file=sys.stderr)
        return 1
    print(f"Created {config_path}")

    # --- Symlink it into sites-enabled ---
    try:
        symlink_path.symlink_to(config_path)
    except OSError as e:
        config_path.unlink(missing_ok=True)
        print(f"ERROR: failed to symlink {symlink_path}: {e}. Rolled back.", file=sys.stderr)
        return 1
    print(f"Enabled {symlink_path}")

    # --- Test the config before touching the running nginx ---
    ok, output = test_nginx_config()
    if not ok:
        symlink_path.unlink(missing_ok=True)
        config_path.unlink(missing_ok=True)
        print("ERROR: nginx config test failed. Rolled back, nginx was NOT reloaded.", file=sys.stderr)
        print(output, file=sys.stderr)
        return 1
    print("nginx config test passed")

    # --- Reload nginx ---
    ok, output = reload_nginx()
    if not ok:
        # Config is valid and in place, it just didn't take effect yet.
        # Don't roll back — the admin can reload manually once they see this.
        print(
            "WARNING: nginx config is valid but reload failed. "
            "Run 'sudo systemctl reload nginx' manually.",
            file=sys.stderr,
        )
        print(output, file=sys.stderr)
        return 1
    print("Reloaded nginx")

    return 0


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    domain = args.domain or args.name

    rc = create_site_directory(args.name)
    if rc != 0:
        return rc

    write_placeholder_index(args.name, domain)

    rc = create_nginx_site(args.name, domain)
    if rc != 0:
        return rc

    print()
    print("Site created successfully.")
    print(f"    Web root: {WEB_ROOT_BASE / args.name}")
    print(f"    Config:   {NGINX_SITES_AVAILABLE / args.name}")
    print(f"    Status:   http://{domain}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
