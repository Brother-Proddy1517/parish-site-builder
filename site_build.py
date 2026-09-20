#!/usr/bin/env python3
"""
Parish Site Builder
v0.3.1 — same behavior as v0.3 (pre-flight validation, nginx site creation,
rollback on failure, post-reload verification), with clearer, more explicit
CLI output. Every check and every filesystem/nginx change is reported by
name and full path, with PASS/FAIL/OK indicators, instead of terse one-line
status messages.

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


# =====================================================================
# Validation (pure functions, no I/O side effects beyond reading state)
# =====================================================================

def is_valid_site_name(name: str) -> bool:
    return bool(SITE_NAME_PATTERN.match(name))


def is_valid_domain(domain: str) -> bool:
    return bool(DOMAIN_PATTERN.match(domain)) and len(domain) <= 253


def nginx_installed() -> bool:
    return shutil.which("nginx") is not None


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


def run_preflight_checks(name: str, domain: str) -> list[dict]:
    """
    Run every check before anything is created or changed.

    Returns a list of dicts: {"label": str, "passed": bool, "detail": str}.
    `detail` may be populated on a pass too (e.g. showing the nginx binary
    path), not just on failure.
    """
    checks = []

    checks.append({
        "label": "Valid site name",
        "passed": is_valid_site_name(name),
        "detail": "" if is_valid_site_name(name) else (
            f"'{name}' must start with a letter/number and contain only letters, "
            "numbers, hyphens, and underscores (max 63 characters)."
        ),
    })

    checks.append({
        "label": "Valid domain",
        "passed": is_valid_domain(domain),
        "detail": "" if is_valid_domain(domain) else (
            f"'{domain}' doesn't look like a valid domain/hostname."
        ),
    })

    nginx_path = shutil.which("nginx")
    installed = nginx_path is not None
    checks.append({
        "label": "nginx installed",
        "passed": installed,
        "detail": nginx_path if installed else "nginx is not installed on this system.",
    })

    if installed:
        running = nginx_is_running()
        checks.append({
            "label": "nginx running",
            "passed": running,
            "detail": "" if running else (
                "nginx is installed but not running (try: sudo systemctl start nginx)."
            ),
        })
    else:
        # Don't bother checking if it's running when it isn't even installed.
        checks.append({"label": "nginx running", "passed": False, "detail": "nginx is not installed."})

    site_dir = WEB_ROOT_BASE / name
    web_exists = site_dir.exists()
    checks.append({
        "label": "Site directory available",
        "passed": not web_exists,
        "detail": "" if not web_exists else f"{site_dir} already exists.",
    })

    config_path = NGINX_SITES_AVAILABLE / name
    symlink_path = NGINX_SITES_ENABLED / name
    config_exists = config_path.exists() or symlink_path.exists()
    checks.append({
        "label": "Nginx configuration available",
        "passed": not config_exists,
        "detail": "" if not config_exists else f"An nginx config for '{name}' already exists.",
    })

    conflict = find_domain_conflict(domain)
    checks.append({
        "label": "Domain not already in use",
        "passed": conflict is None,
        "detail": "" if conflict is None else (
            f"Domain '{domain}' is already used by site config '{conflict}'."
        ),
    })

    return checks


def print_preflight_results(checks: list[dict]) -> bool:
    """Print the checklist, return True if every check passed."""
    print("Preflight checks:")
    all_ok = True
    for check in checks:
        status = "PASS" if check["passed"] else "FAIL"
        line = f"  [{status}] {check['label']}"
        if check["detail"]:
            line += f": {check['detail']}"
        print(line)
        if not check["passed"]:
            all_ok = False

    if not all_ok:
        print()
        print("No changes were made.")

    return all_ok


# =====================================================================
# Site creation — each function does one thing and reports exactly what
# it did (or didn't) do, with full paths.
# =====================================================================

def create_web_root(name: str) -> tuple[int, Path | None]:
    """
    Create /srv/www/<name>.
    Returns (exit_code, path_or_None).
    """
    site_path = WEB_ROOT_BASE / name

    if site_path.exists():
        print(f"  [FAIL] Create directory: {site_path} already exists")
        return 1, None

    if not WEB_ROOT_BASE.exists():
        print(
            f"  [FAIL] Create directory: {WEB_ROOT_BASE} does not exist "
            f"(create it first, e.g. sudo mkdir -p {WEB_ROOT_BASE})"
        )
        return 1, None

    try:
        site_path.mkdir(parents=False, exist_ok=False)
    except PermissionError:
        print(f"  [FAIL] Create directory: permission denied writing {site_path} (try sudo)")
        return 1, None
    except OSError as e:
        print(f"  [FAIL] Create directory: {site_path}: {e}")
        return 1, None

    # TODO(v0.4+): set ownership to www-data:www-data.
    print(f"  [OK] Created directory: {site_path}")
    return 0, site_path


def write_placeholder_index(name: str, domain: str) -> Path | None:
    """
    Best-effort: drop a placeholder index.html so the site isn't a bare 404.
    Not fatal if this fails — the site still works, it'll just 404 until
    real content is added.
    Returns the path on success, None on failure.
    """
    index_path = WEB_ROOT_BASE / name / "index.html"
    try:
        index_path.write_text(PLACEHOLDER_INDEX.format(domain=domain))
        print(f"  [OK] Created index: {index_path}")
        return index_path
    except OSError as e:
        print(f"  [WARN] Could not write index: {index_path}: {e}")
        return None


def write_nginx_config(name: str, domain: str) -> tuple[int, Path | None]:
    """
    Write /etc/nginx/sites-available/<name> from the template.
    Returns (exit_code, path_or_None).
    """
    config_path = NGINX_SITES_AVAILABLE / name
    web_root = WEB_ROOT_BASE / name

    if config_path.exists():
        print(f"  [FAIL] Create Nginx config: {config_path} already exists")
        return 1, None

    config_text = NGINX_TEMPLATE.format(domain=domain, web_root=web_root)
    try:
        config_path.write_text(config_text)
    except PermissionError:
        print(f"  [FAIL] Create Nginx config: permission denied writing {config_path} (try sudo)")
        return 1, None
    except OSError as e:
        print(f"  [FAIL] Create Nginx config: {config_path}: {e}")
        return 1, None

    print(f"  [OK] Created Nginx config: {config_path}")
    return 0, config_path


def enable_site(name: str, config_path: Path) -> tuple[int, Path | None]:
    """
    Symlink /etc/nginx/sites-enabled/<name> -> config_path.
    Returns (exit_code, symlink_path_or_None). Does NOT roll back
    config_path on failure — the caller decides what to do with that.
    """
    symlink_path = NGINX_SITES_ENABLED / name

    if symlink_path.exists():
        print(f"  [FAIL] Enable site: {symlink_path} already exists")
        return 1, None

    try:
        symlink_path.symlink_to(config_path)
    except OSError as e:
        print(f"  [FAIL] Enable site: {symlink_path}: {e}")
        return 1, None

    print(f"  [OK] Enabled site: {symlink_path} -> {config_path}")
    return 0, symlink_path


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


def validate_and_reload(config_path: Path, symlink_path: Path) -> int:
    """
    Section: "Validating Nginx configuration:" — run `nginx -t`, and if it
    passes, reload nginx. Rolls back (removes config_path and symlink_path)
    if the test fails, so a bad config never reaches the running nginx.
    """
    print("Validating Nginx configuration:")

    ok, output = test_nginx_config()
    if not ok:
        print("  [FAIL] nginx -t")
        for line in output.strip().splitlines():
            print(f"         {line}")
        symlink_path.unlink(missing_ok=True)
        config_path.unlink(missing_ok=True)
        print()
        print(f"  Rolled back: removed {symlink_path} and {config_path}")
        print("  nginx was NOT reloaded. No other site was affected.")
        return 1
    print("  [PASS] nginx -t")

    ok, output = reload_nginx()
    if not ok:
        print("  [FAIL] Reload nginx")
        for line in output.strip().splitlines():
            print(f"         {line}")
        print()
        print(
            "  Configuration is valid and in place, but nginx wasn't reloaded. "
            "Run 'sudo systemctl reload nginx' manually."
        )
        return 1
    print("  [OK] Reloaded nginx")

    return 0


def prompt_yes_no(question: str) -> bool:
    answer = input(f"{question} [y/N]: ").strip().lower()
    return answer == "y"


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


def verify_and_maybe_restart(domain: str) -> int:
    """
    Section: "Verifying site:" — check the site is actually reachable, and
    if not, explain why and offer to restart nginx. Never rolls back — the
    config is valid and enabled either way, this only affects whether the
    running nginx process has picked it up yet.
    """
    print("Verifying site:")

    problems = verify_site_serving(domain)
    if not problems:
        print(f"  [PASS] {domain} is being served correctly")
        return 0

    print(f"  [FAIL] {domain} is not being served correctly")
    for p in problems:
        print(f"         {p}")
    print()
    print(
        "  This usually happens when 'reload' doesn't rebind a newly added "
        "listen socket. A full restart fixes it, but briefly disconnects "
        "every site on this server, not just this one."
    )

    if not prompt_yes_no("Restart nginx now to fix this?"):
        print(
            "  Skipped restart. The config is valid and enabled, but the site "
            "may not be reachable until you run: sudo systemctl restart nginx"
        )
        return 0

    ok, output = restart_nginx()
    if not ok:
        print("  [FAIL] Restart nginx")
        for line in output.strip().splitlines():
            print(f"         {line}")
        return 1
    print("  [OK] Restarted nginx")

    problems = verify_site_serving(domain)
    if problems:
        print(f"  [FAIL] {domain} still isn't being served correctly after restart")
        for p in problems:
            print(f"         {p}")
        return 1
    print(f"  [PASS] {domain} is being served correctly")
    return 0


# =====================================================================
# Orchestration
# =====================================================================

def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    name = args.name
    domain = args.domain or args.name

    print(f"Site:   {name}")
    print(f"Domain: {domain}")
    print()

    checks = run_preflight_checks(name, domain)
    if not print_preflight_results(checks):
        return 1
    print()

    created: list[Path] = []

    print("Creating site:")
    rc, web_root_path = create_web_root(name)
    if rc != 0:
        return rc
    created.append(web_root_path)

    index_path = write_placeholder_index(name, domain)
    if index_path is not None:
        created.append(index_path)

    rc, config_path = write_nginx_config(name, domain)
    if rc != 0:
        return rc
    created.append(config_path)

    rc, symlink_path = enable_site(name, config_path)
    if rc != 0:
        # config_path was written but never enabled — clean it up too,
        # since nothing was ever tested or reloaded with it in place.
        config_path.unlink(missing_ok=True)
        return rc
    created.append(symlink_path)
    print()

    rc = validate_and_reload(config_path, symlink_path)
    if rc != 0:
        return rc
    print()

    verify_and_maybe_restart(domain)
    print()

    print("Site created successfully.")
    print()
    print(f"Site:   {name}")
    print(f"Domain: {domain}")
    print()
    print("Created:")
    for path in created:
        print(f"  {path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
