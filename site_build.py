#!/usr/bin/env python3
"""
Parish Site Builder
v0.1 — create a web root directory for a site.

Usage:
    site_build.py <name>

Example:
    site_build.py messiah
        -> creates /srv/www/messiah
"""

import argparse
import sys
from pathlib import Path

WEB_ROOT_BASE = Path("/srv/www")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="site_build.py",
        description="Parish Site Builder — create and manage parish website directories.",
    )
    parser.add_argument(
        "name",
        help="Site name, e.g. 'messiah'. Used as the directory name under /srv/www.",
    )
    return parser


def create_site_directory(name: str) -> int:
    """
    Create /srv/www/<name>.

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

    # TODO(v0.2+): set ownership to www-data:www-data once nginx is in the picture.
    print(f"Created {site_path}")
    return 0


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return create_site_directory(args.name)


if __name__ == "__main__":
    sys.exit(main())
