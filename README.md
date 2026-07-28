# Debian Package Installer

This project provides a set of Python tools to download Debian packages and their dependencies from the official Ubuntu repositories.

## Features

-   **Automated Repository Updates**: A script to download and process the latest package lists.
-   **Recursive Dependency Resolution**: Fetches a package and then recursively fetches all its dependencies.
-   **Local Caching**: Saves downloaded `.deb` files locally to avoid re-downloading.
-   **Configurable**: Easily change which Ubuntu suites and components to source packages from.

## Compare to Alternatives

- **apt-offline**- only downloads the missing packages, not very robust indeed.

- **apt-rdepends**- will cause catastrophic failure when reaching a package name alias.

- **This Project**- works even when running on a different Linux version and/or architecture than the one we're downloading for, and this project downloads **all** dependencies, regardless of whether you (or the target machine) happen to have them installed.

## Requirements

-   Python 3.x
-   `python-debian` library
-   `requests` library

You can install the required Python libraries using pip:

```bash
python3 -m pip install python-debian requests
```

### Platform support

Runs on **Windows, Linux, and macOS** — the scripts are pure Python (HTTP + file
writes) and never invoke `dpkg`/`apt`. Dependency resolution reads everything it
needs from the package index, exactly as `apt` does, so no Linux userland is
required to assemble a bundle.

There is one optional, POSIX-only feature: `--verify-deb-metadata` (see Step 2)
opens each downloaded `.deb` to cross-check it against the index. Modern `.deb`
files use zstd compression, and `python-debian` can only decompress that on
POSIX platforms. Leave the flag off on Windows — it is off by default, and
resolution is unaffected.

## Quick start (GUI)

If you'd rather not touch the command line, run the graphical version:

```sh
python3 gui.py
```

Then: pick your **Target OS** (presets for Ubuntu 24.04, Debian 12 Bookworm, and
Raspberry Pi OS Legacy Bookworm 64-bit), type or **load a list** of package names,
choose where to save the `.tar.gz`, and click **Build Offline Bundle**. A progress
bar and live log show what's happening. The result is a single self-installing
archive — see [Offline bundles](#offline-bundles) below.

The **"Skip packages that can't be resolved"** checkbox (on by default) warns and
continues past names that aren't in the sources — handy for typos or custom/vendor
`.deb`s — and lists what it skipped at the end. Untick it to stop on the first
failure instead.

The GUI needs nothing beyond the standard library (it uses Tkinter, which ships
with Python) plus the two libraries in Requirements.

## Usage (command line)

The command-line process is two steps: first update your local repository index, then download the desired package(s).

**Step 1:** Update the Repository Index

Before you can download packages, you need to create a local index of available packages. Run the `update_repository.py` script:

```sh
python3 update_repository.py
```

This will create a ./repository directory, download the Packages.gz files from the Ubuntu archives, extract them, and save them as .txt files. This step can take a few minutes as it downloads data for multiple Ubuntu suites.

You can customize the sources with command-line arguments:
 * --base-url: The `dists` folder that contains all releases (for example `https://us.archive.ubuntu.com/ubuntu/dists`). Must include the scheme.
 * --suites: Change the Ubuntu releases (e.g., noble, jammy).
 * --components: Change the repository sections (e.g., main, universe).
 * --platform: Change the architecture (e.g., binary-amd64).
For example (all arguments here are optional, with default values good for newest version of Ubuntu 24.04):
```sh
python3 update_repository.py --base-url https://us.archive.ubuntu.com/ubuntu/dists --suites jammy noble noble-updates noble-security noble-backports --components main restricted universe multiverse
```

**What if my target is not the newest version of Ubuntu 24.04?**:

You will have to match the CLI arguments to `update_repository.py` for your specific `apt sources` file.
```sh
user@ubuntu:/etc/apt/sources.list.d$ cat ubuntu.sources
Types: deb
URIs: http://us.archive.ubuntu.com/ubuntu/
Suites: jammy noble noble-updates noble-security noble-backports
Components: main restricted universe multiverse
Signed-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg
user@ubuntu:/etc/apt/sources.list.d$ 
```


**Step 2:** Download a Package and Its Dependencies

Once the repository index is created, use debian-package-installer.py to download a package and all its dependencies.\
For example (`--base-url` argument is optional, with default value good for newest version of Ubuntu 24.04):
```sh
python3 debian-package-installer.py --base-url https://archive.ubuntu.com/ubuntu --packages <package_name1> <package_name2> ...
```

**Example:** To download ffmpeg and all packages it depends on:
```sh
python3 debian-package-installer.py --packages ffmpeg
```

Optional flags:
 * `--bundle OUTPUT.tar.gz` — after downloading, package everything into a
   self-installing archive (see [Offline bundles](#offline-bundles)).
 * `--keep-going` — don't abort when a requested package can't be resolved (a
   typo, or a custom/vendor `.deb` that was never in the Debian sources). Instead,
   print a warning, skip that package, carry on with the rest, and show a summary
   of what was skipped. By default the run stops on the first failure. A requested
   package is skipped in full — if part of its dependency tree is unresolvable, the
   whole package is left out rather than shipping a tree that won't install.
 * `--repo-dir` / `--download-dir` — override the default `./repository` and
   `./downloaded` locations.
 * `--verify-deb-metadata` — after each download, open the `.deb` and confirm its
   control data matches the index, failing on any mismatch (catches mirror drift).
   **POSIX-only** because of zstd decompression; leave it off on Windows.

Two constraints worth knowing before you hit them as errors:
 * **`--base-url` must cover every origin you indexed in Step 1.** The index records each package's path within an archive, not which host it came from, so the downloader tries each base URL in turn for every `.deb`. If you indexed three origins (as in the Raspberry Pi example below), pass all three as a comma-separated list.
 * **`./repository` must hold exactly one architecture.** The resolver detects the target arch from the index filenames and refuses to run on a mixed set. Use `rm -rf ./repository` before indexing for a different arch.

The script will read the index files from the [./repository/](./repository/) directory, resolve the entire dependency tree, and download all required .deb files into the [./downloaded/](./downloaded/) directory.

## Offline bundles

Passing `--bundle` (CLI) or clicking **Build Offline Bundle** (GUI) packs the
downloaded `.deb` files into a single `.tar.gz` with a generated installer:

```
offline-bundle/
  install.sh      # chmod +x and run; installs everything, no network needed
  README.txt      # short instructions
  debs/
    *.deb
```

On the target machine (Raspberry Pi, offline server, etc.):

```sh
tar xzf offline-bundle.tar.gz
cd offline-bundle/
chmod +x install.sh
sudo ./install.sh
```

`install.sh` finds every `.deb` under the folder (across any subfolders), hands
the whole set to `dpkg -i` at once so dpkg can resolve install ordering, then
runs `dpkg --configure -a` to finish. It re-runs itself with `sudo` if you forget,
and warns if the target's architecture differs from what the bundle was built for.

## Project layout

 * [gui.py](./gui.py): Tkinter GUI front end.
 * [update_repository.py](./update_repository.py): CLI to download and prepare package lists.
 * [debian-package-installer.py](debian-package-installer.py): CLI to download a package, its dependencies, and optionally build a bundle.
 * [dpi/](./dpi/): the library the entry points share —
   `parsing` and `index` (read the Packages files), `resolver` (apt-like dependency
   resolution), `downloader` (fetch + walk the graph), `repository` (fetch the
   indexes), `packager` (build the `.tar.gz`), and `reporter` (log/progress sink).
 * [./repository/](./repository/): index `.txt` files created by update_repository.py.
 * [./downloaded/](./downloaded/): default output directory for downloaded `.deb` packages.


**Raspberry Pi OS Example**

This repo is even more important for Raspberry Pi, because now it means that you don't need a physical online Raspberry Pi to download repos for a Raspberry Pi.

Example system info:
```txt
Raspberry Pi System Overview:

- Device: Raspberry Pi
- OS: Raspberry Pi OS (Bookworm, based on Debian 12)
- Edition: Desktop (Standard)
  - Confirmation:
    - raspberrypi-ui-mods package is installed
    - LibreOffice is not installed (Full edition apps are absent)
- Desktop Environment: Labwc (Wayland-based, using wlroots backend)
- Kernel: Linux 6.12.25-rpt-rpi-2712 #1 SPM PREEMPT Debian 1:6.12.25-1+rpt1 (2025-04-30)
- Architecture: ARM 64-bit (aarch64)
- GUI: Installed and active (not Lite)
- Base Distribution: Debian (as indicated by HOME_URL="https://www.debian.org/")
- Hostname: raspberrypi
- APT Repositories:
  - deb http://deb.debian.org/debian bookworm main contrib non-free non-free-firmware
  - deb http://deb.debian.org/debian-security/ bookworm-security main contrib non-free non-free-firmware
  - deb http://deb.debian.org/debian bookworm-updates main contrib non-free non-free-firmware
  - deb http://archive.raspberrypi.com/debian/ bookworm main
```

```sh
rm -rf ./repository
python3 update_repository.py --base-url http://deb.debian.org/debian/dists --suites bookworm bookworm-updates --components main contrib non-free non-free-firmware --platform binary-arm64
python3 update_repository.py --base-url http://deb.debian.org/debian-security/dists --suites bookworm-security --components main contrib non-free non-free-firmware --platform binary-arm64
python3 update_repository.py --base-url http://archive.raspberrypi.com/debian/dists --suites bookworm --components main --platform binary-arm64
python3 debian-package-installer.py --base-url http://archive.raspberrypi.com/debian,http://deb.debian.org/debian,http://deb.debian.org/debian-security --packages ffmpeg
```

## How It Works
 1. update_repository.py connects to the Ubuntu archive, downloads the Packages.gz index for each specified suite and component, extracts it, and saves it as a uniquely named text file in the repository/ folder.
 2. debian-package-installer.py starts by reading all the text files in repository/ to build a master index of available packages, their dependencies, and their download URLs.
 3. When given a package name (e.g., ffmpeg), it finds the package in the index, reads its `Depends`/`Pre-Depends` straight from that index (the same authoritative source apt uses), and downloads the .deb file. It does **not** open the .deb to compute dependencies — that keeps the tool cross-platform and matches apt's own behavior.
 4. It then recursively repeats step 3 for each dependency until the entire chain is resolved and downloaded.
 5. If a package (.deb file) already exists in the download directory, it will not be re-downloaded.
 6. If multiple versions of a dependency are found across different suites, the script chooses the one that sorts highest, which is the newest version.
 7. Virtual packages are resolved through the `Provides:` field: if a dependency has no direct match, the script looks for concrete packages that provide that name and picks the newest suitable one.
 8. Alternatives (`a | b | c`) are tried in order, the same way apt does. Architecture qualifiers (`:any`, `:native`, `:arm64`) and arch restrictions (`[arm64 amd64]`) are honored, and `Pre-Depends` is followed alongside `Depends`. `Recommends` and `Suggests` are deliberately ignored.
 9. If a dependency cannot be satisfied at all, the script fails loudly rather than skipping it — a bundle that looks complete but won't install is worse than an error.

## Limitations

 * **No signature or checksum verification.** The script does not fetch or validate `Release`/`InRelease` files, and does not verify GPG signatures or package hashes. Some examples below use plain HTTP. If integrity matters for your use case, prefer HTTPS mirrors and verify the downloaded set independently.
 * **Newest version always wins**, with no suite pinning. If you index a backports suite alongside a stable one, you may pull backported packages and their newer dependencies.
