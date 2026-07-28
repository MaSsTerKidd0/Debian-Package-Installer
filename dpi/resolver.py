"""
The resolver: turn one dependency atom into one concrete package, apt-style.

Handles arch qualifiers (:any/:native/:arch), [arch] restrictions, version
constraints (in Depends and in versioned Provides), virtual packages, and
alternatives (A | B | C). Newest version wins among candidates.
"""

from typing import Dict, List, Optional, Set

from debian.debian_support import Version

from .models import DepAtom, PackageRecord


def version_satisfies(candidate_version: str,
                      op: Optional[str],
                      needed_version: Optional[str]) -> bool:
    """
    Compare candidate_version against a constraint like (>= needed_version),
    using debian.debian_support.Version for Debian semantics (epoch, revision).
    """
    if op is None or needed_version is None:
        return True

    cand_v = Version(candidate_version)
    need_v = Version(needed_version)

    if op == "=":
        return cand_v == need_v
    elif op == ">=":
        return cand_v >= need_v
    elif op == "<=":
        return cand_v <= need_v
    elif op == ">>":
        return cand_v > need_v
    elif op == "<<":
        return cand_v < need_v
    else:
        # Operator was validated at parse time; reaching here means code drift.
        raise RuntimeError(f"Internal error: unknown version operator {op!r} at comparison time.")


def provided_version_satisfies(
    provider_pkg: PackageRecord,
    virtual_name: str,
    op: Optional[str],
    needed_version: Optional[str],
) -> bool:
    """
    Decide whether `provider_pkg`'s "Provides: virtual_name" satisfies a versioned
    dependency (op, needed_version).

      - No version constraint requested -> satisfied.
      - Provider declared a Provides version -> compare that.
      - Versioned dep, but provider declared no version for the virtual -> NOT
        satisfied. We do not fall back to the provider's own package version;
        dpkg/apt require an explicit declared version on the virtual.

    Returns False (rather than raising) for the last case: an unsatisfiable
    provider is normal -- the caller may have other providers, and above that
    resolve_dep_alternatives() may have another alternative. If nothing in the
    group satisfies, the caller raises with full context.
    """
    if op is None or needed_version is None:
        return True

    declared_ver = provider_pkg.provides_map.get(virtual_name)
    if declared_ver:
        return version_satisfies(declared_ver, op, needed_version)

    return False


def candidate_arches_for_atom(atom: DepAtom, target_arch: str) -> Set[str]:
    """
    Which architectures may satisfy this atom (simplified Debian semantics for
    our single-target offline use case):

     - unqualified "pkg"      -> {target_arch, "all"}  (Architecture: all counts)
     - "pkg:any"              -> {target_arch, "all"}  (we don't pull foreign arches)
     - "pkg:native"           -> {target_arch, "all"}  ("native" == our target)
     - "pkg:<explicit-arch>"  -> {that arch}           (no "all": a specific build)
    """
    q = atom.arch_qual
    if q is None:
        return {target_arch, "all"}
    if q == "any":
        return {target_arch, "all"}
    if q == "native":
        return {target_arch, "all"}
    return {q}


def arch_restriction_allows(atom: DepAtom, target_arch: str) -> bool:
    """A '[arch1 arch2]' annotation means the dep only applies on those arches."""
    if not atom.arch_list:
        return True
    return target_arch in atom.arch_list


def select_best_candidate(candidates: List[PackageRecord]) -> PackageRecord:
    """
    Pick the newest Version (Debian ordering) among candidates, with a stable
    tie-break by source_hint for deterministic runs. Suite/Priority pinning is
    intentionally not implemented -- see the project's Limitations note.
    """
    if not candidates:
        raise RuntimeError("Internal error: select_best_candidate called with empty list.")

    sorted_cands = sorted(
        candidates,
        key=lambda pkg: (Version(pkg.version), pkg.source_hint),
        reverse=True,
    )
    return sorted_cands[0]


def resolve_single_atom(
    atom: DepAtom,
    target_arch: str,
    pkgs_by_name: Dict[str, List[PackageRecord]],
    provides_index: Dict[str, List[PackageRecord]],
) -> Optional[PackageRecord]:
    """
    Resolve one DepAtom into a concrete PackageRecord, or None if unsatisfiable.
    Does NOT recurse; only answers "which .deb would apt pick for THIS atom?".

    Order: arch-restriction gate -> direct name match (arch + version filtered)
    -> virtual Provides match. Newest wins within each. None here tells
    resolve_dep_alternatives() to try the next alternative.
    """
    if not arch_restriction_allows(atom, target_arch):
        return None

    candidate_arches = candidate_arches_for_atom(atom, target_arch)

    # Direct lookup
    direct_matches = [
        pkg for pkg in pkgs_by_name.get(atom.name, [])
        if pkg.arch in candidate_arches and version_satisfies(pkg.version, atom.op, atom.ver)
    ]
    if direct_matches:
        return select_best_candidate(direct_matches)

    # Via virtual Provides
    provider_matches = []
    for provider in provides_index.get(atom.name, []):
        if provider.arch not in candidate_arches:
            continue
        # A versioned virtual whose provider declares no version for it simply
        # doesn't count; keep scanning the other providers.
        if not provided_version_satisfies(provider, atom.name, atom.op, atom.ver):
            continue
        provider_matches.append(provider)

    if provider_matches:
        return select_best_candidate(provider_matches)

    return None


def resolve_dep_alternatives(
    alt_group: List[DepAtom],
    target_arch: str,
    pkgs_by_name: Dict[str, List[PackageRecord]],
    provides_index: Dict[str, List[PackageRecord]],
) -> PackageRecord:
    """
    Resolve one comma-separated dependency group "A | B | C", trying each
    alternative in order like apt. Raises (rather than guessing) if none can be
    satisfied -- guessing could download the wrong provider and hide the problem.
    """
    for atom in alt_group:
        pkg = resolve_single_atom(atom, target_arch, pkgs_by_name, provides_index)
        if pkg:
            return pkg

    # Couldn't satisfy ANY alternative -- verbose, debuggable error.
    human_alts = []
    for atom in alt_group:
        desc = atom.name
        if atom.arch_qual:
            desc += f":{atom.arch_qual}"
        if atom.op and atom.ver:
            desc += f" ({atom.op} {atom.ver})"
        if atom.arch_list:
            desc += f" [{', '.join(atom.arch_list)}]"
        human_alts.append(desc)

    raise RuntimeError(
        "Could not satisfy any alternative in dependency group: "
        + " | ".join(human_alts)
        + "\nReasons this may happen:\n"
        + " - The required package only exists for a different architecture than "
        + "   the repository target arch.\n"
        + " - The dependency is virtual and none of its providers are available "
        + "   for this arch.\n"
        + " - A strict version constraint could not be met.\n"
        + " - The dependency is a versioned virtual and no provider declares a "
        + "   version for it in its Provides field.\n"
        + " - The Packages index in ./repository is incomplete or mismatched to "
        + "   the actual APT sources for the target system.\n"
    )
