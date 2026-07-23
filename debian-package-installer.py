#!/usr/bin/env python3
"""
CLI entry point: resolve and download Debian packages plus their full transitive
dependency closure, optionally packaging the result into a self-installing
.tar.gz bundle.

The real logic lives in the `dpi` package; this file is a thin argument-parsing
shell so the historical command line keeps working:

    python3 debian-package-installer.py --packages ffmpeg
"""

import argparse

from dpi import config, resolve_and_download
from dpi.packager import build_bundle
from dpi.reporter import ConsoleReporter


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch dependencies for given Debian packages.")
    parser.add_argument(
        '--base-url',
        type=str,
        default='https://archive.ubuntu.com/ubuntu',
        help=(
            'Comma-separated base URLs to the archives where deb packages are downloaded from.\n'
            'IMPORTANT:\n'
            '  - These MUST correspond to the same origins/suites you ran update_repository.py against.\n'
            '  - Each base URL is tried in order for every .deb.\n'
            '  - If none work for a given package, we raise a hard error.'
        )
    )
    parser.add_argument('--packages', nargs='+', help='List of Debian packages to fetch dependencies for.')
    parser.add_argument('--repo-dir', default=config.REPO_DIR, help='Directory holding the Packages index .txt files.')
    parser.add_argument('--download-dir', default=config.DOWNLOAD_DIR, help='Directory to download .deb files into.')
    parser.add_argument(
        '--bundle',
        metavar='OUTPUT.tar.gz',
        help='After downloading, package everything into a self-installing .tar.gz at this path.'
    )
    parser.add_argument(
        '--verify-deb-metadata',
        action='store_true',
        help=(
            'After downloading each .deb, open it and cross-check its control data against the\n'
            'index, failing on any mismatch (catches mirror drift). OFF by default: modern .debs\n'
            'use zstd, which python-debian can only decompress on POSIX. Leave off on Windows.'
        )
    )
    args = parser.parse_args()

    base_urls = [u.strip() for u in args.base_url.split(',') if u.strip()]
    if not base_urls:
        raise SystemExit("No valid --base-url values provided. At least one is required.")
    if not args.packages:
        raise SystemExit("No packages specified. Use --packages <pkg1> <pkg2> ...")

    reporter = ConsoleReporter()

    try:
        target_arch, _visited = resolve_and_download(
            args.packages,
            base_urls,
            repo_dir=args.repo_dir,
            download_dir=args.download_dir,
            reporter=reporter,
            verify_deb_metadata=args.verify_deb_metadata,
        )
    except Exception as e:
        raise SystemExit(f"\nCRITICAL ERROR: {e}")

    print("\nAll dependencies processed.")

    if args.bundle:
        try:
            build_bundle(args.download_dir, args.bundle, target_arch, args.packages, reporter)
        except Exception as e:
            raise SystemExit(f"\nCRITICAL ERROR while building bundle: {e}")


if __name__ == "__main__":
    main()
