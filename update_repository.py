#!/usr/bin/env python3
"""
CLI entry point: download and prepare the Packages indexes the resolver reads.

Thin argument-parsing shell over dpi.repository so the historical command line
keeps working:

    python3 update_repository.py
"""

import argparse

from dpi import config
from dpi.repository import update_repository
from dpi.reporter import ConsoleReporter


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Download and prepare Ubuntu/Debian package lists for the dependency resolver.",
        formatter_class=argparse.RawTextHelpFormatter,
    )
    parser.add_argument(
        '--base-url',
        type=str,
        default='https://us.archive.ubuntu.com/ubuntu/dists',
        help='The base URL for the distribution\'s dists archive (must include scheme).'
    )
    parser.add_argument(
        '--suites',
        nargs='+',
        default=['jammy', 'noble', 'noble-updates', 'noble-security', 'noble-backports'],
        help='A space-separated list of suites (e.g., noble noble-updates).'
    )
    parser.add_argument(
        '--components',
        nargs='+',
        default=['main', 'restricted', 'universe', 'multiverse'],
        help='A space-separated list of repository components (e.g., main universe).'
    )
    parser.add_argument(
        '--platform',
        type=str,
        default='binary-amd64',
        help='The target architecture platform (e.g., binary-arm64).'
    )
    parser.add_argument('--repo-dir', default=config.REPO_DIR, help='Directory to write the index .txt files into.')
    args = parser.parse_args()

    try:
        update_repository(
            args.base_url,
            args.suites,
            args.components,
            args.platform,
            repo_dir=args.repo_dir,
            reporter=ConsoleReporter(),
        )
    except ValueError as e:
        raise SystemExit(str(e))


if __name__ == "__main__":
    main()
