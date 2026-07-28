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
from dpi.starter_kits import load_starter_kits, match_machine, merge_packages


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
    parser.add_argument(
        '--machine',
        metavar='TYPE',
        help=(
            "Also include the 'essential packages' starter kit for this machine type\n"
            "(e.g. 'pi', 'pc', 'tablet', 'server'). Matched case-insensitively against\n"
            "starter_kits.json. Implies --keep-going, since kit names span distros.\n"
            "Use --list-machines to see the options."
        )
    )
    parser.add_argument('--ssh', action='store_true', help='Also include the OpenSSH server (openssh-server).')
    parser.add_argument('--list-machines', action='store_true', help='List starter-kit machine types and exit.')
    parser.add_argument('--repo-dir', default=config.REPO_DIR, help='Directory holding the Packages index .txt files.')
    parser.add_argument('--download-dir', default=config.DOWNLOAD_DIR, help='Directory to download .deb files into.')
    parser.add_argument(
        '--bundle',
        metavar='OUTPUT.tar.gz',
        help='After downloading, package everything into a self-installing .tar.gz at this path.'
    )
    parser.add_argument(
        '--keep-going',
        action='store_true',
        help=(
            'Do not abort when a requested package cannot be resolved (e.g. a typo or a\n'
            'custom/vendor .deb not in the sources). Warn, skip that package, continue with\n'
            'the rest, and print a summary. Default: stop on the first failure.'
        )
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

    # Starter kits (for --machine / --ssh / --list-machines). Load lazily so a
    # broken starter_kits.json only matters when those options are used.
    kits = load_starter_kits()

    if args.list_machines:
        print("Available machine types (starter kits):")
        for name, kit in kits.items():
            desc = kit.get("description", "")
            print(f"  {name}  ({len(kit['packages'])} packages)")
            if desc:
                print(f"      {desc}")
        return

    base_urls = [u.strip() for u in args.base_url.split(',') if u.strip()]
    if not base_urls:
        raise SystemExit("No valid --base-url values provided. At least one is required.")

    # Assemble the final package list: what the user asked for, plus any add-ons.
    user_packages = args.packages or []
    add_ons = []
    machine_used = None
    if args.machine:
        try:
            machine_used = match_machine(args.machine, kits)
        except ValueError as e:
            raise SystemExit(f"{e}\nUse --list-machines to see the options.")
        add_ons += kits[machine_used]["packages"]
    if args.ssh:
        add_ons.append("openssh-server")

    packages = merge_packages(user_packages, add_ons)
    if not packages:
        raise SystemExit(
            "Nothing to do. Specify --packages <pkg...>, and/or --machine <type> / --ssh."
        )

    # Kit names deliberately span distros (e.g. Ubuntu 'linux-firmware' vs Debian
    # 'firmware-*'); enable keep-going so the non-matching names are skipped
    # rather than aborting the run.
    keep_going = args.keep_going or bool(machine_used)

    reporter = ConsoleReporter()

    if machine_used:
        extra = len(packages) - len(user_packages)
        print(f"Including {extra} extra package(s) from the '{machine_used}' starter kit"
              + (" + OpenSSH" if args.ssh else "")
              + (" (keep-going auto-enabled)." if not args.keep_going else "."))

    try:
        target_arch, visited, failures = resolve_and_download(
            packages,
            base_urls,
            repo_dir=args.repo_dir,
            download_dir=args.download_dir,
            reporter=reporter,
            verify_deb_metadata=args.verify_deb_metadata,
            keep_going=keep_going,
        )
    except Exception as e:
        raise SystemExit(f"\nCRITICAL ERROR: {e}")

    # Summary. With keep-going, skipped packages are reported here rather than
    # having aborted the run.
    succeeded = [p for p in packages if p not in {name for name, _ in failures}]
    print(f"\nDone. Requested: {len(packages)}, "
          f"resolved: {len(succeeded)}, skipped: {len(failures)}. "
          f"{len(visited)} .deb file(s) downloaded.")
    if failures:
        print("Skipped packages (not found or unresolvable):")
        for name, reason in failures:
            print(f"  - {name}: {reason.strip().splitlines()[0] if reason.strip() else 'unknown reason'}")

    if args.bundle:
        if not visited:
            print("\nNothing was downloaded, so no bundle was built.")
            return
        try:
            build_bundle(args.download_dir, args.bundle, target_arch, succeeded, reporter)
        except Exception as e:
            raise SystemExit(f"\nCRITICAL ERROR while building bundle: {e}")


if __name__ == "__main__":
    main()
