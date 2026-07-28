"""
Fetch and decompress the Packages indexes (the "update" step).

Downloads Packages.gz (falling back to Packages.xz) for each suite x component,
decompresses it, and writes one uniquely named .txt per combination into the
repository directory. This is the input the resolver later reads.
"""

import gzip
import lzma
import os
import shutil
import tempfile
from typing import List, Optional
from urllib.parse import urlparse

import requests

from .config import DOWNLOAD_TIMEOUT_SECONDS, REPO_DIR
from .reporter import Reporter, ConsoleReporter


def setup_repository_dir(repo_dir: str, reporter: Reporter) -> None:
    """Create repo_dir if needed, or reuse it (new files are appended)."""
    reporter.log(f"Setting up repository directory at: {repo_dir}")

    # Check BEFORE creating -- after makedirs(exist_ok=True) it always exists.
    already_existed = os.path.isdir(repo_dir)
    try:
        os.makedirs(repo_dir, exist_ok=True)
    except OSError as e:
        raise Exception(f"Error creating directory {repo_dir}: {e}")

    if already_existed:
        reporter.log(f"Existing directory found, will append to it: {repo_dir}")
    else:
        reporter.log(f"Successfully created new directory: {repo_dir}")


def download_and_extract(url: str, output_path: str, repo_dir: str, reporter: Reporter) -> None:
    """
    Download a compressed Packages file (.gz, falling back to .xz on 404),
    decompress it, and write the plain text to output_path. A missing index
    (404 on both formats) is warned and skipped, not fatal.
    """
    try:
        reporter.log(f"Downloading: {url}")
        response = requests.get(url, timeout=DOWNLOAD_TIMEOUT_SECONDS)
        response.raise_for_status()
    except requests.exceptions.HTTPError as e:
        if e.response.status_code == 404 and url.endswith('.gz'):
            url = url[:-3] + '.xz'
            reporter.log(f"Trying alternative format: {url}")
            try:
                response = requests.get(url, timeout=DOWNLOAD_TIMEOUT_SECONDS)
                response.raise_for_status()
            except requests.exceptions.RequestException as ex:
                raise Exception(f"Failed to download {url}: {ex}")
        elif e.response.status_code == 404:
            reporter.log(f"Warning: Not found (404): {url}. Skipping.")
            return
        else:
            raise Exception(f"HTTP Error for {url}: {e}")
    except requests.exceptions.RequestException as e:
        raise Exception(f"Failed to download {url}: {e}")

    ext = '.xz' if url.endswith('.xz') else '.gz'

    # Unique temp name per download so concurrent runs can't clobber each other's
    # archive mid-extract and write the wrong suite's data to output_path.
    fd, archive_path = tempfile.mkstemp(prefix='Packages-', suffix=ext, dir=repo_dir)
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(response.content)

        opener = gzip.open if ext == '.gz' else lzma.open
        try:
            with opener(archive_path, 'rb') as f_in:
                with open(output_path, 'wb') as f_out:
                    shutil.copyfileobj(f_in, f_out)
            reporter.log(f"Extracted and saved to: {output_path}")
        except Exception as e:
            raise Exception(f"Failed to extract {url}: {e}")
    finally:
        if os.path.exists(archive_path):
            os.remove(archive_path)


def update_repository(
    base_url: str,
    suites: List[str],
    components: List[str],
    platform: str,
    repo_dir: str = REPO_DIR,
    reporter: Optional[Reporter] = None,
) -> None:
    """
    For each suite x component, fetch and store the Packages index for `platform`
    (e.g. 'binary-arm64'). A failure on one combination is logged and skipped so a
    single missing component doesn't abort the whole run.
    """
    if reporter is None:
        reporter = ConsoleReporter()

    parsed = urlparse(base_url)
    if not parsed.scheme or not parsed.hostname:
        raise ValueError(
            f"Invalid base URL {base_url!r}: expected a full URL including scheme, "
            f"e.g. 'https://us.archive.ubuntu.com/ubuntu/dists'."
        )
    host = parsed.hostname.replace('.', '-')

    setup_repository_dir(repo_dir, reporter)

    for suite in suites:
        for component in components:
            url = f"{base_url.rstrip('/')}/{suite}/{component}/{platform}/Packages.gz"
            output_filename = f"{host}-{suite}-{component}-{platform}.txt"
            output_path = os.path.join(repo_dir, output_filename)
            try:
                download_and_extract(url, output_path, repo_dir, reporter)
            except Exception as e:
                reporter.log(f"Error processing {suite}/{component}: {e}")
                reporter.log("Continuing with the next item...")

    reporter.log("\nRepository update process finished.")
