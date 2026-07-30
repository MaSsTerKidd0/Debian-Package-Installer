#!/usr/bin/env python3
"""
Debian Package Installer -- CustomTkinter GUI.

A modern, dark-by-default front end: pick a target OS, give it package names
(typed or loaded from a file), optionally fold in a machine "starter kit" and an
SSH server, and it downloads the full dependency closure and writes a
self-installing .tar.gz bundle -- with a live log and progress bar.

All network/disk work runs on a background thread that talks to the UI only
through a thread-safe queue drained by the main loop (Tk/CustomTkinter widgets
are not thread-safe, so they're only ever touched from the main thread).

Run:  python gui.py     (needs `pip install customtkinter`)
"""

import os
import queue
import shutil
import threading
import tkinter as tk
from tkinter import filedialog, messagebox

import customtkinter as ctk

from dpi import config, resolve_and_download
from dpi.packager import build_bundle
from dpi.presets import load_presets
from dpi.reporter import CallbackReporter
from dpi.repository import update_repository
from dpi.starter_kits import load_starter_kits, merge_packages


# Default both dropdowns to a Raspberry Pi entry if one exists.
_DEFAULT_PICK_HINT = "raspberry pi"

_CARD_PAD = {"padx": 14, "pady": (0, 14)}
_INNER = {"padx": 14, "pady": 8}


class App:
    def __init__(self, root: ctk.CTk, presets: dict, kits: dict):
        self.root = root
        self.presets = presets
        self.kits = kits

        root.title("Debian Package Installer")
        root.geometry("960x720")
        root.minsize(880, 640)

        # Messages from the worker thread land here; the main loop drains them.
        self.q: "queue.Queue[tuple]" = queue.Queue()
        self.worker: threading.Thread = None

        self._build_widgets()
        self.root.after(100, self._drain_queue)

    # -- helpers ------------------------------------------------------------
    def _default_by_hint(self, options) -> str:
        for name in options:
            if _DEFAULT_PICK_HINT in name.lower():
                return name
        return next(iter(options))

    def _card(self, parent, title: str) -> ctk.CTkFrame:
        """A titled rounded card that fills its column width."""
        card = ctk.CTkFrame(parent, corner_radius=12)
        card.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            card, text=title, anchor="w",
            font=ctk.CTkFont(size=13, weight="bold"),
        ).grid(row=0, column=0, sticky="ew", padx=14, pady=(12, 2))
        return card

    # -- layout -------------------------------------------------------------
    def _build_widgets(self):
        self.root.grid_columnconfigure(0, weight=0, minsize=460)
        self.root.grid_columnconfigure(1, weight=1)
        self.root.grid_rowconfigure(1, weight=1)

        # --- Header: title + appearance switcher -------------------------
        header = ctk.CTkFrame(self.root, corner_radius=0, fg_color="transparent")
        header.grid(row=0, column=0, columnspan=2, sticky="ew", padx=16, pady=(14, 8))
        header.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            header, text="Debian Package Installer",
            font=ctk.CTkFont(size=20, weight="bold"),
        ).grid(row=0, column=0, sticky="w")
        self.appearance = ctk.CTkSegmentedButton(
            header, values=["System", "Light", "Dark"], command=ctk.set_appearance_mode,
        )
        self.appearance.set("Dark")
        self.appearance.grid(row=0, column=1, sticky="e")

        # --- Left column: configuration cards ----------------------------
        left = ctk.CTkScrollableFrame(self.root, corner_radius=0, fg_color="transparent")
        left.grid(row=1, column=0, sticky="nsew", padx=(12, 6), pady=4)
        left.grid_columnconfigure(0, weight=1)
        r = 0

        # Target OS
        card = self._card(left, "Target OS")
        card.grid(row=r, column=0, sticky="ew", **_CARD_PAD); r += 1
        self.os_var = tk.StringVar(value=self._default_by_hint(self.presets))
        ctk.CTkOptionMenu(card, variable=self.os_var, values=list(self.presets.keys())).grid(
            row=1, column=0, sticky="ew", **_INNER)

        # Packages
        card = self._card(left, "Packages (one per line)")
        card.grid(row=r, column=0, sticky="ew", **_CARD_PAD); r += 1
        self.pkg_text = ctk.CTkTextbox(card, height=120)
        self.pkg_text.grid(row=1, column=0, sticky="ew", **_INNER)
        btns = ctk.CTkFrame(card, fg_color="transparent")
        btns.grid(row=2, column=0, sticky="ew", padx=14, pady=(0, 12))
        ctk.CTkButton(btns, text="Load list…", width=110, command=self._load_list).pack(side="left")
        ctk.CTkButton(btns, text="Clear", width=80, fg_color="transparent", border_width=1,
                      command=lambda: self.pkg_text.delete("1.0", "end")).pack(side="left", padx=8)

        # Essential packages
        card = self._card(left, "Essential packages")
        card.grid(row=r, column=0, sticky="ew", **_CARD_PAD); r += 1
        self.essentials_var = tk.BooleanVar(value=False)
        ctk.CTkCheckBox(card, text="Include a starter kit for this machine",
                        variable=self.essentials_var, command=self._toggle_machine).grid(
            row=1, column=0, sticky="w", **_INNER)
        self.machine_var = tk.StringVar(value=self._default_by_hint(self.kits))
        self.machine_menu = ctk.CTkOptionMenu(
            card, variable=self.machine_var, values=list(self.kits.keys()),
            state="disabled", command=lambda _v: self._update_kit_desc())
        self.machine_menu.grid(row=2, column=0, sticky="ew", **_INNER)
        self.kit_desc_var = tk.StringVar(value="")
        ctk.CTkLabel(card, textvariable=self.kit_desc_var, anchor="w", justify="left",
                     wraplength=390, text_color=("gray40", "gray60")).grid(
            row=3, column=0, sticky="ew", padx=14, pady=(0, 4))
        self.ssh_var = tk.BooleanVar(value=False)
        ctk.CTkCheckBox(card, text="Include OpenSSH server (remote access)",
                        variable=self.ssh_var).grid(row=4, column=0, sticky="w", padx=14, pady=(4, 12))

        # Options
        card = self._card(left, "Options")
        card.grid(row=r, column=0, sticky="ew", **_CARD_PAD); r += 1
        self.update_var = tk.BooleanVar(value=True)
        ctk.CTkCheckBox(card, text="Download/refresh package index first (once per OS)",
                        variable=self.update_var).grid(row=1, column=0, sticky="w", **_INNER)
        self.keepgoing_var = tk.BooleanVar(value=True)
        ctk.CTkCheckBox(card, text="Skip packages that can't be resolved (warn & continue)",
                        variable=self.keepgoing_var).grid(row=2, column=0, sticky="w", padx=14, pady=(4, 12))

        # Output
        card = self._card(left, "Output bundle")
        card.grid(row=r, column=0, sticky="ew", **_CARD_PAD); r += 1
        self.out_var = tk.StringVar(value=os.path.abspath("offline-bundle.tar.gz"))
        ctk.CTkEntry(card, textvariable=self.out_var).grid(row=1, column=0, sticky="ew", **_INNER)
        ctk.CTkButton(card, text="Browse…", width=110, command=self._pick_output).grid(
            row=2, column=0, sticky="w", padx=14, pady=(0, 12))

        # --- Right column: activity --------------------------------------
        right = ctk.CTkFrame(self.root, corner_radius=12)
        right.grid(row=1, column=1, sticky="nsew", padx=(6, 12), pady=4)
        right.grid_columnconfigure(0, weight=1)
        right.grid_rowconfigure(1, weight=1)
        ctk.CTkLabel(right, text="Activity log", anchor="w",
                     font=ctk.CTkFont(size=13, weight="bold")).grid(
            row=0, column=0, sticky="ew", padx=14, pady=(12, 2))
        self.log = ctk.CTkTextbox(right, state="disabled", wrap="word",
                                  font=ctk.CTkFont(family="Consolas", size=12))
        self.log.grid(row=1, column=0, sticky="nsew", padx=14, pady=6)
        self.progress = ctk.CTkProgressBar(right)
        self.progress.set(0)
        self.progress.grid(row=2, column=0, sticky="ew", padx=14, pady=(6, 4))
        self.status_var = tk.StringVar(value="Ready.")
        ctk.CTkLabel(right, textvariable=self.status_var, anchor="w").grid(
            row=3, column=0, sticky="ew", padx=14, pady=(0, 12))

        # --- Build button spanning the bottom ----------------------------
        self.run_btn = ctk.CTkButton(
            self.root, text="Build Offline Bundle", height=44,
            font=ctk.CTkFont(size=15, weight="bold"), command=self._start)
        self.run_btn.grid(row=2, column=0, columnspan=2, sticky="ew", padx=16, pady=(4, 16))

    # -- small UI helpers ---------------------------------------------------
    def _toggle_machine(self):
        self.machine_menu.configure(state="normal" if self.essentials_var.get() else "disabled")
        self._update_kit_desc()

    def _update_kit_desc(self):
        if self.essentials_var.get():
            kit = self.kits.get(self.machine_var.get(), {})
            n = len(kit.get("packages", []))
            self.kit_desc_var.set(f"{kit.get('description', '')}  ({n} packages)")
        else:
            self.kit_desc_var.set("")

    def _read_names(self, text: str):
        names = []
        for line in text.replace(",", "\n").splitlines():
            line = line.split("#", 1)[0].strip()
            if line:
                names.append(line)
        return names

    def _load_list(self):
        path = filedialog.askopenfilename(
            title="Choose a file listing package names",
            filetypes=[("Text files", "*.txt"), ("All files", "*.*")])
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()
        except Exception as e:
            messagebox.showerror("Could not read file", str(e))
            return
        self.pkg_text.delete("1.0", "end")
        self.pkg_text.insert("1.0", "\n".join(self._read_names(content)))

    def _pick_output(self):
        path = filedialog.asksaveasfilename(
            title="Save bundle as", defaultextension=".tar.gz",
            initialfile="offline-bundle.tar.gz",
            filetypes=[("Gzipped tar", "*.tar.gz"), ("All files", "*.*")])
        if path:
            self.out_var.set(path)

    def _packages(self):
        return self._read_names(self.pkg_text.get("1.0", "end"))

    def _append_log(self, msg: str):
        self.log.configure(state="normal")
        self.log.insert("end", msg + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    # -- run / worker -------------------------------------------------------
    def _start(self):
        if self.worker and self.worker.is_alive():
            return

        user_packages = self._packages()
        add_ons = []
        machine = None
        if self.essentials_var.get():
            machine = self.machine_var.get()
            add_ons += self.kits.get(machine, {}).get("packages", [])
        if self.ssh_var.get():
            add_ons.append("openssh-server")

        packages = merge_packages(user_packages, add_ons)
        if not packages:
            messagebox.showwarning(
                "No packages",
                "Enter at least one package name, or tick 'Include a starter kit' / OpenSSH.")
            return
        output = self.out_var.get().strip()
        if not output:
            messagebox.showwarning("No output path", "Choose where to save the bundle.")
            return

        preset = self.presets[self.os_var.get()]
        do_update = self.update_var.get()
        keep_going = self.keepgoing_var.get()

        self.run_btn.configure(state="disabled")
        self.progress.set(0)
        self.status_var.set("Working…")
        self._append_log("=" * 60)
        extra = len(packages) - len(user_packages)
        if extra > 0:
            src = f"'{machine}' kit" if machine else "add-ons"
            self._append_log(f"Added {extra} extra package(s) from {src} "
                             f"(missing ones will be skipped if keep-going is on).")

        self.worker = threading.Thread(
            target=self._work, args=(preset, packages, output, do_update, keep_going), daemon=True)
        self.worker.start()

    def _work(self, preset, packages, output, do_update, keep_going):
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
            target_arch, visited, failures = resolve_and_download(
                packages, preset["download_base_urls"],
                repo_dir=repo_dir, download_dir=download_dir,
                reporter=reporter, keep_going=keep_going,
            )

            if not visited:
                self.q.put(("error",
                            "No packages could be resolved, so no bundle was built.\n"
                            "Check the names, or that the index matches your target OS."))
                return

            self.q.put(("status", "Building bundle…"))
            succeeded = [p for p in packages if p not in {n for n, _ in failures}]
            build_bundle(download_dir, output, target_arch, succeeded, reporter)

            self.q.put(("done", output, len(visited), failures))
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
                    self.progress.set((done / total) if total else 0)
                    self.status_var.set(f"Downloading… {done} of ~{total} packages")
                elif kind == "done":
                    output, count, failures = item[1], item[2], item[3]
                    self.progress.set(1)
                    skipped_note = f" ({len(failures)} skipped)" if failures else ""
                    self.status_var.set(f"Done — {count} packages bundled{skipped_note}.")
                    self._append_log(f"\nBundle ready: {output}")
                    if failures:
                        self._append_log("Skipped (not found or unresolvable):")
                        for name, reason in failures:
                            first = reason.strip().splitlines()[0] if reason.strip() else "unknown reason"
                            self._append_log(f"  - {name}: {first}")
                    self.run_btn.configure(state="normal")
                    skipped_msg = ""
                    if failures:
                        skipped_msg = (
                            f"\n\nSkipped {len(failures)} package(s) that couldn't be resolved:\n"
                            + "\n".join(f"  • {name}" for name, _ in failures))
                    messagebox.showinfo(
                        "Bundle complete",
                        f"Wrote {count} packages to:\n{output}{skipped_msg}\n\n"
                        "On the target machine:\n"
                        "  tar xzf <bundle>.tar.gz\n"
                        "  cd <bundle>/\n"
                        "  chmod +x install.sh\n"
                        "  sudo ./install.sh")
                elif kind == "error":
                    self.status_var.set("Failed.")
                    self._append_log(f"\nERROR: {item[1]}")
                    self.run_btn.configure(state="normal")
                    messagebox.showerror("Failed", item[1])
        except queue.Empty:
            pass
        self.root.after(100, self._drain_queue)


def main():
    ctk.set_appearance_mode("Dark")
    ctk.set_default_color_theme("blue")
    root = ctk.CTk()

    # Load presets/kits before building the UI; a malformed JSON should be a clear
    # message, not a stack trace, and we fall back to built-in defaults.
    try:
        presets = load_presets()
    except Exception as e:
        from dpi.presets import DEFAULT_PRESETS
        messagebox.showwarning("Could not load presets.json",
                               f"{e}\n\nFalling back to built-in defaults.")
        presets = DEFAULT_PRESETS
    try:
        kits = load_starter_kits()
    except Exception as e:
        from dpi.starter_kits import DEFAULT_STARTER_KITS
        messagebox.showwarning("Could not load starter_kits.json",
                               f"{e}\n\nFalling back to built-in defaults.")
        kits = DEFAULT_STARTER_KITS

    App(root, presets, kits)
    root.mainloop()


if __name__ == "__main__":
    main()
