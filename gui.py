#!/usr/bin/env python3
"""
Small Tkinter GUI for Debian Package Installer.

Pick a target OS, give it a list of package names (typed or loaded from a file),
and it downloads the full dependency closure and writes a self-installing
.tar.gz bundle -- with a live log and progress bar.

All network/disk work runs on a background thread; the worker talks to the UI
only through a thread-safe queue that the Tk main loop drains. Tkinter is not
thread-safe, so widgets are ONLY ever touched from the main thread.

Run:  python gui.py
"""

import os
import queue
import shutil
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from dpi import config, resolve_and_download
from dpi.packager import build_bundle
from dpi.reporter import CallbackReporter
from dpi.repository import update_repository


# ---------------------------------------------------------------------------
# OS presets. Each bundles the index sources (for the "update" step) and the
# download mirrors (for fetching .debs). "index_sources" may have several
# entries -- e.g. Raspberry Pi pulls from Debian, Debian-security, and the RPi
# Foundation repo -- which is why this can't be a single base URL.
# ---------------------------------------------------------------------------
PRESETS = {
    "Ubuntu 24.04 LTS (amd64)": {
        "platform": "binary-amd64",
        "index_sources": [
            {
                "base_url": "https://us.archive.ubuntu.com/ubuntu/dists",
                "suites": ["noble", "noble-updates", "noble-security", "noble-backports"],
                "components": ["main", "restricted", "universe", "multiverse"],
            },
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


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title("Debian Package Installer")
        root.minsize(640, 620)

        # Messages from the worker thread land here; the main loop drains them.
        self.q: "queue.Queue[tuple]" = queue.Queue()
        self.worker: threading.Thread = None

        self._build_widgets()
        self.root.after(100, self._drain_queue)

    # -- layout -------------------------------------------------------------
    def _build_widgets(self):
        pad = {"padx": 8, "pady": 4}
        frm = ttk.Frame(self.root, padding=10)
        frm.pack(fill="both", expand=True)
        frm.columnconfigure(1, weight=1)

        row = 0
        ttk.Label(frm, text="Target OS:").grid(row=row, column=0, sticky="w", **pad)
        self.os_var = tk.StringVar(value=list(PRESETS.keys())[-1])  # default: RPi
        os_combo = ttk.Combobox(frm, textvariable=self.os_var,
                                values=list(PRESETS.keys()), state="readonly")
        os_combo.grid(row=row, column=1, columnspan=2, sticky="ew", **pad)

        row += 1
        ttk.Label(frm, text="Packages (one per line):").grid(row=row, column=0, sticky="nw", **pad)
        self.pkg_text = tk.Text(frm, height=6, width=40, undo=True)
        self.pkg_text.grid(row=row, column=1, sticky="ew", **pad)
        btns = ttk.Frame(frm)
        btns.grid(row=row, column=2, sticky="n", **pad)
        ttk.Button(btns, text="Load list…", command=self._load_list).pack(fill="x", pady=2)
        ttk.Button(btns, text="Clear", command=lambda: self.pkg_text.delete("1.0", "end")).pack(fill="x", pady=2)

        row += 1
        ttk.Label(frm, text="Output bundle:").grid(row=row, column=0, sticky="w", **pad)
        self.out_var = tk.StringVar(value=os.path.abspath("offline-bundle.tar.gz"))
        ttk.Entry(frm, textvariable=self.out_var).grid(row=row, column=1, sticky="ew", **pad)
        ttk.Button(frm, text="Browse…", command=self._pick_output).grid(row=row, column=2, **pad)

        row += 1
        self.update_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            frm,
            text="Download/refresh package index first (needed once per OS; can take a few minutes)",
            variable=self.update_var,
        ).grid(row=row, column=0, columnspan=3, sticky="w", **pad)

        row += 1
        self.run_btn = ttk.Button(frm, text="Build Offline Bundle", command=self._start)
        self.run_btn.grid(row=row, column=0, columnspan=3, sticky="ew", **pad)

        row += 1
        self.progress = ttk.Progressbar(frm, mode="determinate", maximum=100)
        self.progress.grid(row=row, column=0, columnspan=3, sticky="ew", **pad)

        row += 1
        self.status_var = tk.StringVar(value="Ready.")
        ttk.Label(frm, textvariable=self.status_var).grid(row=row, column=0, columnspan=3, sticky="w", **pad)

        row += 1
        frm.rowconfigure(row, weight=1)
        log_frame = ttk.Frame(frm)
        log_frame.grid(row=row, column=0, columnspan=3, sticky="nsew", **pad)
        log_frame.rowconfigure(0, weight=1)
        log_frame.columnconfigure(0, weight=1)
        self.log = tk.Text(log_frame, height=12, state="disabled", wrap="word")
        self.log.grid(row=0, column=0, sticky="nsew")
        sb = ttk.Scrollbar(log_frame, command=self.log.yview)
        sb.grid(row=0, column=1, sticky="ns")
        self.log.configure(yscrollcommand=sb.set)

    # -- small UI helpers ---------------------------------------------------
    def _load_list(self):
        path = filedialog.askopenfilename(
            title="Choose a file listing package names",
            filetypes=[("Text files", "*.txt"), ("All files", "*.*")],
        )
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()
        except Exception as e:
            messagebox.showerror("Could not read file", str(e))
            return
        # Accept one-per-line OR whitespace/comma separated; ignore blank lines
        # and '#' comments.
        names = []
        for line in content.replace(",", "\n").splitlines():
            line = line.split("#", 1)[0].strip()
            if line:
                names.append(line)
        self.pkg_text.delete("1.0", "end")
        self.pkg_text.insert("1.0", "\n".join(names))

    def _pick_output(self):
        path = filedialog.asksaveasfilename(
            title="Save bundle as",
            defaultextension=".tar.gz",
            initialfile="offline-bundle.tar.gz",
            filetypes=[("Gzipped tar", "*.tar.gz"), ("All files", "*.*")],
        )
        if path:
            self.out_var.set(path)

    def _packages(self):
        raw = self.pkg_text.get("1.0", "end")
        names = []
        for line in raw.replace(",", "\n").splitlines():
            line = line.split("#", 1)[0].strip()
            if line:
                names.append(line)
        return names

    def _append_log(self, msg: str):
        self.log.configure(state="normal")
        self.log.insert("end", msg + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    # -- run / worker -------------------------------------------------------
    def _start(self):
        if self.worker and self.worker.is_alive():
            return
        packages = self._packages()
        if not packages:
            messagebox.showwarning("No packages", "Enter at least one package name.")
            return
        output = self.out_var.get().strip()
        if not output:
            messagebox.showwarning("No output path", "Choose where to save the bundle.")
            return

        preset = PRESETS[self.os_var.get()]
        do_update = self.update_var.get()

        self.run_btn.configure(state="disabled")
        self.progress.configure(value=0)
        self.status_var.set("Working…")
        self._append_log("=" * 60)

        self.worker = threading.Thread(
            target=self._work, args=(preset, packages, output, do_update), daemon=True
        )
        self.worker.start()

    def _work(self, preset, packages, output, do_update):
        """Runs on the worker thread. Only touches the UI via self.q."""
        def log(msg):
            self.q.put(("log", msg))

        def progress(done, total):
            self.q.put(("progress", done, total))

        reporter = CallbackReporter(log_cb=log, progress_cb=progress)

        try:
            repo_dir = config.REPO_DIR
            download_dir = config.DOWNLOAD_DIR

            if do_update:
                # Clear the index first so switching target OS can't trip the
                # one-architecture-per-repository rule (mirrors README's rm -rf).
                if os.path.isdir(repo_dir):
                    log(f"Clearing existing index at {repo_dir} …")
                    shutil.rmtree(repo_dir, ignore_errors=True)
                for src in preset["index_sources"]:
                    update_repository(
                        src["base_url"], src["suites"], src["components"],
                        preset["platform"], repo_dir=repo_dir, reporter=reporter,
                    )

            self.q.put(("status", "Resolving and downloading dependencies…"))
            target_arch, visited = resolve_and_download(
                packages,
                preset["download_base_urls"],
                repo_dir=repo_dir,
                download_dir=download_dir,
                reporter=reporter,
            )

            self.q.put(("status", "Building bundle…"))
            build_bundle(download_dir, output, target_arch, packages, reporter)

            self.q.put(("done", output, len(visited)))
        except Exception as e:
            self.q.put(("error", str(e)))

    # -- main-thread queue drain -------------------------------------------
    def _drain_queue(self):
        try:
            while True:
                item = self.q.get_nowait()
                kind = item[0]
                if kind == "log":
                    self._append_log(item[1])
                elif kind == "status":
                    self.status_var.set(item[1])
                elif kind == "progress":
                    done, total = item[1], item[2]
                    pct = (done / total * 100) if total else 0
                    self.progress.configure(value=pct)
                    self.status_var.set(f"Downloading… {done} of ~{total} packages")
                elif kind == "done":
                    output, count = item[1], item[2]
                    self.progress.configure(value=100)
                    self.status_var.set(f"Done — {count} packages bundled.")
                    self._append_log(f"\nBundle ready: {output}")
                    self.run_btn.configure(state="normal")
                    messagebox.showinfo(
                        "Bundle complete",
                        f"Wrote {count} packages to:\n{output}\n\n"
                        "On the target machine:\n"
                        "  tar xzf <bundle>.tar.gz\n"
                        "  cd <bundle>/\n"
                        "  chmod +x install.sh\n"
                        "  sudo ./install.sh",
                    )
                elif kind == "error":
                    self.status_var.set("Failed.")
                    self._append_log(f"\nERROR: {item[1]}")
                    self.run_btn.configure(state="normal")
                    messagebox.showerror("Failed", item[1])
        except queue.Empty:
            pass
        self.root.after(100, self._drain_queue)


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
