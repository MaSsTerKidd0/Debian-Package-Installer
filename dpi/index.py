"""Build the in-memory package index from ./repository/*.txt."""

import os
import re
from typing import Dict, List, Optional, Set, Tuple

from .models import PackageRecord
from .parsing import (
    parse_stanzas_from_file,
    parse_provides_field,
    extract_target_arch_from_repo_filename,
)
from .reporter import Reporter, ConsoleReporter


def build_package_index(
    repo_dir: str,
    reporter: Optional[Reporter] = None,
) -> Tuple[Dict[str, List[PackageRecord]], Dict[str, List[PackageRecord]], str]:
    """
    Read ALL repo_dir/*.txt files and build two lookup tables:

    pkgs_by_name:    { "real-package-name": [PackageRecord, ...] }
    provides_index:  { "virtual-or-aliased-name": [PackageRecord, ...] }

    Also detect the single target architecture we're assembling for. We REQUIRE
    that all repository files agree on one arch; mixing arches would break the
    "download everything for a Pi without having a Pi" promise, so we raise.

    Multiple PackageRecord entries per name are kept (multiple versions across
    suites); the resolver chooses the best later.
    """
    if reporter is None:
        reporter = ConsoleReporter()

    if not os.path.isdir(repo_dir) or not os.listdir(repo_dir):
        raise FileNotFoundError(
            f"Repository directory '{repo_dir}' not found or is empty.\n"
            f"Please run 'python3 update_repository.py' to download package lists first."
        )

    pkgs_by_name: Dict[str, List[PackageRecord]] = {}
    provides_index: Dict[str, List[PackageRecord]] = {}
    detected_arches: Set[str] = set()

    text_files = [os.path.join(repo_dir, f) for f in os.listdir(repo_dir) if f.endswith('.txt')]
    if not text_files:
        raise RuntimeError(
            f"No .txt repository files found in {repo_dir!r}. "
            f"Did update_repository.py run correctly?"
        )

    for txt_path in text_files:
        source_hint = _source_hint_from_filename(os.path.basename(txt_path))
        detected_arches.add(extract_target_arch_from_repo_filename(os.path.basename(txt_path)))

        for stanza in parse_stanzas_from_file(txt_path):
            name = stanza.get("Package")
            version = stanza.get("Version")
            arch = stanza.get("Architecture")
            filename = stanza.get("Filename")

            # Mandatory fields: refuse incomplete stanzas rather than resolving
            # deps with missing data and producing garbage.
            if not (name and version and arch and filename):
                raise RuntimeError(
                    f"Malformed stanza in {txt_path} (source {source_hint}): "
                    f"missing one of required fields Package/Version/Architecture/Filename. "
                    f"Stanza keys: {list(stanza.keys())}"
                )

            provides_map = parse_provides_field(stanza.get("Provides", ""))

            record = PackageRecord(
                name=name,
                version=version,
                arch=arch,
                filename=filename,
                depends_raw=stanza.get("Depends", ""),
                pre_depends_raw=stanza.get("Pre-Depends", ""),
                provides_map=provides_map,
                multi_arch=stanza.get("Multi-Arch", ""),
                priority=stanza.get("Priority", ""),
                source_hint=source_hint,
            )

            pkgs_by_name.setdefault(name, []).append(record)
            for virt_name in provides_map.keys():
                provides_index.setdefault(virt_name, []).append(record)

    # Exactly one target architecture across all files: our contract is
    # "download the world for THIS target arch, offline".
    if len(detected_arches) != 1:
        raise RuntimeError(
            f"Inconsistent target architectures detected across repository/*.txt: "
            f"{detected_arches!r}. We do not support multi-arch graphs in one run. "
            f"Please regenerate ./repository for exactly one arch using update_repository.py "
            f"(see README)."
        )

    target_arch = list(detected_arches)[0]
    reporter.log(
        f"Loaded package metadata for target architecture '{target_arch}' "
        f"from {len(text_files)} repository file(s)."
    )
    total_records = sum(len(v) for v in pkgs_by_name.values())
    reporter.log(f"Indexed {total_records} package records across all suites/components.")

    return pkgs_by_name, provides_index, target_arch


def _source_hint_from_filename(basefile: str) -> str:
    """
    Derive an origin/component/platform breadcrumb from a repository filename of
    the form '<host>-<suite>-<component>-binary-<arch>.txt'.

    The platform segment is itself 'binary-<arch>' and contains a '-', so peel it
    off with an anchored pattern before splitting the rest. We deliberately do
    NOT split origin from suite: BOTH can contain '-' ('deb-debian-org',
    'noble-updates'), so that boundary is genuinely ambiguous from the filename.
    This string is only a human-readable breadcrumb and a deterministic tie-break
    key -- do not build resolver logic on top of it.
    """
    stem = basefile[:-4]  # drop .txt

    m_platform = re.search(r'-(binary-[a-z0-9][a-z0-9+.-]*)$', stem)
    if not m_platform:
        raise RuntimeError(
            f"Repository file name {basefile!r} does not match expected pattern "
            f"'<host>-<suite>-<component>-binary-<arch>.txt'."
        )
    platform_hint = m_platform.group(1)
    rest = stem[:m_platform.start()]

    origin_hint, sep, component_hint = rest.rpartition('-')
    if not sep:
        raise RuntimeError(
            f"Repository file name {basefile!r} does not match expected pattern "
            f"'<host>-<suite>-<component>-binary-<arch>.txt'."
        )

    return f"{origin_hint}/{component_hint}/{platform_hint}"
