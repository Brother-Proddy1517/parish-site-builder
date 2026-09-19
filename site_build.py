#!/usr/bin/env python3
"""
Parish Site Builder
v0.3 — pre-flight validation, then create a web root directory AND a
working nginx site.

Before touching anything, checks: valid site name, valid domain, nginx
installed and running, no existing web dir or config, and that the domain
isn't already claimed by a different site. If anything fails, nothing is
created or changed.

Usage:
    site_build.py <name> [--domain DOMAIN]

Example:
    site_build.py messiah --domain messiah.example.org
        -> validates everything first
        -> creates /srv/www/messiah
        -> writes /etc/nginx/sites-available/messiah
        -> symlinks it into /etc/nginx/sites-enabled/messiah
        -> tests nginx config, reloads nginx if valid
        -> verifies the site is actually reachable

    site_build.py messiah
        -> same, but domain defaults to "messiah"
"""

import argparse
import http.client
import re
import shutil
import subprocess
import sys
from pathlib import Path

WEB_ROOT_BASE = Path("/srv/www")
NGINX_SITES_AVAILABLE = Path("/etc/nginx/sites-available")
NGINX_SITES_ENABLED = Path("/etc/nginx/sites-enabled")

# Marker text from Debian/Ubuntu's default nginx page. If we see this when
# we expected the site we just created, nginx is still routing the request
# to the default site instead of the new one.
DEFAULT_NGINX_MARKER = "Welcome to nginx!"

# Site name: safe as both a directory name and an nginx config filename.
SITE_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,62}$")

# Domain / hostname: standard DNS label rules, dot-separated. Also accepts a
# single bare label (e.g. "messiah") since that's what --domain defaults to
# for local/VM testing without a real DNS name.
DOMAIN_PATTERN = re.compile(
    r"^[a-zA-Z0-9]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?"
    r"(\.[a-zA-Z0-9]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?)*$"
)

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


def is_valid_site_name(name: str) -> bool:
    return bool(SITE_NAME_PATTERN.match(name))


def is_valid_domain(domain: str) -> bool:
    return bool(DOMAIN_PATTERN.match(domain)) and len(domain) <= 253


def nginx_is_running() -> bool:
    try:
        result = subprocess.run(
            ["systemctl", "is-active", "nginx"], capture_output=True, text=True, check=False
        )
        return result.returncode == 0
    except FileNotFoundError:
        return False


def find_domain_conflict(domain: str) -> str | None:
    """
    Scan existing nginx site configs for one that already claims this exact
    domain as a server_name. Returns the conflicting site's filename, or
    None if the domain is free.

    This catches the case our other checks miss: two different site *names*
    (e.g. "messiah" and "messiah2") both trying to claim the same domain.
    """
    if not NGINX_SITES_AVAILABLE.exists():
        return None

    server_name_re = re.compile(r"server_name\s+([^;]+);")

    for config_file in NGINX_SITES_AVAILABLE.iterdir():
        if not config_file.is_file():
            continue
        try:
            text = config_file.read_text()
        except OSError:
            continue
        for match in server_name_re.finditer(text):
            names = match.group(1).split()
            if domain in names:
                return config_file.name
    return None


def run_preflight_checks(name: str, domain: str) -> list[tuple[str, bool, str]]:
    """
    Run every check before anything is created or changed.

    Returns a list of (label, passed, detail) tuples. `detail` is a
    human-readable reason, only meaningful when passed is False.
    """
    checks = []

    checks.append((
        "Valid site name",
        is_valid_site_name(name),
        f"'{name}' must start with a letter/number and contain only letters, "
        "numbers, hyphens, and underscores (max 63 characters).",
    ))

    checks.append((
        "Valid domain",
        is_valid_domain(domain),
        f"'{domain}' doesn't look like a valid domain/hostname.",
    ))

    installed = nginx_installed()
    checks.append((
        "nginx installed",
        installed,
        "nginx is not installed on this system.",
    ))

    if installed:
        running = nginx_is_running()
        checks.append((
            "nginx running",
            running,
            "nginx is installed but not running (try: sudo systemctl start nginx).",
        ))
    else:
        # Don't bother checking if it's running when it isn't even installed.
        checks.append(("nginx running", False, "nginx is not installed."))

    web_exists = (WEB_ROOT_BASE / name).exists()
    checks.append((
        "Web directory doesn't already exist",
        not web_exists,
        f"{WEB_ROOT_BASE / name} already exists.",
    ))

    config_exists = (NGINX_SITES_AVAILABLE / name).exists() or (NGINX_SITES_ENABLED / name).exists()
    checks.append((
        "No existing site configuration",
        not config_exists,
        f"An nginx config for '{name}' already exists.",
    ))

    conflict = find_domain_conflict(domain)
    checks.append((
        "Domain not already in use",
        conflict is None,
        f"Domain '{domain}' is already used by site config '{conflict}'." if conflict else "",
    ))

    return checks


def print_preflight_results(checks: list[tuple[str, bool, str]]) -> bool:
    """Print the checklist, return True if every check passed."""
    print("Checking...")
    print()
    all_ok = True
    for label, ok, _detail in checks:
        symbol = "\u2713" if ok else "\u2717"
        print(f"{symbol} {label}")
        if not ok:
            all_ok = False

    if not all_ok:
        print()
        print("ERROR")
        print()
        for label, ok, detail in checks:
            if not ok:
                print(detail)
        print()
        print("No changes were made.")

    return all_ok


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


def restart_nginx() -> tuple[bool, str]:
    """Run `systemctl restart nginx`. Returns (success, output)."""
    try:
        result = subprocess.run(
            ["systemctl", "restart", "nginx"], capture_output=True, text=True, check=False
        )
        output = (result.stdout or "") + (result.stderr or "")
        return result.returncode == 0, output
    except FileNotFoundError:
        return False, "systemctl command not found"


def verify_site_serving(domain: str) -> list[str]:
    """
    Probe the site over both IPv4 and IPv6 loopback with the right Host
    header, and check whether nginx is actually routing to it.

    `reload` re-reads config files but doesn't reliably rebind newly added
    listen sockets, so a config that tests clean and reloads without error
    can still silently fall through to the default site. This catches that.

    Returns a list of human-readable problems. Empty list = looks fine.
    """
    problems = []
    for family_name, host in [("IPv4", "127.0.0.1"), ("IPv6", "::1")]:
        try:
            conn = http.client.HTTPConnection(host, 80, timeout=3)
            conn.putrequest("GET", "/", skip_host=True)
            conn.putheader("Host", domain)
            conn.endheaders()
            resp = conn.getresponse()
            body = resp.read(4096).decode(errors="replace")
            conn.close()
            if DEFAULT_NGINX_MARKER in body:
                problems.append(
                    f"{family_name}: still serving the default nginx page, not {domain}"
                )
        except OSError as e:
            problems.append(f"{family_name}: could not connect ({e})")
    return problems


def prompt_yes_no(question: str) -> bool:
    answer = input(f"{question} [y/N]: ").strip().lower()
    return answer == "y"


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

    # --- Verify the site is actually reachable, not just configured ---
    problems = verify_site_serving(domain)
    if problems:
        print()
        print("WARNING: the site may not be serving correctly yet:")
        for p in problems:
            print(f"  - {p}")
        print(
            "This usually happens when 'reload' doesn't rebind a newly added "
            "listen socket. A full restart fixes it, but briefly disconnects "
            "every site on this server, not just this one."
        )
        if prompt_yes_no("Restart nginx now to fix this?"):
            ok, output = restart_nginx()
            if not ok:
                print("ERROR: nginx restart failed.", file=sys.stderr)
                print(output, file=sys.stderr)
                return 1
            print("Restarted nginx")

            problems = verify_site_serving(domain)
            if problems:
                print("WARNING: site still doesn't look right after restart:")
                for p in problems:
                    print(f"  - {p}")
            else:
                print("Verified: site is serving correctly.")
        else:
            print(
                "Skipped restart. The config is valid and enabled, but the site "
                "may not be reachable until you run: sudo systemctl restart nginx"
            )
    else:
        print("Verified: site is serving correctly.")

    return 0


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    domain = args.domain or args.name

    checks = run_preflight_checks(args.name, domain)
    if not print_preflight_results(checks):
        return 1

    print()
    print("Creating site...")
    print()

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
