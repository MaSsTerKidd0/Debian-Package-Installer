"""Default paths and tunables. Kept in one place so the CLI, GUI, and packager
all agree, and so a caller can override them without editing logic modules."""

DOWNLOAD_DIR = "./downloaded/"
REPO_DIR = "./repository"

# Never block forever on an unresponsive mirror. Applies per-request; the
# base-URL fallback loop moves on to the next mirror on timeout.
DOWNLOAD_TIMEOUT_SECONDS = 30
