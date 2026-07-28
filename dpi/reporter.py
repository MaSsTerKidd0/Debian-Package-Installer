"""
A tiny sink for human-readable log lines and coarse progress.

The core logic (index/download) used to call print() directly, which made it
impossible to drive a GUI progress bar or capture output. A Reporter decouples
"something happened" from "how the user is told about it":

  - ConsoleReporter  -> prints, for the CLI.
  - CallbackReporter -> forwards to callables, for the GUI (which marshals them
    onto the Tk main thread).

Progress is deliberately coarse. The dependency graph is discovered lazily by
recursion, so the true total isn't known up front; callers report
(done, done + still_queued), which advances monotonically toward 100% as the
work queue drains.
"""

from typing import Callable, Optional


class Reporter:
    """Base sink. Default implementation swallows everything (useful in tests)."""

    def log(self, message: str) -> None:
        pass

    def warn(self, message: str) -> None:
        """A non-fatal problem the user should see. Routed through log() with a
        prefix by default, so any Reporter gets warnings for free; subclasses may
        override to render them distinctly (e.g. colored in a GUI)."""
        self.log(f"WARNING: {message}")

    def progress(self, done: int, total: int) -> None:
        pass


class ConsoleReporter(Reporter):
    """Prints log lines to stdout. Progress is ignored -- the log lines already
    show live activity in a terminal, and a redrawing bar fights with them."""

    def log(self, message: str) -> None:
        print(message)


class CallbackReporter(Reporter):
    """Forwards to caller-supplied callables. Either may be None."""

    def __init__(
        self,
        log_cb: Optional[Callable[[str], None]] = None,
        progress_cb: Optional[Callable[[int, int], None]] = None,
    ):
        self._log_cb = log_cb
        self._progress_cb = progress_cb

    def log(self, message: str) -> None:
        if self._log_cb is not None:
            self._log_cb(message)

    def progress(self, done: int, total: int) -> None:
        if self._progress_cb is not None:
            self._progress_cb(done, total)
