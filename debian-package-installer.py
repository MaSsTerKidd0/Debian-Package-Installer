import os
import requests
from debian.debfile import DebFile
from debian.debian_support import Version
from typing import Set, List, Dict, Optional, Tuple
import argparse
import re

"""
This script used to work like this:
 - scrape only `Filename:` lines from ./repository/*.txt,
 - guess package matches by filename prefix,
 - download newest filename match,
 - read its Depends field,
 - recurse.

That was "good enough" for trivial trees, but it fails hard on real Debian/Raspberry Pi dependency graphs
for multiple reasons that are NOT optional in Debian packaging:
 - `foo:any`, `foo:native`, `foo:arm64`
 - virtual packages (things that appear only via Provides:)
 - alternatives (`a | b | c`)
 - Architecture: all packages that satisfy deps even on arch-specific systems
 - Pre-Depends
 - version constraints in Depends and in Provides
 - recursive loops (must guard against them sanely)

This rewrite keeps the public CLI behavior and the general flow,
but replaces the resolver internals to behave more like `apt` for the target arch.

We still intentionally ONLY download packages for the single *target* architecture
that the repo files were indexed for (ex: arm64 for Raspberry Pi OS).
We do NOT try to pull in other arches. We treat the host we're running on as irrelevant.
"""

DOWNLOAD_DIR = "./downloaded/"
REPO_DIR = "./repository"

if not os.path.exists(DOWNLOAD_DIR):
    os.makedirs(DOWNLOAD_DIR)

##############################################################################
# Data structures
##############################################################################

class PackageRecord:
    """
    Represents ONE binary package entry from a Debian Packages index stanza.

    We store:
     - name: Package
     - version: Version
     - arch: Architecture (e.g. "arm64", "all")
     - filename: pool/... path, used for downloading
     - depends_raw: Depends field (string or '')
     - pre_depends_raw: Pre-Depends field (string or '')
     - provides_map: dict of {virtual_name: provided_version or None}
       NOTE: Debian allows "Provides: virt (= 1.2)". If provided_version is None,
       then the provider claims to provide it but without a declared version.
       When satisfying versioned deps on a virtual, we will fall back to checking
       the provider's own package version if the virtual didn't carry its own version.
     - multi_arch: Multi-Arch field string (may influence :any semantics in real APT;
       we currently don't duplicate full APT legality checks, but we keep it visible
       because future contributors are going to need it to be correct for edge cases.)
     - priority: Priority string (used by Debian to express importance like "required",
       "important", etc.). We may someday prefer higher priority providers for virtual
       packages when ambiguous. We store it now because picking a stable provider for
       virtuals is otherwise ambiguous.
     - source_hint: (suite/component/etc.)
       We infer some hints from the filename of the repository source .txt, so that we
       can explain where we found this package if we ever need to debug or choose
       between same-named packages across different suites.
    """
    __slots__ = (
        "name", "version", "arch", "filename",
        "depends_raw", "pre_depends_raw",
        "provides_map", "multi_arch", "priority",
        "source_hint"
    )

    def __init__(
        self,
        name: str,
        version: str,
        arch: str,
        filename: str,
        depends_raw: str,
        pre_depends_raw: str,
        provides_map: Dict[str, Optional[str]],
        multi_arch: str,
        priority: str,
        source_hint: str,
    ):
        self.name = name
        self.version = version
        self.arch = arch
        self.filename = filename
        self.depends_raw = depends_raw or ""
        self.pre_depends_raw = pre_depends_raw or ""
        self.provides_map = provides_map or {}
        self.multi_arch = multi_arch or ""
        self.priority = priority or ""
        self.source_hint = source_hint or ""


##############################################################################
# Parsing the Packages index files we already downloaded
##############################################################################

def _parse_stanzas_from_file(path: str) -> List[Dict[str, str]]:
    """
    Read one repository/*.txt file (which is basically a decompressed Packages.gz).

    Debian Packages files are a series of RFC822-style stanzas separated by blank lines,
    where fields can be continued with leading spaces.

    We *cannot* assume order of lines, but we CAN assume:
      - Key: Value on first line
      - Continuation lines start with space or tab

    We return a list of dicts (field_name -> full_value_stripped).
    This is a minimal reimplementation of deb822 parsing logic.

    If this parsing fails or produces nonsense, that is a *fatal* error because
    literally every resolver step depends on this data structure.
    """
    stanzas: List[Dict[str, str]] = []
    current: Dict[str, str] = {}

    try:
        # We must decode strictly; silent replacement would hide corrupted or
        # incorrectly encoded repository files and undermine correctness.
        with open(path, 'r', encoding='utf-8', errors='strict') as f:
            for raw_line in f:
                line = raw_line.rstrip('\n')
                if line == "":
                    # End of stanza
                    if current:
                        stanzas.append(current)
                        current = {}
                    continue

                if line.startswith(" ") or line.startswith("\t"):
                    # Continuation of previous field
                    if not current:
                        raise RuntimeError(
                            f"Invalid Packages file format: got continuation line "
                            f"but we don't have an active stanza in {path!r}: {line!r}"
                        )
                    # Append to the last key inserted
                    last_key = list(current.keys())[-1]
                    current[last_key] += "\n" + line.lstrip()
                else:
                    # New field
                    if ":" not in line:
                        raise RuntimeError(
                            f"Invalid Packages file format in {path!r}: "
                            f"line without colon: {line!r}"
                        )
                    key, val = line.split(":", 1)
                    key = key.strip()
                    val = val.lstrip()  # keep rest
                    current[key] = val
            # flush last stanza if file didn't end with blank line
            if current:
                stanzas.append(current)
    except FileNotFoundError:
        raise RuntimeError(f"Repository file not found while parsing: {path}")
    except UnicodeDecodeError as e:
        # Fail loudly on encoding problems; this indicates a broken or unexpected file.
        raise RuntimeError(f"Error parsing repository file {path}: UTF-8 decoding failed: {e}")
    except Exception as e:
        raise RuntimeError(f"Error parsing repository file {path}: {e}")

    return stanzas


def _parse_provides_field(provides_val: str) -> Dict[str, Optional[str]]:
    """
    Parse a Provides field which can look like:
        "foo (= 1.2), bar, baz (= 3)"
    Return dict { "foo": "1.2", "bar": None, "baz": "3" }

    We do NOT assume commas only separate entries; Debian syntax says commas do separate.
    We do NOT assume entries have no spaces.
    """
    result: Dict[str, Optional[str]] = {}
    if not provides_val:
        return result

    parts = [p.strip() for p in provides_val.split(',') if p.strip()]
    for p in parts:
        # Example tokens:
        #   "foo (= 1.2)"
        #   "bar"
        #   "baz (= 3)"
        m = re.match(r'^([a-z0-9][a-z0-9+.-]*)(?:\s*\(=\s*([^)]+)\))?$', p)
        if not m:
            # We refuse to silently swallow unexpected syntax, because wrong here
            # means you produce a "valid" but broken download set.
            raise RuntimeError(
                f"Unexpected Provides token syntax: {p!r}. "
                f"This parser assumes 'name' or 'name (= version)'."
            )
        virt_name = m.group(1)
        virt_ver = m.group(2) if m.group(2) else None
        result[virt_name] = virt_ver
    return result


def _extract_target_arch_from_repo_filename(filename: str) -> str:
    """
    update_repository.py names files like:
        {host}-{suite}-{component}-{platform}.txt
    where {platform} is 'binary-<arch>' (e.g. 'binary-arm64').

    The host itself may contain '-' (e.g. 'deb-debian-org'), so we must NOT
    rely on naive split() and taking the last segment. Instead, detect a tail
    of the form '-binary-<arch>' in the stem.
    """
    base = os.path.basename(filename)
    if not base.endswith('.txt'):
        raise RuntimeError(f"Internal error: expected .txt repo file, got {filename!r}")
    stem = base[:-4]  # drop .txt

    m = re.search(r'(?:^|-)binary-([a-z0-9][a-z0-9+.-]*)$', stem)
    if not m:
        raise RuntimeError(
            f"Cannot parse target arch from repository file name {filename!r}: "
            f"expected the pattern '*-binary-<arch>.txt'."
        )
    arch = m.group(1)
    return arch


def build_package_index(repo_dir: str) -> Tuple[Dict[str, List[PackageRecord]], Dict[str, List[PackageRecord]], str]:
    """
    Read ALL ./repository/*.txt files and build two lookup tables:

    pkgs_by_name:
        { "real-package-name": [PackageRecord, PackageRecord, ...] }
    provides_index:
        { "virtual-or-aliased-name": [PackageRecord, PackageRecord, ...] }

    Also detect the single target architecture we're assembling for (arm64, etc.).
    We REQUIRE that all repository files agree on the same target arch. If not,
    we raise because mixing arches would absolutely break the "download everything
    for a Pi without having a Pi" promise.

    NOTE: We keep multiple PackageRecord entries per name because you may have
    multiple versions across suites, etc. We will choose the "best" later.

    This function REPLACES the old `load_all_urls` which only scraped 'Filename:'.
    """
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
        # Derive a hint string to help users debug which suite/component/host this came from.
        # Filename pattern came from update_repository.py:
        #   output_filename = f"{host}-{suite}-{component}-{args.platform}.txt"
        basefile = os.path.basename(txt_path)
        stem = basefile[:-4]

        # Split from the RIGHT, because host can contain '-'s.
        pre, _sep, platform_hint = stem.rpartition('-')
        if not _sep:
            raise RuntimeError(
                f"Repository file name {basefile!r} does not match expected pattern "
                f"'<host>-<suite>-<component>-<platform>.txt'."
            )
        pre2, _sep, component_hint = pre.rpartition('-')
        if not _sep:
            raise RuntimeError(
                f"Repository file name {basefile!r} does not match expected pattern "
                f"'<host>-<suite>-<component>-<platform>.txt'."
            )
        host_hint, _sep, suite_hint = pre2.rpartition('-')
        if not _sep:
            raise RuntimeError(
                f"Repository file name {basefile!r} does not match expected pattern "
                f"'<host>-<suite>-<component>-<platform>.txt'."
            )

        source_hint = f"{host_hint}/{suite_hint}/{component_hint}/{platform_hint}"

        arch_from_file = _extract_target_arch_from_repo_filename(basefile)
        detected_arches.add(arch_from_file)

        stanzas = _parse_stanzas_from_file(txt_path)
        for stanza in stanzas:
            name = stanza.get("Package")
            version = stanza.get("Version")
            arch = stanza.get("Architecture")
            filename = stanza.get("Filename")
            depends_raw = stanza.get("Depends", "")
            pre_depends_raw = stanza.get("Pre-Depends", "")
            provides_raw = stanza.get("Provides", "")
            multi_arch = stanza.get("Multi-Arch", "")
            priority = stanza.get("Priority", "")

            # Sanity check: mandatory fields
            if not (name and version and arch and filename):
                # We refuse to silently accept incomplete stanzas because
                # then we "resolve" deps with missing data and produce garbage.
                raise RuntimeError(
                    f"Malformed stanza in {txt_path} (source {source_hint}): "
                    f"missing one of required fields Package/Version/Architecture/Filename. "
                    f"Stanza keys: {list(stanza.keys())}"
                )

            provides_map = _parse_provides_field(provides_raw)

            record = PackageRecord(
                name=name,
                version=version,
                arch=arch,
                filename=filename,
                depends_raw=depends_raw,
                pre_depends_raw=pre_depends_raw,
                provides_map=provides_map,
                multi_arch=multi_arch,
                priority=priority,
                source_hint=source_hint,
            )

            # Register into pkgs_by_name
            pkgs_by_name.setdefault(name, []).append(record)

            # Register into provides_index for each provided virtual name
            for virt_name in provides_map.keys():
                provides_index.setdefault(virt_name, []).append(record)

    # Validate target arches
    # We expect EXACTLY one 'target' architecture in all repo files.
    # Why: our business logic is "download the world for THIS target arch, offline".
    # Mixing multiple arches here would invalidate everything.
    if len(detected_arches) != 1:
        raise RuntimeError(
            f"Inconsistent target architectures detected across repository/*.txt: "
            f"{detected_arches!r}. We do not support multi-arch graphs in one run. "
            f"Please regenerate ./repository for exactly one arch using update_repository.py "
            f"(see README)."
        )

    target_arch = list(detected_arches)[0]
    print(f"Loaded package metadata for target architecture '{target_arch}' "
          f"from {len(text_files)} repository file(s).")

    total_records = sum(len(v) for v in pkgs_by_name.values())
    print(f"Indexed {total_records} package records across all suites/components.")

    return pkgs_by_name, provides_index, target_arch


##############################################################################
# Dependency parsing helpers
##############################################################################

class DepAtom:
    """
    Represents a single atomic dependency requirement like:
        "perl:any (>= 5.36) [arm64]"
        "liborc-0.4-dev-bin:any (= 1:0.4.33-2)"
        "dbus-session-bus"
        "default-dbus-session-bus"
        "zlib1g:arm64"
        "bash:native"

    Fields:
      - name: base package name (no :arch suffix here)
      - arch_qual: one of:
            None        -> no arch qualifier
            "any"       -> ':any'
            "native"    -> ':native'
            "arm64"     -> ':arm64', ':amd64', etc.
      - op: version operator (">=", "<=", "=", ">>", "<<"), or None
      - ver: version string if op is not None, else None
      - arch_list: optional list of arches from "[arch1 arch2]".
                   If present, this dep ONLY applies if target_arch is in that list.
    """
    __slots__ = ("name", "arch_qual", "op", "ver", "arch_list")

    def __init__(self, name: str,
                 arch_qual: Optional[str],
                 op: Optional[str],
                 ver: Optional[str],
                 arch_list: Optional[List[str]]):
        self.name = name
        self.arch_qual = arch_qual
        self.op = op
        self.ver = ver
        self.arch_list = arch_list or []


def parse_dep_field(dep_field_val: str) -> List[List[DepAtom]]:
    """
    Parse a Depends or Pre-Depends field into a list-of-lists:

    Return structure:
        [
          [DepAtom, DepAtom, ...],  # first alternative group "A | B | C"
          [DepAtom],                # next dep in comma list
          [DepAtom, DepAtom, ...],  # etc.
        ]

    Example:
        "default-dbus-session-bus | dbus-session-bus, perl:any, liborc-0.4-dev-bin:any (= 1:0.4.33-2)"

    becomes:
        [
          [DepAtom('default-dbus-session-bus',...), DepAtom('dbus-session-bus', ...)],
          [DepAtom('perl', arch_qual='any', ...)],
          [DepAtom('liborc-0.4-dev-bin', arch_qual='any', op='=', ver='1:0.4.33-2')]
        ]

    We are careful and strict here because if we silently mis-parse, the resolver
    may select nonsense packages, which is *worse* than failing loudly.
    """
    if not dep_field_val:
        return []

    results: List[List[DepAtom]] = []

    # Split top-level by commas = logical AND
    comma_groups = [grp.strip() for grp in dep_field_val.split(',') if grp.strip()]
    for comma_grp in comma_groups:
        # Now split alternatives by '|', preserving order.
        alts_raw = [alt.strip() for alt in comma_grp.split('|') if alt.strip()]
        alt_atoms: List[DepAtom] = []
        for atom_raw in alts_raw:
            alt_atoms.append(_parse_single_dep_atom(atom_raw))
        if alt_atoms:
            results.append(alt_atoms)

    return results


def _parse_single_dep_atom(atom_raw: str) -> DepAtom:
    """
    Parse ONE alternative within a dependency expression.

    Grammar we support (subset of Debian policy for binary Depends):
      <name> [ ":" <archqual> ] [ "(" <op> <ver> ")" ] [ "[" <archlist> "]" ]

    where:
      - <archqual> can be:
            "any"     -> means any arch satisfying multiarch rules.
                         We implement the project policy: keep only target arch + 'all'.
            "native"  -> means the native arch of the build.
                         For us, "native" == target arch we're resolving for.
            <literal arch> like "arm64", "amd64"
      - version constraint is like "(>= 1.2)" or "(= 3:1-2)" etc.
      - [archlist] is like "[arm64 amd64]". If present and target arch isn't in it,
        this dep does NOT apply at all.

    NOTE: Debian also allows profile qualifiers like "<!nocheck>" in Build-Depends.
    These do not generally appear in runtime Depends of binary packages.
    If they ever leak in, we will treat them as fatal unsupported syntax instead of
    guessing, because guessing could hide missing core runtime deps.

    We remove/consume:
      - one "(...)" block if present
      - one "[...]" block if present
      - and then parse "name[:archqual]" from the remainder.

    We raise on unexpected formats (defensive by design).
    """

    work = atom_raw.strip()

    # Extract any [archlist]
    arch_list: List[str] = []
    m_archlist = re.search(r'\[([^\]]+)\]', work)
    if m_archlist:
        arch_list_str = m_archlist.group(1)
        arch_list = [a.strip() for a in arch_list_str.split() if a.strip()]
        work = work[:m_archlist.start()] + work[m_archlist.end():]

    # Extract any (op ver)
    op = None
    ver = None
    m_ver = re.search(r'\(([^)]+)\)', work)
    if m_ver:
        inner = m_ver.group(1).strip()
        # Expect "<op> <ver>"
        # op can be >=, <=, =, >>, <<
        parts = inner.split(None, 1)
        if len(parts) != 2:
            raise RuntimeError(
                f"Cannot parse version constraint in dep atom {atom_raw!r}: "
                f"expected '(op version)'. Got {inner!r}"
            )
        op_candidate, ver_candidate = parts[0].strip(), parts[1].strip()
        if op_candidate not in (">=", "<=", "=", ">>", "<<"):
            raise RuntimeError(
                f"Unknown version operator {op_candidate!r} in dep atom {atom_raw!r}."
            )
        op = op_candidate
        ver = ver_candidate
        work = work[:m_ver.start()] + work[m_ver.end():]

    # Now what's left should be "name" or "name:qual"
    work = work.strip()
    if not work:
        raise RuntimeError(
            f"Dependency atom {atom_raw!r} lost its base name after parsing; "
            f"this should never happen."
        )

    if ':' in work:
        base_name, arch_qual = work.split(':', 1)
        base_name = base_name.strip()
        arch_qual = arch_qual.strip()
        if not base_name:
            raise RuntimeError(
                f"Bad dep atom {atom_raw!r}: empty package name before ':'"
            )
        if not arch_qual:
            raise RuntimeError(
                f"Bad dep atom {atom_raw!r}: empty arch qualifier after ':'"
            )
    else:
        base_name = work
        arch_qual = None

    # Sanity: package names are lowercase alnum + . + + + -
    if not re.match(r'^[a-z0-9][a-z0-9+.-]*$', base_name):
        raise RuntimeError(
            f"Suspicious package name {base_name!r} in dep atom {atom_raw!r}. "
            f"We refuse to continue because guessing wrong here is fatal."
        )

    # Normalize arch qualifier: translate 'native' into "target arch later".
    # We *do not* translate 'any' here yet; we interpret in resolver.
    return DepAtom(
        name=base_name,
        arch_qual=arch_qual,
        op=op,
        ver=ver,
        arch_list=arch_list,
    )


##############################################################################
# Version comparison helpers
##############################################################################

def _version_satisfies(candidate_version: str,
                       op: Optional[str],
                       needed_version: Optional[str]) -> bool:
    """
    Compare candidate_version against a constraint like (>= needed_version).

    We use debian.debian_support.Version to get Debian semantics
    (epoch:version-revision, lexical rules, etc.).
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
        # We already validated operator earlier, so reaching here means code drift.
        raise RuntimeError(
            f"Internal error: unknown version operator {op!r} at comparison time."
        )


def _provided_version_satisfies(
    provider_pkg: PackageRecord,
    virtual_name: str,
    op: Optional[str],
    needed_version: Optional[str],
) -> bool:
    """
    Given that 'provider_pkg' claims to Provide: virtual_name [ (= ver)? ],
    decide if that satisfies the version constraint (op, needed_version).

    Rules we enforce here:
      - If no version constraint was requested, it's fine.
      - If provider declared a Provides version for that virtual, compare that.
      - Else (no declared Provides version) and the dependency is versioned,
        we treat this as UNSATISFIABLE and raise a detailed exception instead of
        falling back to the provider's own package version. This matches dpkg/apt
        expectations that a versioned virtual must be satisfied by a provider that
        explicitly declares a version for that virtual.

    We intentionally DO NOT silently ignore version constraints: if we can't meet
    them under these rules, we raise with context to make it debuggable.
    """
    if op is None or needed_version is None:
        return True

    declared_ver = provider_pkg.provides_map.get(virtual_name)
    if declared_ver:
        return _version_satisfies(declared_ver, op, needed_version)

    # No declared Provides version for a versioned virtual: refuse and explain.
    raise RuntimeError(
        "Versioned virtual dependency cannot be satisfied: "
        f"requested '{virtual_name} {op} {needed_version}', but provider "
        f"'{provider_pkg.name} {provider_pkg.version} ({provider_pkg.arch})' "
        "does not declare a version for that virtual in its Provides field. "
        "Falling back to the provider's package version would be incorrect; "
        "this is blocked intentionally to avoid producing a download set that "
        "apt/dpkg would later reject."
    )


##############################################################################
# Resolver core
##############################################################################

def _candidate_arches_for_atom(atom: DepAtom, target_arch: str) -> Set[str]:
    """
    Decide which architectures are acceptable for satisfying this atom.

    Debian semantics (simplified for our offline use case):
     - Unqualified "pkg":
         Accept target_arch AND "all".
         Rationale: "Architecture: all" packages are arch-independent data/meta
         packages and should satisfy such deps. If we didn't include "all",
         we'd incorrectly fail on valid deps. We DO NOT consider foreign arches.
     - "pkg:any":
         We interpret as "same as unqualified for our target": {target_arch, all}.
         Real apt can consider *any* arch that is Multi-Arch: allowed/foreign,
         but we refuse to pull in random other arch because this tool's contract
         is: "download for THIS target arch".
     - "pkg:native":
         Means "the native arch of the build". For us, "native" == target_arch
         because we're building the offline set *for* that arch. We still
         allow "all" here as a convenience: an Architecture: all package can
         satisfy typical native tooling deps.
     - "pkg:<explicit-arch>":
         Only that arch. We DO NOT include "all" here, because "foo:arm64"
         means specifically the arm64 build, not a generic meta package.
    """
    q = atom.arch_qual
    if q is None:
        return {target_arch, "all"}
    if q == "any":
        return {target_arch, "all"}
    if q == "native":
        return {target_arch, "all"}
    # explicit architecture case:
    return {q}


def _arch_restriction_allows(atom: DepAtom, target_arch: str) -> bool:
    """
    Some dependency atoms are annotated with '[arch1 arch2]'.
    That means: "this dep only matters on these arches".

    If arch_list is empty, no restriction.
    If non-empty, we require target_arch to be in that list.
    """
    if not atom.arch_list:
        return True
    return target_arch in atom.arch_list


def _select_best_candidate(
    candidates: List[PackageRecord]
) -> PackageRecord:
    """
    Pick the "best" candidate from a list of PackageRecord.

    CURRENT POLICY:
      - We choose the newest Version according to Debian version ordering.
      - We ignore suite priority ordering or Priority: field for now.

    Rationale:
      This matches the original script's behavior ("take the newest version")
      and therefore does not regress existing behavior. It also keeps logic
      deterministic and easy to reason about. We document the risk:
        * This may pull from e.g. backports when a stable version also exists.
        * That can lead to newer deps that may not exist in the stable suite.
      But this risk already existed in the original tool, so we haven't
      worsened it.

    NOTE TO FUTURE CONTRIBUTORS:
      If you implement suite/component priority, do NOT silently change
      behavior. You MUST explain in code why the new priority order is safe,
      and you MUST raise explicit warnings when crossing suite boundaries.
    """
    if not candidates:
        raise RuntimeError(
            "Internal error: _select_best_candidate called with empty list."
        )

    # Sort descending by Version (newest first); keep stable tie-break by source_hint
    # so behavior across runs is deterministic if versions tie.
    sorted_cands = sorted(
        candidates,
        key=lambda pkg: (Version(pkg.version), pkg.source_hint),
        reverse=True
    )
    return sorted_cands[0]


def resolve_single_atom(
    atom: DepAtom,
    target_arch: str,
    pkgs_by_name: Dict[str, List[PackageRecord]],
    provides_index: Dict[str, List[PackageRecord]],
) -> Optional[PackageRecord]:
    """
    Resolve one DepAtom into a concrete PackageRecord OR return None if unsatisfiable.

    This does NOT recurse dependencies. This ONLY figures out:
        Which .deb would apt pick (under our policy constraints)
        to satisfy THIS atom?

    Resolution logic:

    1. If atom's arch restriction [ ... ] excludes target_arch, return None
       (meaning: for THIS arch, this dep doesn't apply at all).

    2. Determine acceptable arches for this atom:
         - see _candidate_arches_for_atom()

    3. Try direct matches in pkgs_by_name[atom.name] that have an allowed arch.
       Filter them by version constraint.

    4. If none found, try virtual resolution via provides_index[atom.name]:
       A virtual name can be satisfied by any provider that lists this name
       in its Provides:. We again filter by allowed arch and version constraint.
       We also check versioned-Provides:
         If the dep said "foo (>= 1.2)", and provider had "Provides: foo (=1.5)",
         that should count. If provider didn't specify a Provides version, we
         do NOT fall back to the provider's own Version; we raise instead.

       If we get MORE THAN ONE suitable provider, we still choose the newest
       (see _select_best_candidate). This is consistent with our "newest wins"
       policy and makes resolution deterministic. In apt, provider selection
       can also consider Priority and pinning; we store Priority so future
       contributors can refine tie-break, but we don't yet do that.

    5. Return the chosen PackageRecord or None.

    NOTE:
      If we return None from resolve_single_atom, the caller (resolve_dep_alternatives)
      will try the next alternative in the "A | B | C" group.
      If all alternatives fail, the whole dependency is unsatisfied and we raise.
    """

    # Step 1: arch restriction gating
    if not _arch_restriction_allows(atom, target_arch):
        # This dep does not apply on this arch at all.
        return None

    # Step 2: acceptable arches
    candidate_arches = _candidate_arches_for_atom(atom, target_arch)

    # Helper to check version constraint quickly
    def _keep_version(pkg: PackageRecord) -> bool:
        return _version_satisfies(pkg.version, atom.op, atom.ver)

    # Step 3: direct lookup
    direct_matches = []
    for pkg in pkgs_by_name.get(atom.name, []):
        if pkg.arch in candidate_arches and _keep_version(pkg):
            direct_matches.append(pkg)

    if direct_matches:
        return _select_best_candidate(direct_matches)

    # Step 4: via virtual Provides
    provider_matches = []
    for provider in provides_index.get(atom.name, []):
        if provider.arch not in candidate_arches:
            continue
        # _provided_version_satisfies may raise for versioned virtuals without declared version
        if not _provided_version_satisfies(provider, atom.name, atom.op, atom.ver):
            continue
        provider_matches.append(provider)

    if provider_matches:
        return _select_best_candidate(provider_matches)

    # Step 5: unsatisfied
    return None


def resolve_dep_alternatives(
    alt_group: List[DepAtom],
    target_arch: str,
    pkgs_by_name: Dict[str, List[PackageRecord]],
    provides_index: Dict[str, List[PackageRecord]],
) -> PackageRecord:
    """
    Resolve one comma-separated dependency group, which may include alternatives:
        "A | B | C"

    We try each alternative IN ORDER, like apt.
    For each alternative we call resolve_single_atom().

    If none are satisfiable (for this arch, or with this version),
    we raise immediately instead of guessing.

    This strictness is intentional:
      - If we guess, we might download the wrong provider, hiding real problems.
      - Failing fast with a detailed message makes it debuggable.

    Returns the chosen PackageRecord.
    """
    for atom in alt_group:
        pkg = resolve_single_atom(atom, target_arch, pkgs_by_name, provides_index)
        if pkg:
            return pkg

    # If we got here, we couldn't satisfy ANY alternative.
    # Emit a verbose error that includes the raw atoms for debugging.
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
        + " - The Packages index in ./repository is incomplete or mismatched to "
        + "   the actual APT sources for the target system.\n"
    )


def resolve_full_dependency_set_for(pkg: PackageRecord) -> List[List[DepAtom]]:
    """
    For a given PackageRecord (the concrete .deb we chose),
    return a parsed list of dependencies that we must ALSO pull in.

    We include BOTH Depends and Pre-Depends because for "download everything
    needed to install offline", Pre-Depends is not optional: dpkg will refuse
    to configure a package if its Pre-Depends aren't unpacked first.

    We IGNORE Recommends/Suggests/etc. on purpose because the original script
    also ignored them, and changing that here would balloon the download set
    and break expectations for this tool's role (core dependency closure,
    not full userland environment).
    """
    combined_strs: List[str] = []
    if pkg.pre_depends_raw:
        combined_strs.append(pkg.pre_depends_raw)
    if pkg.depends_raw:
        combined_strs.append(pkg.depends_raw)

    if not combined_strs:
        return []

    joined = ", ".join(s for s in combined_strs if s.strip())
    return parse_dep_field(joined)


##############################################################################
# Download + recursion logic
##############################################################################

def _download_package_file(
    pkg: PackageRecord,
    base_urls: List[str],
) -> str:
    """
    Download the .deb for `pkg` into DOWNLOAD_DIR using any of the provided
    base_urls.

    The `Filename` field in Packages is a relative path like:
        pool/main/o/orc/liborc-0.4-dev-bin_0.4.33-2_arm64.deb

    Historically, this script:
      - picked one "base URL" first,
      - tried to download,
      - if 404, tried fallback base URLs.
    We keep that logic to avoid breaking existing behavior.

    We raise RuntimeError if no base URL works. We DO NOT silently skip,
    because skipping would hide missing pieces in the offline bundle.

    Returns the absolute local file path we wrote to disk.
    """
    file_name = os.path.basename(pkg.filename)
    file_path = os.path.join(DOWNLOAD_DIR, file_name)

    # If already exists, we don't redownload. This preserves behavior and is also
    # important for performance, since recursive resolution may encounter the
    # same package multiple times.
    if os.path.isfile(file_path):
        print(f"Exists: {file_name} is already downloaded.")
        return file_path

    # We'll try each base URL in order. This is intentionally similar to the
    # previous script's fallback.
    relative_path = pkg.filename.lstrip('/')

    downloaded = False
    for base in base_urls:
        try_url = f"{base.rstrip('/')}/{relative_path}"
        print(f"Downloading: {file_name} from {try_url}")
        try:
            response = requests.get(try_url)
            response.raise_for_status()
            with open(file_path, 'wb') as outf:
                outf.write(response.content)
            downloaded = True
            break
        except requests.exceptions.RequestException as e:
            print(f"Failed to fetch from {try_url}: {e}")
            continue

    if not downloaded:
        raise RuntimeError(
            f"Error fetching {file_name}: not found in ANY base URL.\n"
            f"Looked for relative path '{relative_path}' in:\n"
            + "\n".join(base_urls) + "\n"
            "This likely means your --base-url list does not match the actual "
            "Origins (hosts/suites) of the packages in ./repository.\n"
        )

    return file_path


def _process_package_and_get_deps(
    pkg: PackageRecord,
    base_urls: List[str],
) -> List[List[DepAtom]]:
    """
    Ensure the .deb for `pkg` is downloaded locally, and return its parsed
    dependency groups (list of alt-groups of DepAtom).

    Historically, we opened the downloaded .deb to read Depends. We *still do that*
    as a correctness check, even though we already parsed Depends/Pre-Depends from
    the Packages index.

    Why double-source? Because:
     - If the local .deb we're forced to download is actually a slightly different
       build than the stanza we matched (mirrors can drift), then reading the control
       data inside the .deb gives us the real truth for recursion.

    If the .deb can't be read or is corrupt, we raise immediately with context,
    rather than continuing and producing a half-correct dep closure.

    Return value matches parse_dep_field().
    """
    file_path = _download_package_file(pkg, base_urls)

    try:
        deb = DebFile(file_path)
        depends_field = deb.debcontrol().get('Depends', '')
        pre_depends_field = deb.debcontrol().get('Pre-Depends', '')
    except Exception as e:
        raise RuntimeError(
            f"Error processing deb file {os.path.basename(file_path)} "
            f"for package {pkg.name} {pkg.version} ({pkg.arch}): {e}"
        )

    merged = ", ".join(x for x in (pre_depends_field, depends_field) if x)
    if not merged.strip():
        return []
    return parse_dep_field(merged)


def fetch_dependencies_recursive(
    initial_pkg_name: str,
    target_arch: str,
    pkgs_by_name: Dict[str, List[PackageRecord]],
    provides_index: Dict[str, List[PackageRecord]],
    base_urls: List[str],
    visited_pkgkeys: Set[Tuple[str, str, str]],
):
    """
    Core recursive resolver & downloader.

    Steps:
      1. Resolve the initial name as if it were a single-alternative dep.
         (i.e. treat "ffmpeg" the same way we'd treat a dependency atom "ffmpeg")

      2. Download that .deb, read its dependencies, and recurse into each dep group.
         Each group may contain alternatives A | B | C. We must choose ONE.

    We guard against infinite recursion using ONE set:
      - visited_pkgkeys: {(name, version, arch)} prevents rewalking a package
        even if referenced again via different virtual names.

    NOTE: We do NOT silently skip unresolved deps. If resolution fails for ANY dep,
    we raise immediately with a message that names that dep. This is intentional:
    guessing or skipping would produce a broken offline install set that LOOKS "done"
    but won't actually install cleanly.

    NOTE: We intentionally do NOT handle Suggests/Recommends/etc.
    """

    # Step 1: treat initial_pkg_name as a single DepAtom and resolve
    fake_atom = _parse_single_dep_atom(initial_pkg_name.strip())
    top_pkg = resolve_single_atom(fake_atom, target_arch, pkgs_by_name, provides_index)
    if not top_pkg:
        raise RuntimeError(
            f"Top-level package '{initial_pkg_name}' could not be resolved for arch "
            f"{target_arch}. This means:\n"
            f" - The package isn't in ./repository for this arch,\n"
            f" - or it only exists as a virtual with no valid provider,\n"
            f" - or only exists for a different arch than {target_arch}.\n"
        )

    # Walk a stack manually (DFS)
    stack: List[PackageRecord] = [top_pkg]

    while stack:
        pkg = stack.pop()

        pkg_key = (pkg.name, pkg.version, pkg.arch)
        if pkg_key in visited_pkgkeys:
            continue
        visited_pkgkeys.add(pkg_key)

        # Step 2: download + get dependencies (from actual .deb for correctness)
        dep_groups = _process_package_and_get_deps(pkg, base_urls)

        # Step 3: resolve each dependency group and push onto stack
        for alt_group in dep_groups:
            chosen = resolve_dep_alternatives(
                alt_group,
                target_arch,
                pkgs_by_name,
                provides_index,
            )
            stack.append(chosen)

    # If we reach here with no uncaught exceptions, we're done for this root.
    print(f"Info: Finished resolving '{initial_pkg_name}' and its dependency tree.")


##############################################################################
# Main CLI entry point
##############################################################################

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Fetch dependencies for given Debian packages.")
    parser.add_argument(
        '--base-url',
        type=str,
        default='https://archive.ubuntu.com/ubuntu',
        help=(
            'Comma-separated base URLs to the archives where deb packages are downloaded from.\n'
            'IMPORTANT:\n'
            '  - These MUST correspond to the same origins/suites that you ran update_repository.py against.\n'
            '  - We will try each base URL in order for every .deb.\n'
            '  - If none of them work for a given package, we raise a hard error.\n'
            'This preserves legacy behavior, but now with much stricter dependency resolution.'
        )
    )
    parser.add_argument('--packages', nargs='+', help='List of Debian packages to fetch dependencies for.')
    args = parser.parse_args()

    # Build metadata index from ./repository first.
    # This replaces the old "load_all_urls" logic, and it is REQUIRED for correctness.
    try:
        pkgs_by_name, provides_index, target_arch = build_package_index(REPO_DIR)
    except (FileNotFoundError, Exception) as e:
        raise RuntimeError(f"\nCRITICAL ERROR while indexing repository: {e}")

    base_urls = [u.strip() for u in args.base_url.split(',') if u.strip()]
    if not base_urls:
        raise RuntimeError(
            "No valid --base-url values provided. At least one base URL is required "
            "to actually download .deb files."
        )

    if not args.packages:
        raise RuntimeError(
            "No packages specified. Use --packages <pkg1> <pkg2> ..."
        )

    visited_pkgkeys: Set[Tuple[str, str, str]] = set()

    # For each requested top-level package, resolve + download full transitive deps.
    for package_name in args.packages:
        try:
            fetch_dependencies_recursive(
                package_name,
                target_arch,
                pkgs_by_name,
                provides_index,
                base_urls,
                visited_pkgkeys,
            )
        except Exception as e:
            raise RuntimeError(
                f"\nCRITICAL ERROR while resolving '{package_name}': {e}"
            )

    print("\nAll dependencies processed.")
