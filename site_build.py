#!/usr/bin/env python3
"""
Parish Site Builder — Ubuntu Server only
v0.5 — adds optional SSL via Let's Encrypt (certbot), on top of v0.4's
subcommand CLI (create, list, status, enable, disable, remove, update)
and SSH-based git deployment.

Directory layout per site:
    /srv/www/<name>/                    site base
    /srv/www/<name>/.parish-site.json   metadata (domain, linked repo/branch)
    /srv/www/<name>/repo/               git working tree (if git-linked)
    /srv/www/<name>/repo/public/        nginx root (if git-linked)
    /srv/www/<name>/index.html          nginx root content (if NOT git-linked)

Keeping the git checkout in repo/ and only serving repo/public/ means
.git/ (and anything else in the repo not meant to be public) is never
inside nginx's web root — important since nginx doesn't block dotfiles
by default.

Git auth is SSH, using root's SSH key (the tool is always run with sudo,
so root is who actually runs git). Add the deploy key to
/root/.ssh/ and register its public half as a read-only Deploy key on the
GitHub repo. To avoid hanging on the first connection's host-key prompt,
git commands run with GIT_SSH_COMMAND="ssh -o StrictHostKeyChecking=accept-new".

SSL uses `certbot certonly --webroot` — never the --nginx plugin, since
that would let certbot edit nginx configs directly, conflicting with this
tool owning and regenerating them from its own template. We write the SSL
server block ourselves, pointing at the standard
/etc/letsencrypt/live/<domain>/ cert paths. Renewal is left to certbot's
own systemd timer, not reimplemented here.

Usage:
    site_build.py create <name> [--domain DOMAIN] [--git URL] [--branch BRANCH]
                                 [--ssl --email EMAIL]
    site_build.py list
    site_build.py status [name]
    site_build.py enable <name>
    site_build.py disable <name>
    site_build.py remove <name> [--keep-files] [--yes]
    site_build.py update <name>
    site_build.py enable-ssl <name> --email EMAIL

Examples:
    site_build.py create messiah --domain messiah.example.org
        -> preflight checks, then creates /srv/www/messiah with a
           placeholder index.html, nginx config, enables + tests +
           reloads nginx, verifies it's reachable

    site_build.py create messiah --domain messiah.example.org \\
        --git git@github.com:parish/messiah-site.git --branch main
        -> same, but clones the repo into /srv/www/messiah/repo and
           points nginx at /srv/www/messiah/repo/public

    site_build.py create messiah --domain messiah.example.org \\
        --ssl --email admin@example.org
        -> same, plus issues a Let's Encrypt cert and switches the site
           to HTTPS (with HTTP redirecting to HTTPS). If issuance fails,
           the ENTIRE site is rolled back — same as any other create
           failure. Requires a real, publicly resolvable domain.

    site_build.py enable-ssl messiah --email admin@example.org
        -> adds SSL to a site that already exists. On failure, only the
           SSL-specific changes roll back — the site keeps serving HTTP.

    site_build.py update messiah
        -> git fetch origin && git reset --hard origin/main && git clean -fd
           in /srv/www/messiah/repo (static files update immediately,
           no nginx reload needed)

    site_build.py list      -> table of every site: name, domain, enabled, git
    site_build.py status              -> table of every site with live HTTP + git + cert status
    site_build.py status messiah      -> detailed status for just this one site
    site_build.py disable messiah     -> removes the nginx symlink, reloads
    site_build.py enable messiah      -> re-adds it, tests, reloads
    site_build.py remove messiah      -> removes config + symlink + site dir
                                          (asks for confirmation first)
"""

import argparse
import http.client
import json
import os
import re
import shutil
import socket
import ssl
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

WEB_ROOT_BASE = Path("/srv/www")
NGINX_SITES_AVAILABLE = Path("/etc/nginx/sites-available")
NGINX_SITES_ENABLED = Path("/etc/nginx/sites-enabled")
LETSENCRYPT_LIVE_DIR = Path("/etc/letsencrypt/live")

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

# Loose check: just enough to catch typos, not a full URL grammar.
GIT_URL_PATTERN = re.compile(r"^(https?://|git@|ssh://)\S+$")

# Loose check: just enough to catch typos, not full RFC 5322 validation.
EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

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

# Used once SSL is enabled. Port 80 redirects to HTTPS; the real site is
# served only on 443. Written by write_ssl_nginx_config, replacing the
# plain NGINX_TEMPLATE config in place.
NGINX_SSL_TEMPLATE = """server {{
    listen 80;
    listen [::]:80;
    server_name {domain};
    return 301 https://$host$request_uri;
}}

server {{
    listen 443 ssl;
    listen [::]:443 ssl;
    server_name {domain};

    ssl_certificate {fullchain};
    ssl_certificate_key {privkey};

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

METADATA_FILENAME = ".parish-site.json"


# =====================================================================
# Site metadata model
# =====================================================================

@dataclass(frozen=True)
class SiteMetadata:
    name: str
    domain: str
    git_repo: str | None = None
    git_branch: str | None = None

    @property
    def site_dir(self) -> Path:
        return WEB_ROOT_BASE / self.name

    @property
    def repo_dir(self) -> Path:
        return self.site_dir / "repo"

    @property
    def public_dir(self) -> Path:
        return self.repo_dir / "public"

    @property
    def metadata_path(self) -> Path:
        return self.site_dir / METADATA_FILENAME

    @property
    def nginx_root(self) -> Path:
        """Where nginx should actually serve from."""
        return self.public_dir if self.git_repo else self.site_dir

    def to_dict(self) -> dict:
        d = {"name": self.name, "domain": self.domain}
        if self.git_repo:
            d["git"] = {"repo": self.git_repo, "branch": self.git_branch or "main"}
        return d

    @staticmethod
    def from_dict(d: dict) -> "SiteMetadata":
        git = d.get("git")
        if git:
            return SiteMetadata(
                name=d["name"], domain=d["domain"],
                git_repo=git.get("repo"), git_branch=git.get("branch") or "main",
            )
        return SiteMetadata(name=d["name"], domain=d["domain"])


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="site_build.py",
        description="Parish Site Builder — create and manage parish website directories and nginx configs.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    create_p = subparsers.add_parser("create", help="Create a new site")
    create_p.add_argument("name", help="Site name, used as the directory and nginx config filename.")
    create_p.add_argument("--domain", help="Domain for the nginx server_name. Defaults to the site name.")
    create_p.add_argument("--git", dest="git_repo", help="Git repo SSH URL, e.g. git@github.com:ORG/REPO.git")
    create_p.add_argument("--branch", dest="git_branch", default="main", help="Git branch to deploy (default: main)")
    create_p.add_argument("--ssl", action="store_true", help="Issue a Let's Encrypt certificate after creating the site.")
    create_p.add_argument("--email", help="Contact email for Let's Encrypt. Required if --ssl is given.")

    subparsers.add_parser("list", help="List all sites")

    status_p = subparsers.add_parser("status", help="Show status for one site, or all sites if omitted")
    status_p.add_argument("name", nargs="?", help="Site name. Omit to show every site.")

    enable_p = subparsers.add_parser("enable", help="Enable a disabled site")
    enable_p.add_argument("name")

    disable_p = subparsers.add_parser("disable", help="Disable a site without deleting it")
    disable_p.add_argument("name")

    remove_p = subparsers.add_parser("remove", help="Remove a site entirely")
    remove_p.add_argument("name")
    remove_p.add_argument(
        "--keep-files", action="store_true",
        help="Keep the site directory on disk; only remove the nginx config.",
    )
    remove_p.add_argument("--yes", action="store_true", help="Skip the confirmation prompt.")

    update_p = subparsers.add_parser("update", help="Pull the latest version from the linked git repo")
    update_p.add_argument("name")

    enable_ssl_p = subparsers.add_parser("enable-ssl", help="Issue a Let's Encrypt certificate for an existing site")
    enable_ssl_p.add_argument("name")
    enable_ssl_p.add_argument("--email", required=True, help="Contact email for Let's Encrypt.")

    subparsers.add_parser(
        "doctor",
        help="Check that this machine is ready to host sites (nginx, certbot, git, ports, firewall)",
    )

    return parser


# =====================================================================
# Validation (pure functions, no I/O side effects beyond reading state)
# =====================================================================

def is_valid_site_name(name: str) -> bool:
    return bool(SITE_NAME_PATTERN.match(name))


def is_valid_domain(domain: str) -> bool:
    return bool(DOMAIN_PATTERN.match(domain)) and len(domain) <= 253


def is_valid_git_url(url: str) -> bool:
    return bool(GIT_URL_PATTERN.match(url))


def is_valid_email(email: str) -> bool:
    return bool(EMAIL_PATTERN.match(email))


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


def extract_domain(config_path: Path) -> str | None:
    """Pull the first server_name out of an nginx config file (fallback for
    sites that predate the metadata file)."""
    try:
        text = config_path.read_text()
    except OSError:
        return None
    match = re.search(r"server_name\s+([^;]+);", text)
    return match.group(1).split()[0] if match else None


def redact_url(url: str) -> str:
    """
    Mask embedded credentials before printing a URL. SSH URLs
    (git@host:org/repo.git) never carry secrets in the URL itself — the key
    is a file on disk — but this is kept as defense in depth in case an
    https://user:pass@... URL is ever used instead.
    """
    return re.sub(r"://[^@/\s]+@", "://***@", url)


def run_preflight_checks(meta: SiteMetadata) -> list[dict]:
    """
    Run every check before anything is created or changed.
    Returns a list of dicts: {"label": str, "passed": bool, "detail": str}.
    """
    checks = []

    checks.append({
        "label": "Valid site name",
        "passed": is_valid_site_name(meta.name),
        "detail": "" if is_valid_site_name(meta.name) else (
            f"'{meta.name}' must start with a letter/number and contain only letters, "
            "numbers, hyphens, and underscores (max 63 characters)."
        ),
    })

    checks.append({
        "label": "Valid domain",
        "passed": is_valid_domain(meta.domain),
        "detail": "" if is_valid_domain(meta.domain) else (
            f"'{meta.domain}' doesn't look like a valid domain/hostname."
        ),
    })

    if meta.git_repo:
        valid_git = is_valid_git_url(meta.git_repo)
        checks.append({
            "label": "Valid git URL",
            "passed": valid_git,
            "detail": "" if valid_git else (
                f"'{redact_url(meta.git_repo)}' doesn't look like a git URL "
                "(expected it to start with https://, git@, or ssh://)."
            ),
        })
        git_path = shutil.which("git")
        checks.append({
            "label": "git installed",
            "passed": git_path is not None,
            "detail": git_path if git_path else "git is not installed on this system.",
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
        checks.append({"label": "nginx running", "passed": False, "detail": "nginx is not installed."})

    web_exists = meta.site_dir.exists()
    checks.append({
        "label": "Site directory available",
        "passed": not web_exists,
        "detail": "" if not web_exists else f"{meta.site_dir} already exists.",
    })

    config_path = NGINX_SITES_AVAILABLE / meta.name
    symlink_path = NGINX_SITES_ENABLED / meta.name
    config_exists = config_path.exists() or symlink_path.exists()
    checks.append({
        "label": "Nginx configuration available",
        "passed": not config_exists,
        "detail": "" if not config_exists else f"An nginx config for '{meta.name}' already exists.",
    })

    conflict = find_domain_conflict(meta.domain)
    checks.append({
        "label": "Domain not already in use",
        "passed": conflict is None,
        "detail": "" if conflict is None else (
            f"Domain '{meta.domain}' is already used by site config '{conflict}'."
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


def is_ssl_enabled(name: str) -> bool:
    """Whether a site's nginx config already has an SSL server block."""
    config_path = NGINX_SITES_AVAILABLE / name
    if not config_path.exists():
        return False
    try:
        return "ssl_certificate " in config_path.read_text()
    except OSError:
        return False


def run_ssl_checks(domain: str, email: str, *, require_no_existing_cert: bool = True) -> list[dict]:
    """
    Checks specific to issuing a certificate, shared by `create --ssl` and
    `enable-ssl`. These can all be evaluated before touching anything —
    whether the actual issuance succeeds (DNS propagation, rate limits,
    etc.) can only be known once certbot is actually run.
    """
    checks = []

    certbot_path = shutil.which("certbot")
    checks.append({
        "label": "certbot installed",
        "passed": certbot_path is not None,
        "detail": certbot_path if certbot_path else "certbot is not installed on this system.",
    })

    valid_email = is_valid_email(email)
    checks.append({
        "label": "Valid email",
        "passed": valid_email,
        "detail": "" if valid_email else f"'{email}' doesn't look like a valid email address.",
    })

    has_dot = "." in domain
    checks.append({
        "label": "Domain looks like a real hostname",
        "passed": has_dot,
        "detail": "" if has_dot else (
            f"'{domain}' has no dot — Let's Encrypt can't issue certificates for bare "
            "local names. Use a real domain (or a service like sslip.io for testing)."
        ),
    })

    if require_no_existing_cert:
        cert_dir = LETSENCRYPT_LIVE_DIR / domain
        exists = cert_dir.exists()
        checks.append({
            "label": "No existing certificate for domain",
            "passed": not exists,
            "detail": "" if not exists else f"{cert_dir} already exists.",
        })

    return checks


def run_enable_ssl_site_checks(name: str) -> list[dict]:
    """Checks specific to `enable-ssl`: the site must already exist,
    be enabled, and not already have SSL configured."""
    checks = []

    config_path = NGINX_SITES_AVAILABLE / name
    exists = config_path.exists()
    checks.append({
        "label": "Site exists",
        "passed": exists,
        "detail": "" if exists else f"No such site: {name}",
    })

    if exists:
        enabled = (NGINX_SITES_ENABLED / name).exists()
        checks.append({
            "label": "Site is enabled",
            "passed": enabled,
            "detail": "" if enabled else (
                f"'{name}' is disabled. Certbot needs it serving HTTP to complete the "
                f"challenge — enable it first with: site_build.py enable {name}"
            ),
        })

        already_ssl = is_ssl_enabled(name)
        checks.append({
            "label": "Site is not already SSL-enabled",
            "passed": not already_ssl,
            "detail": "" if not already_ssl else f"'{name}' already has SSL configured.",
        })

    return checks


# =====================================================================
# Git helpers (SSH, using root's key)
# =====================================================================

def git_env_for_ssh() -> dict:
    """
    Make git-over-SSH non-interactive on first connection by accepting new
    host keys automatically, instead of hanging on:
      "Are you sure you want to continue connecting (yes/no)?"
    """
    env = dict(os.environ)
    env.setdefault("GIT_SSH_COMMAND", "ssh -o StrictHostKeyChecking=accept-new")
    return env


def run_git(args: list[str], cwd: Path | None = None) -> tuple[bool, str]:
    try:
        result = subprocess.run(
            ["git"] + args,
            cwd=str(cwd) if cwd else None,
            env=git_env_for_ssh(),
            capture_output=True, text=True, check=False,
        )
        output = (result.stdout or "") + (result.stderr or "")
        return result.returncode == 0, output
    except FileNotFoundError:
        return False, "git command not found"


def clone_repo(meta: SiteMetadata) -> tuple[bool, str]:
    """Clone meta.git_repo (at meta.git_branch) into meta.repo_dir."""
    branch = meta.git_branch or "main"
    return run_git(["clone", "--branch", branch, meta.git_repo, str(meta.repo_dir)])


def write_metadata(meta: SiteMetadata) -> tuple[int, Path | None]:
    try:
        meta.metadata_path.write_text(json.dumps(meta.to_dict(), indent=2, sort_keys=True) + "\n")
    except PermissionError:
        print(f"  [FAIL] Write metadata: permission denied writing {meta.metadata_path} (try sudo)")
        return 1, None
    except OSError as e:
        print(f"  [FAIL] Write metadata: {meta.metadata_path}: {e}")
        return 1, None
    print(f"  [OK] Wrote metadata: {meta.metadata_path}")
    return 0, meta.metadata_path


def try_read_metadata(name: str) -> SiteMetadata | None:
    """Silent variant for list/status scans — returns None on any problem."""
    path = WEB_ROOT_BASE / name / METADATA_FILENAME
    try:
        return SiteMetadata.from_dict(json.loads(path.read_text()))
    except (OSError, json.JSONDecodeError, KeyError):
        return None


def read_metadata_for_site(name: str) -> tuple[int, SiteMetadata | None]:
    """Noisy variant for commands (like update) where a missing/bad
    metadata file is itself the thing that should fail the operation."""
    path = WEB_ROOT_BASE / name / METADATA_FILENAME
    if not path.exists():
        print(f"[FAIL] No metadata found at {path} (site not created with this version?)")
        return 1, None
    try:
        return 0, SiteMetadata.from_dict(json.loads(path.read_text()))
    except OSError as e:
        print(f"[FAIL] Read metadata: {path}: {e}")
        return 1, None
    except (json.JSONDecodeError, KeyError) as e:
        print(f"[FAIL] Read metadata: {path}: invalid content ({e})")
        return 1, None


# =====================================================================
# Site creation building blocks
# =====================================================================

def create_web_root(name: str) -> tuple[int, Path | None]:
    """Create /srv/www/<name>. Returns (exit_code, path_or_None)."""
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

    # Left root-owned deliberately, not a TODO: default umask (022) makes
    # this world-readable (644)/traversable (755), which is all nginx's
    # www-data worker needs to serve it — and the content is public web
    # content anyway. Only revisit this if a future feature needs
    # www-data to *write* into the site directory (uploads, caching, etc).
    print(f"  [OK] Created directory: {site_path}")
    return 0, site_path


def write_placeholder_index(meta: SiteMetadata) -> Path | None:
    """Best-effort: drop a placeholder index.html for non-git sites so
    they're not a bare 404. Not fatal if this fails."""
    index_path = meta.site_dir / "index.html"
    try:
        index_path.write_text(PLACEHOLDER_INDEX.format(domain=meta.domain))
        print(f"  [OK] Created index: {index_path}")
        return index_path
    except OSError as e:
        print(f"  [WARN] Could not write index: {index_path}: {e}")
        return None


def write_nginx_config(meta: SiteMetadata) -> tuple[int, Path | None]:
    """Write /etc/nginx/sites-available/<name>, rooted at meta.nginx_root."""
    config_path = NGINX_SITES_AVAILABLE / meta.name

    if config_path.exists():
        print(f"  [FAIL] Create Nginx config: {config_path} already exists")
        return 1, None

    config_text = NGINX_TEMPLATE.format(domain=meta.domain, web_root=meta.nginx_root)
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
    """Symlink /etc/nginx/sites-enabled/<name> -> config_path."""
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
    try:
        result = subprocess.run(["nginx", "-t"], capture_output=True, text=True, check=False)
        output = (result.stdout or "") + (result.stderr or "")
        return result.returncode == 0, output
    except FileNotFoundError:
        return False, "nginx command not found"


def reload_nginx() -> tuple[bool, str]:
    try:
        result = subprocess.run(
            ["systemctl", "reload", "nginx"], capture_output=True, text=True, check=False
        )
        output = (result.stdout or "") + (result.stderr or "")
        return result.returncode == 0, output
    except FileNotFoundError:
        return False, "systemctl command not found"


def restart_nginx() -> tuple[bool, str]:
    try:
        result = subprocess.run(
            ["systemctl", "restart", "nginx"], capture_output=True, text=True, check=False
        )
        output = (result.stdout or "") + (result.stderr or "")
        return result.returncode == 0, output
    except FileNotFoundError:
        return False, "systemctl command not found"


def validate_and_reload(config_path: Path, symlink_path: Path) -> int:
    """Run `nginx -t`; reload if it passes; roll back config+symlink if not."""
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
    """Check the site is reachable; offer to restart nginx if not."""
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
# SSL / certbot
# =====================================================================

def issue_certificate(domain: str, webroot: Path, email: str) -> tuple[bool, str]:
    """
    Run `certbot certonly --webroot`. Deliberately NOT the --nginx plugin:
    that would let certbot edit our nginx config directly, which conflicts
    with this tool owning and regenerating configs from its own template.
    certonly only obtains the cert files; we write the SSL server block
    ourselves.
    """
    cmd = [
        "certbot", "certonly", "--webroot",
        "-w", str(webroot), "-d", domain,
        "--non-interactive", "--agree-tos",
        "-m", email, "--no-eff-email",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=False)
        output = (result.stdout or "") + (result.stderr or "")
        return result.returncode == 0, output
    except FileNotFoundError:
        return False, "certbot command not found"


def verify_https_serving(domain: str) -> list[str]:
    """
    Make a real HTTPS request to the domain (not loopback — SSL requires
    real, publicly resolvable DNS to have gotten this far at all) and
    confirm the TLS handshake succeeds and nginx responds. A successful
    handshake is itself meaningful: it proves the cert matches the domain
    and chains to a trusted CA, not just that a file exists on disk.
    """
    problems = []
    try:
        context = ssl.create_default_context()
        conn = http.client.HTTPSConnection(domain, 443, timeout=5, context=context)
        conn.request("GET", "/", headers={"Host": domain})
        resp = conn.getresponse()
        resp.read()
        conn.close()
        if resp.status >= 500:
            problems.append(f"HTTPS request returned status {resp.status}")
    except ssl.SSLCertVerificationError as e:
        problems.append(f"Certificate verification failed: {e}")
    except OSError as e:
        problems.append(f"Could not connect over HTTPS: {e}")
    return problems


def parse_cert_expiry(openssl_enddate_output: str) -> datetime | None:
    """Parse `openssl x509 -enddate` output, e.g. 'notAfter=Jan  1 00:00:00 2027 GMT'."""
    if "=" not in openssl_enddate_output:
        return None
    date_part = openssl_enddate_output.split("=", 1)[1].strip()
    date_part = re.sub(r"\s+GMT$", "", date_part)
    date_part = re.sub(r"\s+", " ", date_part)
    try:
        return datetime.strptime(date_part, "%b %d %H:%M:%S %Y").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def cert_expiry_days(domain: str) -> int | None:
    """
    Days until the cert expires, reading the actual cert file rather than
    trusting certbot's own bookkeeping — consistent with how this tool
    verifies HTTP reachability by actually probing it, not just trusting
    nginx's reload exit code. Returns None if no cert is found.
    """
    cert_path = LETSENCRYPT_LIVE_DIR / domain / "fullchain.pem"
    if not cert_path.exists():
        return None
    try:
        result = subprocess.run(
            ["openssl", "x509", "-enddate", "-noout", "-in", str(cert_path)],
            capture_output=True, text=True, check=False,
        )
    except FileNotFoundError:
        return None
    if result.returncode != 0:
        return None
    expiry = parse_cert_expiry(result.stdout.strip())
    if expiry is None:
        return None
    return (expiry - datetime.now(timezone.utc)).days


def format_cert_status(domain: str) -> str:
    """Short form for table display, e.g. '71d', 'EXPIRED 3d ago', or '-'."""
    days = cert_expiry_days(domain)
    if days is None:
        return "-"
    if days < 0:
        return f"EXPIRED {abs(days)}d ago"
    return f"{days}d"


def enable_ssl_for_site(meta: SiteMetadata, email: str) -> int:
    """
    Issue a certificate and rewrite the site's nginx config for SSL.
    Rolls back the config (not the whole site) on failure after the
    config has been touched — the site keeps serving over HTTP. Whether
    the *whole site* should be rolled back (the `create --ssl` case) is
    the caller's decision, not this function's.
    """
    print("Enabling SSL:")

    ok, output = issue_certificate(meta.domain, meta.nginx_root, email)
    if not ok:
        print("  [FAIL] Obtain certificate (certbot certonly --webroot)")
        for line in output.strip().splitlines():
            print(f"         {line}")
        return 1
    print(f"  [OK] Obtained certificate for {meta.domain}")

    config_path = NGINX_SITES_AVAILABLE / meta.name
    try:
        old_text = config_path.read_text()
    except OSError as e:
        print(f"  [FAIL] Read existing config: {config_path}: {e}")
        return 1

    cert_dir = LETSENCRYPT_LIVE_DIR / meta.domain
    new_text = NGINX_SSL_TEMPLATE.format(
        domain=meta.domain, web_root=meta.nginx_root,
        fullchain=cert_dir / "fullchain.pem", privkey=cert_dir / "privkey.pem",
    )
    try:
        config_path.write_text(new_text)
    except OSError as e:
        print(f"  [FAIL] Write SSL config: {config_path}: {e}")
        return 1
    print(f"  [OK] Updated Nginx config for SSL: {config_path}")

    ok, output = test_nginx_config()
    if not ok:
        print("  [FAIL] nginx -t")
        for line in output.strip().splitlines():
            print(f"         {line}")
        config_path.write_text(old_text)
        print()
        print(f"  Rolled back: restored {config_path} to its pre-SSL version.")
        print("  nginx was NOT reloaded. The site is still serving over HTTP only.")
        return 1
    print("  [PASS] nginx -t")

    ok, output = reload_nginx()
    if not ok:
        print("  [FAIL] Reload nginx")
        for line in output.strip().splitlines():
            print(f"         {line}")
        print()
        print(
            "  The SSL config is valid and in place, but nginx wasn't reloaded. "
            "Run 'sudo systemctl reload nginx' manually."
        )
        return 1
    print("  [OK] Reloaded nginx")

    problems = verify_https_serving(meta.domain)
    if problems:
        print(f"  [FAIL] {meta.domain} is not being served correctly over HTTPS")
        for p in problems:
            print(f"         {p}")
        return 1
    print(f"  [PASS] {meta.domain} is being served correctly over HTTPS")

    days = cert_expiry_days(meta.domain)
    if days is not None:
        print(f"  Certificate valid for {days} days")

    return 0


# =====================================================================
# doctor — whole-machine readiness check (informative, nothing blocks)
# =====================================================================

def nginx_enabled_on_boot() -> bool:
    try:
        result = subprocess.run(
            ["systemctl", "is-enabled", "nginx"], capture_output=True, text=True, check=False
        )
        return result.returncode == 0
    except FileNotFoundError:
        return False


def certbot_timer_active() -> bool:
    try:
        result = subprocess.run(
            ["systemctl", "is-active", "certbot.timer"], capture_output=True, text=True, check=False
        )
        return result.returncode == 0
    except FileNotFoundError:
        return False


def can_connect_localhost(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=2):
            return True
    except OSError:
        return False


def ufw_status_output() -> str:
    try:
        result = subprocess.run(["ufw", "status"], capture_output=True, text=True, check=False)
        return (result.stdout or "") + (result.stderr or "")
    except FileNotFoundError:
        return ""


def ufw_allows_port(output: str, port: int) -> bool:
    for line in output.splitlines():
        if str(port) in line and "ALLOW" in line.upper():
            return True
    return False


def run_doctor_checks() -> list[dict]:
    """
    Whole-machine readiness, not tied to any one site. Unlike preflight
    checks, nothing here blocks anything — it's purely informative, run
    any time (especially useful right after a fresh VPS setup, before
    ever running `create`). Each check has a "status" of PASS, WARN, or
    FAIL rather than a plain pass/fail: a missing optional tool (git,
    certbot) is a WARN, not a FAIL, since not every site needs them.
    """
    checks = []

    is_root = hasattr(os, "geteuid") and os.geteuid() == 0
    checks.append({
        "label": "Running as root",
        "status": "PASS" if is_root else "FAIL",
        "detail": "" if is_root else "Most commands need root — run with sudo.",
    })

    nginx_path = shutil.which("nginx")
    checks.append({
        "label": "nginx installed",
        "status": "PASS" if nginx_path else "FAIL",
        "detail": nginx_path or "Install with: sudo apt install nginx",
    })

    running = False
    if nginx_path:
        running = nginx_is_running()
        checks.append({
            "label": "nginx running",
            "status": "PASS" if running else "FAIL",
            "detail": "" if running else "Start with: sudo systemctl start nginx",
        })
        enabled = nginx_enabled_on_boot()
        checks.append({
            "label": "nginx enabled on boot",
            "status": "PASS" if enabled else "WARN",
            "detail": "" if enabled else "Enable with: sudo systemctl enable nginx",
        })
    else:
        checks.append({"label": "nginx running", "status": "FAIL", "detail": "nginx is not installed."})
        checks.append({"label": "nginx enabled on boot", "status": "FAIL", "detail": "nginx is not installed."})

    git_path = shutil.which("git")
    checks.append({
        "label": "git installed",
        "status": "PASS" if git_path else "WARN",
        "detail": git_path or "Only needed for --git / update. Install with: sudo apt install git",
    })

    openssl_path = shutil.which("openssl")
    checks.append({
        "label": "openssl installed",
        "status": "PASS" if openssl_path else "WARN",
        "detail": openssl_path or "Needed for cert expiry in `status`. Install with: sudo apt install openssl",
    })

    certbot_path = shutil.which("certbot")
    checks.append({
        "label": "certbot installed",
        "status": "PASS" if certbot_path else "WARN",
        "detail": certbot_path or "Only needed for --ssl / enable-ssl. Install with: sudo apt install certbot",
    })
    if certbot_path:
        timer_active = certbot_timer_active()
        checks.append({
            "label": "certbot renewal timer active",
            "status": "PASS" if timer_active else "WARN",
            "detail": "" if timer_active else (
                "Certs won't auto-renew. Check: sudo systemctl list-timers | grep certbot"
            ),
        })

    web_root_exists = WEB_ROOT_BASE.exists()
    checks.append({
        "label": f"{WEB_ROOT_BASE} exists",
        "status": "PASS" if web_root_exists else "FAIL",
        "detail": "" if web_root_exists else f"Create it with: sudo mkdir -p {WEB_ROOT_BASE}",
    })

    sites_available_exists = NGINX_SITES_AVAILABLE.exists()
    checks.append({
        "label": f"{NGINX_SITES_AVAILABLE} exists",
        "status": "PASS" if sites_available_exists else "FAIL",
        "detail": "" if sites_available_exists else "nginx doesn't look properly installed.",
    })

    sites_enabled_exists = NGINX_SITES_ENABLED.exists()
    checks.append({
        "label": f"{NGINX_SITES_ENABLED} exists",
        "status": "PASS" if sites_enabled_exists else "FAIL",
        "detail": "" if sites_enabled_exists else "nginx doesn't look properly installed.",
    })

    if nginx_path and running:
        for port in (80, 443):
            reachable = can_connect_localhost(port)
            checks.append({
                "label": f"nginx listening on port {port} (local check)",
                "status": "PASS" if reachable else "WARN",
                "detail": "" if reachable else (
                    f"Could not connect to 127.0.0.1:{port}. This only confirms nginx is "
                    "bound locally — it doesn't confirm the port is reachable from the internet."
                ),
            })

    ufw_path = shutil.which("ufw")
    if ufw_path:
        output = ufw_status_output()
        for port in (80, 443):
            allowed = ufw_allows_port(output, port)
            checks.append({
                "label": f"ufw allows port {port}",
                "status": "PASS" if allowed else "WARN",
                "detail": "" if allowed else f"Run: sudo ufw allow {port}/tcp",
            })
    # If ufw isn't installed, skip silently — some VPS providers firewall
    # at the network/security-group level instead of locally.

    return checks


def print_doctor_results(checks: list[dict]) -> bool:
    print("System check:")
    has_fail = False
    for check in checks:
        line = f"  [{check['status']}] {check['label']}"
        if check["detail"]:
            line += f": {check['detail']}"
        print(line)
        if check["status"] == "FAIL":
            has_fail = True

    print()
    if has_fail:
        print("Some checks failed — fix these before creating sites.")
    else:
        print("System looks ready. (WARNs are advisory, not blocking.)")

    return not has_fail


def cmd_doctor(args) -> int:
    checks = run_doctor_checks()
    ok = print_doctor_results(checks)
    return 0 if ok else 1


# =====================================================================
# Subcommand handlers
# =====================================================================

def cmd_create(args) -> int:
    name = args.name
    domain = args.domain or args.name
    want_ssl = getattr(args, "ssl", False)
    email = getattr(args, "email", None) or ""
    meta = SiteMetadata(
        name=name, domain=domain,
        git_repo=args.git_repo,
        git_branch=args.git_branch if args.git_repo else None,
    )

    print(f"Site:   {meta.name}")
    print(f"Domain: {meta.domain}")
    if meta.git_repo:
        print(f"Git:    {redact_url(meta.git_repo)} ({meta.git_branch})")
    if want_ssl:
        print(f"SSL:    yes ({email or 'no email given'})")
    print()

    checks = run_preflight_checks(meta)
    if want_ssl:
        checks += run_ssl_checks(meta.domain, email)
    if not print_preflight_results(checks):
        return 1
    print()

    created: list[Path] = []

    print("Creating site:")
    rc, site_dir = create_web_root(name)
    if rc != 0:
        return rc
    created.append(site_dir)

    rc, meta_path = write_metadata(meta)
    if rc != 0:
        shutil.rmtree(site_dir, ignore_errors=True)
        print(f"  Rolled back: removed {site_dir}")
        return rc
    created.append(meta_path)

    if meta.git_repo:
        ok, output = clone_repo(meta)
        if not ok:
            print(f"  [FAIL] Clone git repo: {redact_url(meta.git_repo)}")
            for line in output.strip().splitlines():
                print(f"         {line}")
            shutil.rmtree(site_dir, ignore_errors=True)
            print()
            print(f"  Rolled back: removed {site_dir}")
            print("  No changes were made to nginx.")
            return 1
        print(f"  [OK] Cloned git repo: {redact_url(meta.git_repo)} -> {meta.repo_dir}")
        created.append(meta.repo_dir)
    else:
        index_path = write_placeholder_index(meta)
        if index_path is not None:
            created.append(index_path)

    rc, config_path = write_nginx_config(meta)
    if rc != 0:
        shutil.rmtree(site_dir, ignore_errors=True)
        print(f"  Rolled back: removed {site_dir}")
        return rc
    created.append(config_path)

    rc, symlink_path = enable_site(name, config_path)
    if rc != 0:
        config_path.unlink(missing_ok=True)
        shutil.rmtree(site_dir, ignore_errors=True)
        print(f"  Rolled back: removed {config_path} and {site_dir}")
        return rc
    created.append(symlink_path)
    print()

    rc = validate_and_reload(config_path, symlink_path)
    if rc != 0:
        return rc
    print()

    verify_and_maybe_restart(meta.domain)
    print()

    if want_ssl:
        rc = enable_ssl_for_site(meta, email)
        if rc != 0:
            print()
            print("SSL setup failed. Per --ssl semantics, rolling back the entire site")
            print("(same as any other create failure) rather than leaving it on HTTP-only.")
            symlink_path.unlink(missing_ok=True)
            config_path.unlink(missing_ok=True)
            shutil.rmtree(site_dir, ignore_errors=True)
            reload_nginx()
            print(f"Rolled back: removed {symlink_path}, {config_path}, and {site_dir}")
            return 1
        print()

    print("Site created successfully.")
    print()
    print(f"Site:   {meta.name}")
    print(f"Domain: {meta.domain}")
    if meta.git_repo:
        print(f"Git:    {redact_url(meta.git_repo)} ({meta.git_branch})")
    if want_ssl:
        print(f"SSL:    https://{meta.domain}")
    print()
    print("Created:")
    for path in created:
        print(f"  {path}")
    if meta.git_repo:
        print()
        print(f"Update this site later with: sudo python3 site_build.py update {name}")

    return 0


def cmd_list(args) -> int:
    if not NGINX_SITES_AVAILABLE.exists():
        print("No sites found.")
        return 0

    site_names = sorted(
        p.name for p in NGINX_SITES_AVAILABLE.iterdir()
        if p.is_file() and p.name != "default"
    )
    if not site_names:
        print("No sites found.")
        return 0

    print(f"{'SITE':<20}{'DOMAIN':<30}{'ENABLED':<10}{'GIT'}")
    for name in site_names:
        meta = try_read_metadata(name)
        domain = meta.domain if meta else (extract_domain(NGINX_SITES_AVAILABLE / name) or "?")
        enabled = "yes" if (NGINX_SITES_ENABLED / name).exists() else "no"
        git_linked = "yes" if meta and meta.git_repo else "no"
        print(f"{name:<20}{domain:<30}{enabled:<10}{git_linked}")
    return 0


def cmd_status(args) -> int:
    if args.name:
        return _status_single(args.name)
    return _status_all()


def _status_single(name: str) -> int:
    config_path = NGINX_SITES_AVAILABLE / name
    if not config_path.exists():
        print(f"No such site: {name}")
        return 1

    meta = try_read_metadata(name)
    domain = meta.domain if meta else (extract_domain(config_path) or name)
    enabled = (NGINX_SITES_ENABLED / name).exists()

    print(f"Site:    {name}")
    print(f"Domain:  {domain}")
    print(f"Enabled: {'yes' if enabled else 'no'}")

    if enabled:
        problems = verify_site_serving(domain)
        print(f"HTTP:    {'OK' if not problems else 'FAIL - ' + '; '.join(problems)}")
    else:
        print("HTTP:    not checked (site is disabled)")

    if meta and meta.git_repo:
        ok, commit = run_git(["rev-parse", "--short", "HEAD"], cwd=meta.repo_dir)
        commit_display = commit.strip() if ok else "unknown"
        print(f"Git:     {redact_url(meta.git_repo)} ({meta.git_branch}) @ {commit_display}")
    else:
        print("Git:     not linked")

    days = cert_expiry_days(domain)
    if days is None:
        print("Cert:    none (HTTP only)")
    elif days < 0:
        print(f"Cert:    EXPIRED {abs(days)} days ago")
    else:
        print(f"Cert:    valid, expires in {days} days")

    return 0


def _status_all() -> int:
    if not NGINX_SITES_AVAILABLE.exists():
        print("No sites found.")
        return 0

    site_names = sorted(
        p.name for p in NGINX_SITES_AVAILABLE.iterdir()
        if p.is_file() and p.name != "default"
    )
    if not site_names:
        print("No sites found.")
        return 0

    print(f"{'SITE':<20}{'HTTP':<8}{'GIT':<10}{'CERT'}")
    for name in site_names:
        meta = try_read_metadata(name)
        domain = meta.domain if meta else (extract_domain(NGINX_SITES_AVAILABLE / name) or name)
        enabled = (NGINX_SITES_ENABLED / name).exists()
        if enabled:
            problems = verify_site_serving(domain)
            http_status = "OK" if not problems else "FAIL"
        else:
            http_status = "off"
        git_display = "linked" if meta and meta.git_repo else "-"
        cert_display = format_cert_status(domain)
        print(f"{name:<20}{http_status:<8}{git_display:<10}{cert_display}")
    return 0


def cmd_enable(args) -> int:
    name = args.name
    config_path = NGINX_SITES_AVAILABLE / name
    if not config_path.exists():
        print(f"[FAIL] No such site: {name}")
        return 1

    symlink_path = NGINX_SITES_ENABLED / name
    if symlink_path.exists():
        print(f"[OK] {name} is already enabled")
        return 0

    rc, symlink_path = enable_site(name, config_path)
    if rc != 0:
        return rc

    return validate_and_reload(config_path, symlink_path)


def cmd_disable(args) -> int:
    name = args.name
    symlink_path = NGINX_SITES_ENABLED / name

    if not symlink_path.exists():
        print(f"[OK] {name} is already disabled")
        return 0

    symlink_path.unlink()
    print(f"[OK] Disabled: removed {symlink_path}")

    ok, output = reload_nginx()
    if not ok:
        print("[FAIL] Reload nginx")
        for line in output.strip().splitlines():
            print(f"       {line}")
        return 1
    print("[OK] Reloaded nginx")
    return 0


def cmd_remove(args) -> int:
    name = args.name
    config_path = NGINX_SITES_AVAILABLE / name
    symlink_path = NGINX_SITES_ENABLED / name
    site_dir = WEB_ROOT_BASE / name

    if not config_path.exists() and not site_dir.exists():
        print(f"No such site: {name}")
        return 1

    print("This will remove:")
    if symlink_path.exists():
        print(f"  {symlink_path}")
    if config_path.exists():
        print(f"  {config_path}")
    if not args.keep_files and site_dir.exists():
        print(f"  {site_dir} (and all its contents)")

    if not args.yes:
        if not prompt_yes_no("Are you sure?"):
            print("Cancelled. No changes were made.")
            return 1

    if symlink_path.exists():
        symlink_path.unlink()
        print(f"[OK] Removed {symlink_path}")
    if config_path.exists():
        config_path.unlink()
        print(f"[OK] Removed {config_path}")
    if not args.keep_files and site_dir.exists():
        shutil.rmtree(site_dir)
        print(f"[OK] Removed {site_dir}")

    ok, output = reload_nginx()
    if ok:
        print("[OK] Reloaded nginx")
    else:
        print("[WARN] nginx reload failed — run 'sudo systemctl reload nginx' manually")
        for line in output.strip().splitlines():
            print(f"       {line}")

    return 0


def cmd_update(args) -> int:
    name = args.name

    rc, meta = read_metadata_for_site(name)
    if rc != 0 or meta is None:
        return 1

    if not meta.git_repo:
        print(f"[FAIL] '{name}' is not linked to a git repository.")
        print(f"       Recreate it with --git <repo-url> to link one.")
        return 1

    if not (meta.repo_dir / ".git").exists():
        print(f"[FAIL] {meta.repo_dir} is not a git repository (missing .git).")
        return 1

    print("Updating from git:")

    ok, output = run_git(["fetch", "origin"], cwd=meta.repo_dir)
    if not ok:
        print("  [FAIL] git fetch origin")
        for line in output.strip().splitlines():
            print(f"         {line}")
        return 1
    print("  [OK] git fetch origin")

    branch = meta.git_branch or "main"
    ok, output = run_git(["reset", "--hard", f"origin/{branch}"], cwd=meta.repo_dir)
    if not ok:
        print(f"  [FAIL] git reset --hard origin/{branch}")
        for line in output.strip().splitlines():
            print(f"         {line}")
        return 1
    print(f"  [OK] git reset --hard origin/{branch}")

    ok, output = run_git(["clean", "-fd"], cwd=meta.repo_dir)
    if not ok:
        print("  [FAIL] git clean -fd")
        for line in output.strip().splitlines():
            print(f"         {line}")
        return 1
    print("  [OK] git clean -fd")

    ok, commit_out = run_git(["rev-parse", "--short", "HEAD"], cwd=meta.repo_dir)
    if ok:
        print(f"  Now at commit: {commit_out.strip()}")

    print()
    print(f"Updated: {name} from {redact_url(meta.git_repo)} ({branch})")
    print("(Static files update immediately — no nginx reload needed.)")

    print()
    verify_and_maybe_restart(meta.domain)

    return 0


def cmd_enable_ssl(args) -> int:
    name = args.name
    email = args.email

    site_checks = run_enable_ssl_site_checks(name)
    site_exists = any(c["label"] == "Site exists" and c["passed"] for c in site_checks)

    meta = None
    ssl_checks: list[dict] = []
    if site_exists:
        meta = try_read_metadata(name) or SiteMetadata(
            name=name, domain=extract_domain(NGINX_SITES_AVAILABLE / name) or name,
        )
        ssl_checks = run_ssl_checks(meta.domain, email)

    print(f"Site: {name}")
    if meta:
        print(f"Domain: {meta.domain}")
    print()

    if not print_preflight_results(site_checks + ssl_checks):
        return 1
    print()

    return enable_ssl_for_site(meta, email)


# =====================================================================
# Orchestration
# =====================================================================

def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    handlers = {
        "create": cmd_create,
        "list": cmd_list,
        "status": cmd_status,
        "enable": cmd_enable,
        "disable": cmd_disable,
        "remove": cmd_remove,
        "update": cmd_update,
        "enable-ssl": cmd_enable_ssl,
        "doctor": cmd_doctor,
    }
    return handlers[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
