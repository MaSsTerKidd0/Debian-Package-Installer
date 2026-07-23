"""
Debian Package Installer -- offline dependency downloader.

This package assembles a complete, self-contained set of .deb files (a package
plus its entire transitive dependency closure) for a target Debian/Ubuntu/
Raspberry Pi system, from a machine that is NOT that target -- including a
different OS and a different CPU architecture.

Modules:
  config      -- default paths and tunables
  reporter    -- log/progress sink (console or GUI callbacks)
  models      -- PackageRecord, DepAtom data structures
  parsing     -- Packages-index and dependency-field parsers
  index       -- build the in-memory package index from ./repository/*.txt
  resolver    -- turn a dependency atom into a concrete package (apt-like)
  downloader  -- download .debs and walk the dependency graph
  repository  -- fetch and decompress the Packages indexes (update step)
  packager    -- bundle downloads into a .tar.gz with a self-installing script

The high-level entry point most callers want is resolve_and_download() below.
"""

from typing import List, Set, Tuple

from .reporter import Reporter, ConsoleReporter
from .index import build_package_index
from .downloader import fetch_dependencies_recursive
from . import config


def resolve_and_download(
    package_names: List[str],
    base_urls: List[str],
    repo_dir: str = config.REPO_DIR,
    download_dir: str = config.DOWNLOAD_DIR,
    reporter: Reporter = None,
    verify_deb_metadata: bool = False,
) -> Tuple[str, Set[Tuple[str, str, str]]]:
    """
    Build the index from `repo_dir`, then resolve and download every package in
    `package_names` together with its full transitive dependency closure into
    `download_dir`.

    Returns (target_arch, visited_pkgkeys) where visited_pkgkeys is the set of
    (name, version, arch) tuples actually downloaded -- useful for reporting.

    This is the single orchestration point shared by the CLI and the GUI so the
    two can never drift apart in behavior.
    """
    if reporter is None:
        reporter = ConsoleReporter()

    pkgs_by_name, provides_index, target_arch = build_package_index(repo_dir, reporter)

    visited: Set[Tuple[str, str, str]] = set()
    for name in package_names:
        fetch_dependencies_recursive(
            name,
            target_arch,
            pkgs_by_name,
            provides_index,
            base_urls,
            visited,
            download_dir,
            reporter,
            verify_deb_metadata,
        )

    return target_arch, visited
