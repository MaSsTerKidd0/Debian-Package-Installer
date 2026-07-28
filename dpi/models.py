"""Data structures for one binary package and one dependency atom."""

from typing import Dict, List, Optional


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
     - multi_arch: Multi-Arch field string (may influence :any semantics in real
       APT; we don't duplicate full APT legality checks, but we keep it visible
       because future contributors will need it to be correct for edge cases.)
     - priority: Priority string ("required", "important", ...). We may someday
       prefer higher-priority providers for ambiguous virtual packages; we store
       it now because picking a stable provider for virtuals is otherwise
       ambiguous.
     - source_hint: (origin/component/platform) breadcrumb inferred from the
       repository source .txt filename, so we can explain where we found a
       package when debugging or choosing between same-named packages.
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


class DepAtom:
    """
    Represents a single atomic dependency requirement like:
        "perl:any (>= 5.36) [arm64]"
        "liborc-0.4-dev-bin:any (= 1:0.4.33-2)"
        "dbus-session-bus"
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
