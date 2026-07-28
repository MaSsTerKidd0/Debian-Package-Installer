"""
Load "starter kit" essential-package sets from an editable JSON file
(starter_kits.json), keyed by machine type.

When the GUI's "Include essential packages" box is ticked, the selected machine
type's package list is merged into what you asked to download -- so a fresh
target comes up with Wi-Fi, firmware, audio, SSD tools, etc. already present.

    "<machine type>": {
        "description": "<shown in the UI / README>",
        "packages": ["network-manager", "wpasupplicant", ...]
    }

IMPORTANT: package names differ across distros (Ubuntu ships one big
`linux-firmware`; Debian splits it into `firmware-*` under non-free-firmware).
Each kit lists both families on purpose and relies on the resolver's keep-going
mode to skip the names that don't exist on the chosen target. So use these kits
with keep-going enabled (the GUI default).

starter_kits.json is the source of truth; if it's missing we recreate it from
DEFAULT_STARTER_KITS, and if it's present but malformed we raise a clear error.
"""

import json
import os
from typing import Dict, List


# Seed used to (re)create starter_kits.json when absent. Keep in sync with the
# shipped starter_kits.json -- this is only the emergency/first-run fallback.
DEFAULT_STARTER_KITS: Dict[str, dict] = {
    "PC / Laptop": {
        "description": "Wi-Fi + wired networking, common firmware, open-source GPU "
                       "acceleration, audio, Bluetooth, and SSD tools.",
        "packages": [
            "network-manager", "wpasupplicant", "iw", "ethtool", "rfkill",
            "linux-firmware", "firmware-iwlwifi", "firmware-realtek",
            "firmware-atheros", "firmware-misc-nonfree",
            "libgl1-mesa-dri", "mesa-utils", "mesa-vulkan-drivers",
            "pipewire", "pipewire-pulse", "wireplumber", "alsa-utils",
            "bluez",
            "nvme-cli", "smartmontools", "hdparm",
        ],
    },
    "Tablet / Touch": {
        "description": "PC networking/firmware/audio plus touchscreen input, an "
                       "on-screen keyboard, and auto-rotate sensors.",
        "packages": [
            "network-manager", "wpasupplicant", "iw", "rfkill",
            "linux-firmware", "firmware-iwlwifi", "firmware-realtek",
            "xserver-xorg-input-libinput", "libinput-tools",
            "onboard", "iio-sensor-proxy",
            "libgl1-mesa-dri", "mesa-utils",
            "pipewire", "pipewire-pulse", "wireplumber", "alsa-utils",
            "bluez",
            "nvme-cli", "smartmontools",
        ],
    },
    "Server / Headless": {
        "description": "No GUI. SSH, wired networking, storage/SSD health, a "
                       "firewall, and basic ops tools.",
        "packages": [
            "openssh-server", "ifupdown", "ethtool",
            "nvme-cli", "smartmontools", "hdparm",
            "ufw", "htop", "curl", "ca-certificates", "rsync",
        ],
    },
    "Raspberry Pi": {
        "description": "Pi Wi-Fi/Bluetooth firmware and networking, plus SSD/NVMe "
                       "tools for a HAT or USB drive.",
        "packages": [
            "network-manager", "wpasupplicant",
            "firmware-brcm80211", "bluez",
            "nvme-cli", "smartmontools",
        ],
    },
}


def starter_kits_path() -> str:
    """starter_kits.json lives at the project root (one level up from here)."""
    return os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "starter_kits.json"
    )


def save_starter_kits(kits: Dict[str, dict], path: str = None) -> None:
    path = path or starter_kits_path()
    with open(path, "w", encoding="utf-8") as f:
        json.dump(kits, f, indent=2)
        f.write("\n")


def _validate(kits: Dict[str, dict]) -> None:
    if not isinstance(kits, dict) or not kits:
        raise ValueError("starter kits file must be a non-empty JSON object of {name: kit}.")
    for name, kit in kits.items():
        where = f"starter kit {name!r}"
        if not isinstance(kit, dict):
            raise ValueError(f"{where}: must be an object.")
        pkgs = kit.get("packages")
        if not isinstance(pkgs, list) or not pkgs or not all(isinstance(x, str) and x for x in pkgs):
            raise ValueError(f"{where}: 'packages' must be a non-empty list of package-name strings.")
        if "description" in kit and not isinstance(kit["description"], str):
            raise ValueError(f"{where}: 'description' must be a string if present.")


def load_starter_kits(path: str = None) -> Dict[str, dict]:
    """
    Return the starter-kits dict. Creates starter_kits.json from
    DEFAULT_STARTER_KITS if absent; raises ValueError if it exists but is invalid.
    """
    path = path or starter_kits_path()

    if not os.path.exists(path):
        save_starter_kits(DEFAULT_STARTER_KITS, path)
        return dict(DEFAULT_STARTER_KITS)

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        raise ValueError(f"starter_kits.json is not valid JSON ({path}): {e}")

    _validate(data)
    return data


def _norm(s: str) -> str:
    """Lowercase and strip non-alphanumerics, so 'Raspberry Pi' == 'raspberrypi'."""
    return "".join(ch for ch in s.lower() if ch.isalnum())


def match_machine(query: str, kits: Dict[str, dict]) -> str:
    """
    Resolve a user-typed machine name to an exact starter-kit key, forgivingly:
    exact (case-insensitive) first, then normalized equality, then a unique
    substring match. So 'pi', 'raspberry', or 'Raspberry Pi' all find the Pi kit.

    Raises ValueError if nothing matches, or if a substring is ambiguous.
    """
    q = query.strip()
    for name in kits:
        if name.lower() == q.lower():
            return name

    nq = _norm(q)
    exacts = [name for name in kits if _norm(name) == nq]
    if len(exacts) == 1:
        return exacts[0]

    subs = [name for name in kits if nq and nq in _norm(name)]
    if len(subs) == 1:
        return subs[0]
    if len(subs) > 1:
        raise ValueError(f"machine {query!r} is ambiguous; matches: {subs}. Be more specific.")

    raise ValueError(
        f"machine {query!r} not found. Available: {list(kits)}."
    )


def merge_packages(user_packages: List[str], kit_packages: List[str]) -> List[str]:
    """
    Combine the user's requested packages with a kit's packages, preserving order
    (user first, then kit extras) and dropping duplicates. Case-sensitive, since
    Debian package names are lowercase by policy.
    """
    seen = set()
    merged: List[str] = []
    for name in list(user_packages) + list(kit_packages):
        if name not in seen:
            seen.add(name)
            merged.append(name)
    return merged
