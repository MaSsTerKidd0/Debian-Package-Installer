"""
Load target-OS presets from an editable JSON file (presets.json).

The GUI's OS dropdown is data-driven so you can add or tweak targets without
touching Python: edit presets.json and relaunch. Each preset is:

    "<display name>": {
        "platform": "binary-<arch>",
        "index_sources": [
            {"base_url": "<.../dists>", "suites": [...], "components": [...]},
            ... one entry per origin (e.g. Debian + Debian-security + RPi) ...
        ],
        "download_base_urls": ["<archive root>", ...]
    }

Finding the right suites: browse "<base_url>/" in a browser (e.g.
http://archive.ubuntu.com/ubuntu/dists/) -- every directory there is a suite.
A suite is the lowercase adjective of the release codename (Noble Numbat ->
"noble", Resolute Raccoon -> "resolute"), with -updates/-security/-backports
pockets. Skip -proposed (that's a testing pocket).

presets.json is the source of truth. If it's missing we recreate it from the
built-in DEFAULT_PRESETS below so the app always starts; if it's present but
malformed we raise, so a bad edit is reported rather than silently ignored.
"""

import json
import os
from typing import Dict


# Seed used to (re)create presets.json when it doesn't exist. Keep in sync with
# the shipped presets.json -- this is only the emergency/first-run fallback.
DEFAULT_PRESETS: Dict[str, dict] = {
    "Ubuntu 24.04 LTS Noble (amd64)": {
        "platform": "binary-amd64",
        "index_sources": [
            {
                "base_url": "https://archive.ubuntu.com/ubuntu/dists",
                "suites": ["noble", "noble-updates", "noble-security", "noble-backports"],
                "components": ["main", "restricted", "universe", "multiverse"],
            }
        ],
        "download_base_urls": ["https://archive.ubuntu.com/ubuntu"],
    },
    "Ubuntu 26.04 LTS Resolute (amd64)": {
        "platform": "binary-amd64",
        "index_sources": [
            {
                "base_url": "https://archive.ubuntu.com/ubuntu/dists",
                "suites": ["resolute", "resolute-updates", "resolute-security", "resolute-backports"],
                "components": ["main", "restricted", "universe", "multiverse"],
            }
        ],
        "download_base_urls": ["https://archive.ubuntu.com/ubuntu"],
    },
    "Debian 12 Bookworm (arm64)": {
        "platform": "binary-arm64",
        "index_sources": [
            {
                "base_url": "http://deb.debian.org/debian/dists",
                "suites": ["bookworm", "bookworm-updates"],
                "components": ["main", "contrib", "non-free", "non-free-firmware"],
            },
            {
                "base_url": "http://deb.debian.org/debian-security/dists",
                "suites": ["bookworm-security"],
                "components": ["main", "contrib", "non-free", "non-free-firmware"],
            },
        ],
        "download_base_urls": [
            "http://deb.debian.org/debian",
            "http://deb.debian.org/debian-security",
        ],
    },
    "Raspberry Pi OS Legacy Bookworm 64-bit (arm64)": {
        "platform": "binary-arm64",
        "index_sources": [
            {
                "base_url": "http://deb.debian.org/debian/dists",
                "suites": ["bookworm", "bookworm-updates"],
                "components": ["main", "contrib", "non-free", "non-free-firmware"],
            },
            {
                "base_url": "http://deb.debian.org/debian-security/dists",
                "suites": ["bookworm-security"],
                "components": ["main", "contrib", "non-free", "non-free-firmware"],
            },
            {
                "base_url": "http://archive.raspberrypi.com/debian/dists",
                "suites": ["bookworm"],
                "components": ["main"],
            },
        ],
        "download_base_urls": [
            "http://archive.raspberrypi.com/debian",
            "http://deb.debian.org/debian",
            "http://deb.debian.org/debian-security",
        ],
    },
}


def presets_path() -> str:
    """presets.json lives at the project root (one level up from this package)."""
    return os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "presets.json"
    )


def save_presets(presets: Dict[str, dict], path: str = None) -> None:
    path = path or presets_path()
    with open(path, "w", encoding="utf-8") as f:
        json.dump(presets, f, indent=2)
        f.write("\n")


def _validate(presets: Dict[str, dict]) -> None:
    """Fail loud on the mistakes a hand-editor is likely to make."""
    if not isinstance(presets, dict) or not presets:
        raise ValueError("presets file must be a non-empty JSON object of {name: preset}.")

    for name, p in presets.items():
        where = f"preset {name!r}"
        if not isinstance(p, dict):
            raise ValueError(f"{where}: must be an object.")
        if not isinstance(p.get("platform"), str) or not p["platform"]:
            raise ValueError(f"{where}: missing string 'platform' (e.g. 'binary-amd64').")
        srcs = p.get("index_sources")
        if not isinstance(srcs, list) or not srcs:
            raise ValueError(f"{where}: 'index_sources' must be a non-empty list.")
        for i, src in enumerate(srcs):
            sw = f"{where}, index_sources[{i}]"
            if not isinstance(src, dict):
                raise ValueError(f"{sw}: must be an object.")
            for key in ("base_url",):
                if not isinstance(src.get(key), str) or not src[key]:
                    raise ValueError(f"{sw}: missing string '{key}'.")
            for key in ("suites", "components"):
                val = src.get(key)
                if not isinstance(val, list) or not val or not all(isinstance(x, str) for x in val):
                    raise ValueError(f"{sw}: '{key}' must be a non-empty list of strings.")
        dls = p.get("download_base_urls")
        if not isinstance(dls, list) or not dls or not all(isinstance(x, str) for x in dls):
            raise ValueError(f"{where}: 'download_base_urls' must be a non-empty list of strings.")


def load_presets(path: str = None) -> Dict[str, dict]:
    """
    Return the presets dict. Creates presets.json from DEFAULT_PRESETS if it's
    absent; raises ValueError with a clear message if it exists but is invalid.
    """
    path = path or presets_path()

    if not os.path.exists(path):
        save_presets(DEFAULT_PRESETS, path)
        return dict(DEFAULT_PRESETS)

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        raise ValueError(f"presets.json is not valid JSON ({path}): {e}")

    _validate(data)
    return data
