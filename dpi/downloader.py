"""
Download .deb files and walk the dependency graph.

Source of truth for dependencies is the Packages index (already parsed into
PackageRecord), NOT the .deb archive -- this is what apt does, and it keeps the
tool cross-platform. See _process_package_and_get_deps for the full rationale.
"""

import os
from typing import Dict, List, Optional, Set, Tuple

import requests

from .config import DOWNLOAD_TIMEOUT_SECONDS
from .models import PackageRecord
from .parsing import parse_dep_field, parse_single_dep_atom
from .reporter import Reporter, ConsoleReporter
from .resolver import resolve_single_atom, resolve_dep_alternatives


def _download_package_file(
    pkg: PackageRecord,
    base_urls: List[str],
    download_dir: str,
    reporter: Reporter,
) -> str:
    """
    Download the .deb for `pkg` into download_dir, trying each base URL in order.
    Writes to a .part temp file promoted with os.replace (atomic on POSIX and
    Windows) so an interrupted download never leaves a truncated .deb that the
    "already exists" check would treat as complete. Raises if no mirror works.
    """
    # Ensure the destination exists. The old script created ./downloaded at import
    # time; parametrizing the path means we create it lazily at first use instead.
    os.makedirs(download_dir, exist_ok=True)

    file_name = os.path.basename(pkg.filename)
    file_path = os.path.join(download_dir, file_name)

    # Already downloaded: skip. Preserves behavior and avoids re-fetching a
    # package the recursion reaches by multiple paths.
    if os.path.isfile(file_path):
        reporter.log(f"Exists: {file_name} is already downloaded.")
        return file_path

    relative_path = pkg.filename.lstrip('/')

    downloaded = False
    # Track WHY each mirror failed: "404 everywhere" (wrong base URL) and "the
    # network timed out" (transport problem) need different fixes.
    failures: List[str] = []
    saw_non_404 = False
    for base in base_urls:
        try_url = f"{base.rstrip('/')}/{relative_path}"
        reporter.log(f"Downloading: {file_name} from {try_url}")
        tmp_path = file_path + ".part"
        try:
            response = requests.get(try_url, timeout=DOWNLOAD_TIMEOUT_SECONDS)
            response.raise_for_status()
            with open(tmp_path, 'wb') as outf:
                outf.write(response.content)
            os.replace(tmp_path, file_path)
            downloaded = True
            break
        except requests.exceptions.RequestException as e:
            status = getattr(getattr(e, "response", None), "status_code", None)
            if status != 404:
                saw_non_404 = True
            reporter.log(f"Failed to fetch from {try_url}: {e}")
            failures.append(f"  {try_url}\n    -> {e}")
            continue
        finally:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)

    if not downloaded:
        if saw_non_404:
            hint = (
                "At least one mirror failed for a reason other than 404 (timeout, "
                "connection reset, DNS, or a 5xx). This is usually a network or "
                "mirror-availability problem, not a wrong --base-url. Try again, or "
                "switch to a different mirror (e.g. https://archive.ubuntu.com/ubuntu)."
            )
        else:
            hint = (
                "Every mirror returned 404 for this path. That usually means your "
                "--base-url list does not match the actual origins/suites of the "
                "packages in ./repository."
            )
        raise RuntimeError(
            f"Error fetching {file_name}: could not download from any base URL.\n"
            f"Looked for relative path '{relative_path}'.\n"
            f"Attempts:\n" + "\n".join(failures) + "\n\n" + hint + "\n"
        )

    return file_path


def _merged_dep_string(pre_depends: str, depends: str) -> str:
    """
    Join Pre-Depends and Depends into one comma-separated field. We include BOTH
    because Pre-Depends is not optional for offline install: dpkg refuses to
    unpack a package whose Pre-Depends aren't already unpacked and configured.
    Recommends/Suggests are ignored on purpose (core closure, not full userland).
    """
    return ", ".join(x.strip() for x in (pre_depends, depends) if x and x.strip())


def _verify_deb_matches_index(file_path: str, pkg: PackageRecord, expected_deps: str) -> None:
    """
    Optional integrity cross-check: open the downloaded .deb and confirm its
    embedded control data agrees with the index stanza. OFF by default because
    it can't run everywhere (see _process_package_and_get_deps). A mismatch means
    the mirror served a different build than its index advertises -> we raise.
    """
    from debian.debfile import DebFile  # lazy: zstd decompression is POSIX-only

    control = DebFile(file_path).debcontrol()
    actual = _merged_dep_string(control.get('Pre-Depends', ''), control.get('Depends', ''))

    def _norm(s: str) -> str:
        return " ".join(s.split())

    if _norm(actual) != _norm(expected_deps):
        raise RuntimeError(
            f"Integrity mismatch for {os.path.basename(file_path)} "
            f"({pkg.name} {pkg.version} {pkg.arch}, from {pkg.source_hint}):\n"
            f"  Packages index says: {expected_deps!r}\n"
            f"  The .deb itself says: {actual!r}\n"
            "The mirror served a different build than its index advertises. "
            "Re-run update_repository.py to refresh the index, or use a different mirror."
        )


def _process_package_and_get_deps(
    pkg: PackageRecord,
    base_urls: List[str],
    download_dir: str,
    reporter: Reporter,
    verify_deb_metadata: bool = False,
) -> List[List["object"]]:
    """
    Download the .deb for `pkg`, then return its parsed dependency groups.

    SOURCE OF TRUTH: the Packages index, not the .deb archive -- which is what
    apt does; the index is authoritative and (in real apt) signed.

    PORTABILITY: modern .debs ship control.tar.zst. python-debian decompresses
    zstd by shelling out with subprocess(preexec_fn=...), and preexec_fn is
    POSIX-only, so opening the archive on every package would make the whole tool
    Linux-only -- for metadata we already parsed. verify_deb_metadata=True opts
    into that cross-check where the platform supports it.
    """
    file_path = _download_package_file(pkg, base_urls, download_dir, reporter)

    merged = _merged_dep_string(pkg.pre_depends_raw, pkg.depends_raw)

    if verify_deb_metadata:
        try:
            _verify_deb_matches_index(file_path, pkg, merged)
        except RuntimeError:
            raise  # a genuine mismatch
        except Exception as e:
            raise RuntimeError(
                f"Could not read {os.path.basename(file_path)} to verify it "
                f"({pkg.name} {pkg.version} {pkg.arch}): {e}\n"
                "If this is a preexec_fn/unzstd error, you are on a platform where "
                "python-debian cannot decompress zstd .debs. Drop --verify-deb-metadata; "
                "resolution works fine without it."
            )

    if not merged:
        return []
    return parse_dep_field(merged)


def fetch_dependencies_recursive(
    initial_pkg_name: str,
    target_arch: str,
    pkgs_by_name: Dict[str, List[PackageRecord]],
    provides_index: Dict[str, List[PackageRecord]],
    base_urls: List[str],
    visited_pkgkeys: Set[Tuple[str, str, str]],
    download_dir: str,
    reporter: Optional[Reporter] = None,
    verify_deb_metadata: bool = False,
) -> None:
    """
    Resolve `initial_pkg_name` (treated as a single dependency atom), download it,
    then DFS its dependency groups -- downloading each chosen package -- until the
    graph closes. visited_pkgkeys guards against cycles and shared subtrees. Any
    unresolved dependency raises immediately rather than producing a bundle that
    looks complete but won't install.
    """
    if reporter is None:
        reporter = ConsoleReporter()

    fake_atom = parse_single_dep_atom(initial_pkg_name.strip())
    top_pkg = resolve_single_atom(fake_atom, target_arch, pkgs_by_name, provides_index)
    if not top_pkg:
        raise RuntimeError(
            f"Top-level package '{initial_pkg_name}' could not be resolved for arch "
            f"{target_arch}. This means:\n"
            f" - The package isn't in ./repository for this arch,\n"
            f" - or it only exists as a virtual with no valid provider,\n"
            f" - or only exists for a different arch than {target_arch}.\n"
        )

    stack: List[PackageRecord] = [top_pkg]

    while stack:
        pkg = stack.pop()

        pkg_key = (pkg.name, pkg.version, pkg.arch)
        if pkg_key in visited_pkgkeys:
            continue
        visited_pkgkeys.add(pkg_key)

        dep_groups = _process_package_and_get_deps(
            pkg, base_urls, download_dir, reporter, verify_deb_metadata
        )

        for alt_group in dep_groups:
            chosen = resolve_dep_alternatives(alt_group, target_arch, pkgs_by_name, provides_index)
            stack.append(chosen)

        # Coarse progress: done vs. done + still-queued. Advances toward 100% as
        # the frontier drains. Exact total is unknowable up front (lazy DFS).
        reporter.progress(len(visited_pkgkeys), len(visited_pkgkeys) + len(stack))

    reporter.log(f"Info: Finished resolving '{initial_pkg_name}' and its dependency tree.")
