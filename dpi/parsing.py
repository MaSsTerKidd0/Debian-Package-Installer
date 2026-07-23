"""
Parsers that turn raw text into structured data:
  - Debian Packages-index stanzas (deb822)
  - the Provides field
  - the target architecture encoded in a repository filename
  - Depends / Pre-Depends fields into DepAtom groups

Everything here is strict by design: a silent mis-parse would make the resolver
select nonsense packages, which is worse than failing loudly.
"""

import os
import re
from typing import Dict, List, Optional

from .models import DepAtom


def parse_stanzas_from_file(path: str) -> List[Dict[str, str]]:
    """
    Read one repository/*.txt file (a decompressed Packages.gz).

    Debian Packages files are RFC822-style stanzas separated by blank lines,
    where fields continue on lines beginning with a space or tab. We return a
    list of dicts (field_name -> full value).

    If parsing fails, that is fatal: every resolver step depends on this data.
    """
    stanzas: List[Dict[str, str]] = []
    current: Dict[str, str] = {}

    try:
        # Decode strictly; silent replacement would hide corrupted or
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
                    val = val.lstrip()
                    current[key] = val
            # Flush last stanza if the file didn't end with a blank line
            if current:
                stanzas.append(current)
    except FileNotFoundError:
        raise RuntimeError(f"Repository file not found while parsing: {path}")
    except UnicodeDecodeError as e:
        raise RuntimeError(f"Error parsing repository file {path}: UTF-8 decoding failed: {e}")
    except Exception as e:
        raise RuntimeError(f"Error parsing repository file {path}: {e}")

    return stanzas


def parse_provides_field(provides_val: str) -> Dict[str, Optional[str]]:
    """
    Parse a Provides field like "foo (= 1.2), bar, baz (= 3)" into
    { "foo": "1.2", "bar": None, "baz": "3" }.
    """
    result: Dict[str, Optional[str]] = {}
    if not provides_val:
        return result

    parts = [p.strip() for p in provides_val.split(',') if p.strip()]
    for p in parts:
        m = re.match(r'^([a-z0-9][a-z0-9+.-]*)(?:\s*\(=\s*([^)]+)\))?$', p)
        if not m:
            # Refuse to silently swallow unexpected syntax: wrong here means a
            # "valid" but broken download set.
            raise RuntimeError(
                f"Unexpected Provides token syntax: {p!r}. "
                f"This parser assumes 'name' or 'name (= version)'."
            )
        virt_name = m.group(1)
        virt_ver = m.group(2) if m.group(2) else None
        result[virt_name] = virt_ver
    return result


def extract_target_arch_from_repo_filename(filename: str) -> str:
    """
    Repository files are named {host}-{suite}-{component}-{platform}.txt where
    {platform} is 'binary-<arch>' (e.g. 'binary-arm64'). The host itself may
    contain '-' (e.g. 'deb-debian-org'), so detect a '-binary-<arch>' tail
    rather than splitting naively.
    """
    base = os.path.basename(filename)
    if not base.endswith('.txt'):
        raise RuntimeError(f"Internal error: expected .txt repo file, got {filename!r}")
    stem = base[:-4]

    m = re.search(r'(?:^|-)binary-([a-z0-9][a-z0-9+.-]*)$', stem)
    if not m:
        raise RuntimeError(
            f"Cannot parse target arch from repository file name {filename!r}: "
            f"expected the pattern '*-binary-<arch>.txt'."
        )
    return m.group(1)


def parse_dep_field(dep_field_val: str) -> List[List[DepAtom]]:
    """
    Parse a Depends or Pre-Depends field into a list-of-lists:

        [
          [DepAtom, DepAtom, ...],  # first alternative group "A | B | C"
          [DepAtom],                # next dep in the comma list
          ...
        ]

    Strict by design: a silent mis-parse can select nonsense packages.
    """
    if not dep_field_val:
        return []

    results: List[List[DepAtom]] = []

    # Split top-level by commas = logical AND
    comma_groups = [grp.strip() for grp in dep_field_val.split(',') if grp.strip()]
    for comma_grp in comma_groups:
        # Split alternatives by '|', preserving order.
        alts_raw = [alt.strip() for alt in comma_grp.split('|') if alt.strip()]
        alt_atoms: List[DepAtom] = []
        for atom_raw in alts_raw:
            alt_atoms.append(parse_single_dep_atom(atom_raw))
        if alt_atoms:
            results.append(alt_atoms)

    return results


def parse_single_dep_atom(atom_raw: str) -> DepAtom:
    """
    Parse ONE alternative within a dependency expression.

    Grammar (subset of Debian policy for binary Depends):
      <name> [ ":" <archqual> ] [ "(" <op> <ver> ")" ] [ "[" <archlist> "]" ]

    We consume one "(...)" and one "[...]" block if present, then parse
    "name[:archqual]" from the remainder. We raise on unexpected formats:
    guessing could hide missing core runtime deps.
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

    # What's left should be "name" or "name:qual"
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
            raise RuntimeError(f"Bad dep atom {atom_raw!r}: empty package name before ':'")
        if not arch_qual:
            raise RuntimeError(f"Bad dep atom {atom_raw!r}: empty arch qualifier after ':'")
    else:
        base_name = work
        arch_qual = None

    # Package names are lowercase alnum + . + + + -
    if not re.match(r'^[a-z0-9][a-z0-9+.-]*$', base_name):
        raise RuntimeError(
            f"Suspicious package name {base_name!r} in dep atom {atom_raw!r}. "
            f"We refuse to continue because guessing wrong here is fatal."
        )

    return DepAtom(
        name=base_name,
        arch_qual=arch_qual,
        op=op,
        ver=ver,
        arch_list=arch_list,
    )
