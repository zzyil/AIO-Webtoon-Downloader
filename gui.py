#!/usr/bin/env python3
"""
AIO Webtoon/Manga Downloader — GUI
Tkinter-based graphical interface for aio-dl.py.
"""

import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from tkinter import (
    BooleanVar,
    END,
    IntVar,
    StringVar,
    Tk,
    filedialog,
    messagebox,
    ttk,
)
import tkinter as tk

try:
    from PIL import Image, ImageTk
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

REPO_DIR = Path(__file__).resolve().parent
AIO_DL = REPO_DIR / "aio-dl.py"
SAVED_PARAMS_FILE = "download_params.json"
GUI_PREFS_FILE = REPO_DIR / ".gui_prefs.json"
SUPPORTED_EXTS = {".cbz", ".pdf", ".epub"}
CH_FILE_RE = re.compile(r"[ _]Ch[ _](\d+)", re.IGNORECASE)


# ── Preferences ──────────────────────────────────────────────
def load_prefs() -> dict:
    try:
        return json.loads(GUI_PREFS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_prefs(data: dict):
    try:
        GUI_PREFS_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception:
        pass


# ── Helpers ──────────────────────────────────────────────────
def scan_library(root: str) -> list:
    results = []
    if not os.path.isdir(root):
        return results
    for entry in sorted(os.listdir(root)):
        folder = os.path.join(root, entry)
        if not os.path.isdir(folder) or entry.startswith("."):
            continue
        params_file = os.path.join(folder, SAVED_PARAMS_FILE)
        has_params = os.path.isfile(params_file)
        params = {}
        if has_params:
            try:
                with open(params_file, "r", encoding="utf-8") as f:
                    params = json.load(f)
            except Exception:
                pass
        # Count chapter files
        chapter_nums = set()
        total_size = 0
        file_count = 0
        for fn in os.listdir(folder):
            fp = os.path.join(folder, fn)
            if os.path.isfile(fp):
                ext = os.path.splitext(fn)[1].lower()
                if ext in SUPPORTED_EXTS:
                    file_count += 1
                    total_size += os.path.getsize(fp)
                    m = CH_FILE_RE.search(fn)
                    if m:
                        chapter_nums.add(int(m.group(1)))
        highest = 0
        while (highest + 1) in chapter_nums:
            highest += 1
        # Find cover image
        cover_path = None
        for candidate in (".cover.jpg", ".cover.png", "cover.jpg", "cover.png"):
            cp = os.path.join(folder, candidate)
            if os.path.isfile(cp):
                cover_path = cp
                break
        results.append({
            "name": entry,
            "folder": folder,
            "has_params": has_params,
            "params": params,
            "url": params.get("url", ""),
            "format": params.get("format", "?"),
            "chapters": len(chapter_nums),
            "highest": highest,
            "files": file_count,
            "size": total_size,
            "cover": cover_path,
        })
    return results


def format_size(num_bytes: int) -> str:
    if num_bytes == 0:
        return "0 B"
    mb = num_bytes / (1024 ** 2)
    if mb >= 1024:
        return f"{mb / 1024:.1f} GB"
    if mb >= 1:
        return f"{mb:.1f} MB"
    return f"{num_bytes / 1024:.1f} KB"


# ── Main Application ─────────────────────────────────────────
class AIODownloaderGUI:
    def __init__(self, root: Tk):
        self.root = root
        self.root.title("AIO Webtoon/Manga Downloader")
        self.root.geometry("900x700")
        self.root.minsize(750, 550)

        self.prefs = load_prefs()
        self.running_proc = None
        self.stop_flag = threading.Event()

        # ── Variables ──
        self.output_dir = StringVar(value=self.prefs.get("output_dir", str(REPO_DIR / "comics")))
        self.url_text = StringVar()
        self.chapters = StringVar(value="all")
        self.fmt = StringVar(value=self.prefs.get("format", "epub"))
        self.epub_layout = StringVar(value=self.prefs.get("epub_layout", "vertical"))
        self.language = StringVar(value=self.prefs.get("language", "en"))
        self.quality = IntVar(value=int(self.prefs.get("quality", 85)))
        self.scaling = IntVar(value=int(self.prefs.get("scaling", 100)))
        self.cookies = StringVar(value=self.prefs.get("cookies", ""))
        self.group = StringVar(value=self.prefs.get("group", ""))
        self.split = StringVar(value="")
        self.jobs = IntVar(value=int(self.prefs.get("jobs", 1)))

        self.save_params = BooleanVar(value=True)
        self.keep_chapters = BooleanVar(value=self.prefs.get("keep_chapters", True))
        self.keep_images = BooleanVar(value=self.prefs.get("keep_images", False))
        self.mix_by_upvote = BooleanVar(value=False)
        self.no_partials = BooleanVar(value=self.prefs.get("no_partials", False))
        self.no_processing = BooleanVar(value=False)
        self.verbose = BooleanVar(value=True)
        self.debug_mode = BooleanVar(value=False)

        self._build_ui()

    # ── UI Construction ──────────────────────────────────────
    def _build_ui(self):
        # Top bar: output directory
        top = ttk.Frame(self.root, padding=5)
        top.pack(fill="x")
        ttk.Label(top, text="Output Directory:").pack(side="left")
        ttk.Entry(top, textvariable=self.output_dir, width=50).pack(side="left", fill="x", expand=True, padx=5)
        ttk.Button(top, text="Browse", command=self._browse_output).pack(side="left")

        # Notebook tabs
        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill="both", expand=True, padx=5, pady=5)

        self._build_download_tab()
        self._build_library_tab()
        self._build_output_tab()

    def _build_download_tab(self):
        tab = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(tab, text="Download")

        # ── URL input ──
        url_frame = ttk.LabelFrame(tab, text="URLs (one per line)", padding=5)
        url_frame.pack(fill="x", pady=(0, 5))

        self.url_entry = tk.Text(url_frame, height=4, wrap="word")
        self.url_entry.pack(fill="x")

        # ── Two-column options ──
        opts = ttk.Frame(tab)
        opts.pack(fill="x", pady=5)
        left = ttk.Frame(opts)
        left.pack(side="left", fill="both", expand=True, padx=(0, 5))
        right = ttk.Frame(opts)
        right.pack(side="left", fill="both", expand=True, padx=(5, 0))

        # Left column
        basic = ttk.LabelFrame(left, text="Basic Options", padding=5)
        basic.pack(fill="x", pady=(0, 5))

        r = 0
        ttk.Label(basic, text="Chapters:").grid(row=r, column=0, sticky="w", pady=2)
        ttk.Entry(basic, textvariable=self.chapters, width=20).grid(row=r, column=1, sticky="ew", pady=2, padx=5)
        ttk.Label(basic, text="all / 5 / 1-10 / 1,3,5-7").grid(row=r, column=2, sticky="w")

        r += 1
        ttk.Label(basic, text="Format:").grid(row=r, column=0, sticky="w", pady=2)
        ttk.Combobox(basic, textvariable=self.fmt, values=["epub", "cbz", "pdf", "none"], state="readonly", width=17).grid(row=r, column=1, sticky="ew", pady=2, padx=5)

        r += 1
        ttk.Label(basic, text="EPUB Layout:").grid(row=r, column=0, sticky="w", pady=2)
        ttk.Combobox(basic, textvariable=self.epub_layout, values=["vertical", "page"], state="readonly", width=17).grid(row=r, column=1, sticky="ew", pady=2, padx=5)

        r += 1
        ttk.Label(basic, text="Language:").grid(row=r, column=0, sticky="w", pady=2)
        ttk.Entry(basic, textvariable=self.language, width=20).grid(row=r, column=1, sticky="ew", pady=2, padx=5)

        r += 1
        ttk.Label(basic, text="Quality:").grid(row=r, column=0, sticky="w", pady=2)
        quality_frame = ttk.Frame(basic)
        quality_frame.grid(row=r, column=1, columnspan=2, sticky="ew", pady=2, padx=5)
        ttk.Scale(quality_frame, from_=1, to=100, variable=self.quality, orient="horizontal").pack(side="left", fill="x", expand=True)
        ttk.Label(quality_frame, textvariable=self.quality, width=4).pack(side="left")

        r += 1
        ttk.Label(basic, text="Scaling:").grid(row=r, column=0, sticky="w", pady=2)
        scale_frame = ttk.Frame(basic)
        scale_frame.grid(row=r, column=1, columnspan=2, sticky="ew", pady=2, padx=5)
        ttk.Scale(scale_frame, from_=1, to=100, variable=self.scaling, orient="horizontal").pack(side="left", fill="x", expand=True)
        ttk.Label(scale_frame, textvariable=self.scaling, width=4).pack(side="left")

        basic.columnconfigure(1, weight=1)

        # Right column
        advanced = ttk.LabelFrame(right, text="Advanced Options", padding=5)
        advanced.pack(fill="x", pady=(0, 5))

        r = 0
        ttk.Label(advanced, text="Cookies:").grid(row=r, column=0, sticky="w", pady=2)
        cookie_frame = ttk.Frame(advanced)
        cookie_frame.grid(row=r, column=1, sticky="ew", pady=2, padx=5)
        ttk.Entry(cookie_frame, textvariable=self.cookies).pack(side="left", fill="x", expand=True)
        ttk.Button(cookie_frame, text="...", width=3, command=self._browse_cookies).pack(side="left", padx=(2, 0))

        r += 1
        ttk.Label(advanced, text="Group:").grid(row=r, column=0, sticky="w", pady=2)
        ttk.Entry(advanced, textvariable=self.group).grid(row=r, column=1, sticky="ew", pady=2, padx=5)

        r += 1
        ttk.Label(advanced, text="Split:").grid(row=r, column=0, sticky="w", pady=2)
        ttk.Entry(advanced, textvariable=self.split).grid(row=r, column=1, sticky="ew", pady=2, padx=5)

        r += 1
        ttk.Label(advanced, text="Jobs:").grid(row=r, column=0, sticky="w", pady=2)
        ttk.Spinbox(advanced, textvariable=self.jobs, from_=1, to=8, width=5).grid(row=r, column=1, sticky="w", pady=2, padx=5)

        advanced.columnconfigure(1, weight=1)

        # Checkboxes
        checks = ttk.LabelFrame(right, text="Flags", padding=5)
        checks.pack(fill="x")
        ttk.Checkbutton(checks, text="Save params (for --update-all)", variable=self.save_params).pack(anchor="w")
        ttk.Checkbutton(checks, text="Keep individual chapter files", variable=self.keep_chapters).pack(anchor="w")
        ttk.Checkbutton(checks, text="Keep raw images", variable=self.keep_images).pack(anchor="w")
        ttk.Checkbutton(checks, text="Mix by upvote", variable=self.mix_by_upvote).pack(anchor="w")
        ttk.Checkbutton(checks, text="Skip fractional chapters", variable=self.no_partials).pack(anchor="w")
        ttk.Checkbutton(checks, text="No image processing", variable=self.no_processing).pack(anchor="w")
        ttk.Checkbutton(checks, text="Verbose", variable=self.verbose).pack(anchor="w")
        ttk.Checkbutton(checks, text="Debug", variable=self.debug_mode).pack(anchor="w")

        # Buttons
        btn_frame = ttk.Frame(tab)
        btn_frame.pack(fill="x", pady=10)
        self.download_btn = ttk.Button(btn_frame, text="Download", command=self._start_download)
        self.download_btn.pack(side="left", padx=5)
        self.stop_btn = ttk.Button(btn_frame, text="Stop", command=self._stop_process, state="disabled")
        self.stop_btn.pack(side="left", padx=5)

    def _build_library_tab(self):
        tab = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(tab, text="Library")
        self._cover_images = {}  # Keep references to prevent GC

        # Toolbar
        toolbar = ttk.Frame(tab)
        toolbar.pack(fill="x", pady=(0, 5))
        ttk.Button(toolbar, text="Refresh", command=self._refresh_library).pack(side="left", padx=2)
        self.update_selected_btn = ttk.Button(toolbar, text="Update Selected", command=self._update_selected)
        self.update_selected_btn.pack(side="left", padx=2)
        self.update_all_btn = ttk.Button(toolbar, text="Update All", command=self._update_all)
        self.update_all_btn.pack(side="left", padx=2)
        ttk.Button(toolbar, text="Open Folder", command=self._open_library_folder).pack(side="right", padx=2)

        # Treeview with cover image support
        tree_frame = ttk.Frame(tab)
        tree_frame.pack(fill="both", expand=True)

        columns = ("title", "url", "format", "chapters", "highest", "files", "size")
        show_mode = "tree headings" if HAS_PIL else "headings"
        self.lib_tree = ttk.Treeview(tree_frame, columns=columns, show=show_mode, selectmode="extended")

        if HAS_PIL:
            self.lib_tree.heading("#0", text="Cover")
            self.lib_tree.column("#0", width=60, minwidth=50, anchor="center")

        self.lib_tree.heading("title", text="Title")
        self.lib_tree.heading("url", text="URL")
        self.lib_tree.heading("format", text="Fmt")
        self.lib_tree.heading("chapters", text="Ch")
        self.lib_tree.heading("highest", text="Latest")
        self.lib_tree.heading("files", text="Files")
        self.lib_tree.heading("size", text="Size")

        self.lib_tree.column("title", width=180, minwidth=100)
        self.lib_tree.column("url", width=200, minwidth=80)
        self.lib_tree.column("format", width=45, minwidth=40, anchor="center")
        self.lib_tree.column("chapters", width=40, minwidth=35, anchor="center")
        self.lib_tree.column("highest", width=50, minwidth=40, anchor="center")
        self.lib_tree.column("files", width=40, minwidth=35, anchor="center")
        self.lib_tree.column("size", width=70, minwidth=55, anchor="e")

        scrollbar = ttk.Scrollbar(tree_frame, orient="vertical", command=self.lib_tree.yview)
        self.lib_tree.configure(yscrollcommand=scrollbar.set)
        self.lib_tree.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        # Increase row height for cover thumbnails
        if HAS_PIL:
            style = ttk.Style()
            style.configure("Treeview", rowheight=50)

        # Summary bar
        self.lib_summary = StringVar(value="")
        ttk.Label(tab, textvariable=self.lib_summary, anchor="w").pack(fill="x", side="bottom")

    def _build_output_tab(self):
        tab = ttk.Frame(self.notebook, padding=5)
        self.notebook.add(tab, text="Output")

        self.output_text = tk.Text(tab, wrap="word", state="disabled", bg="#1e1e1e", fg="#d4d4d4",
                                   insertbackground="#d4d4d4", font=("Menlo", 11))
        scrollbar = ttk.Scrollbar(tab, orient="vertical", command=self.output_text.yview)
        self.output_text.configure(yscrollcommand=scrollbar.set)
        self.output_text.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        btn_bar = ttk.Frame(tab)
        btn_bar.pack(fill="x", side="bottom", pady=2)
        ttk.Button(btn_bar, text="Clear", command=self._clear_output).pack(side="right", padx=5)

    # ── Actions ──────────────────────────────────────────────
    def _browse_output(self):
        d = filedialog.askdirectory(initialdir=self.output_dir.get())
        if d:
            self.output_dir.set(d)

    def _browse_cookies(self):
        f = filedialog.askopenfilename(filetypes=[("Text files", "*.txt"), ("All", "*.*")])
        if f:
            self.cookies.set(f)

    def _clear_output(self):
        self.output_text.configure(state="normal")
        self.output_text.delete("1.0", END)
        self.output_text.configure(state="disabled")

    def _append_output(self, text: str):
        self.output_text.configure(state="normal")
        self.output_text.insert(END, text)
        self.output_text.see(END)
        self.output_text.configure(state="disabled")

    def _save_current_prefs(self):
        self.prefs.update({
            "output_dir": self.output_dir.get(),
            "format": self.fmt.get(),
            "epub_layout": self.epub_layout.get(),
            "language": self.language.get(),
            "quality": self.quality.get(),
            "scaling": self.scaling.get(),
            "cookies": self.cookies.get(),
            "group": self.group.get(),
            "jobs": self.jobs.get(),
            "keep_chapters": self.keep_chapters.get(),
            "keep_images": self.keep_images.get(),
            "no_partials": self.no_partials.get(),
        })
        save_prefs(self.prefs)

    def _build_cmd(self, urls: list, chapters: str = None) -> list:
        cmd = [sys.executable, str(AIO_DL)]
        cmd.extend(urls)
        cmd.extend(["--chapters", chapters or self.chapters.get()])
        cmd.extend(["--format", self.fmt.get()])
        cmd.extend(["--language", self.language.get()])
        cmd.extend(["--quality", str(self.quality.get())])
        cmd.extend(["--scaling", str(self.scaling.get())])
        cmd.extend(["--output-dir", self.output_dir.get()])

        if self.fmt.get() == "epub":
            cmd.extend(["--epub-layout", self.epub_layout.get()])
        if self.cookies.get():
            cmd.extend(["--cookies", self.cookies.get()])
        if self.group.get():
            for g in self.group.get().split(","):
                g = g.strip()
                if g:
                    cmd.extend(["--group", g])
        if self.split.get():
            cmd.extend(["--split", self.split.get()])
        if len(urls) > 1 and self.jobs.get() > 1:
            cmd.extend(["--jobs", str(self.jobs.get())])
        if self.save_params.get():
            cmd.append("--save-params")
        if self.keep_chapters.get():
            cmd.append("--keep-chapters")
        if self.keep_images.get():
            cmd.append("--keep-images")
        if self.mix_by_upvote.get():
            cmd.append("--mix-by-upvote")
        if self.no_partials.get():
            cmd.append("--no-partials")
        if self.no_processing.get():
            cmd.append("--no-processing")
        if self.verbose.get():
            cmd.append("--verbose")
        if self.debug_mode.get():
            cmd.append("--debug")
        return cmd

    def _set_running(self, running: bool):
        state = "disabled" if running else "normal"
        self.download_btn.configure(state=state)
        self.update_selected_btn.configure(state=state)
        self.update_all_btn.configure(state=state)
        self.stop_btn.configure(state="normal" if running else "disabled")

    def _run_cmd(self, cmd: list, on_done=None):
        self.stop_flag.clear()
        self._set_running(True)
        self.notebook.select(2)  # Switch to Output tab
        self._append_output(f"$ {' '.join(cmd[:5])}...\n\n")

        def _worker():
            try:
                self.running_proc = subprocess.Popen(
                    cmd,
                    cwd=str(REPO_DIR),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    encoding="utf-8",
                    errors="replace",
                    bufsize=1,
                )
                for line in self.running_proc.stdout:
                    if self.stop_flag.is_set():
                        self.running_proc.terminate()
                        self.root.after(0, self._append_output, "\n--- Stopped by user ---\n")
                        break
                    self.root.after(0, self._append_output, line)
                self.running_proc.wait()
                code = self.running_proc.returncode
                self.root.after(0, self._append_output, f"\n--- Process exited with code {code} ---\n\n")
            except Exception as e:
                self.root.after(0, self._append_output, f"\n--- Error: {e} ---\n\n")
            finally:
                self.running_proc = None
                self.root.after(0, self._set_running, False)
                if on_done:
                    self.root.after(0, on_done)

        threading.Thread(target=_worker, daemon=True).start()

    def _run_cmd_sequence(self, cmds: list, index: int = 0):
        """Run a list of commands sequentially."""
        if index >= len(cmds):
            self._set_running(False)
            self._refresh_library()
            return
        label, cmd = cmds[index]
        self._append_output(f"\n{'='*60}\n{label}\n{'='*60}\n")
        self.stop_flag.clear()

        def _worker():
            try:
                self.running_proc = subprocess.Popen(
                    cmd,
                    cwd=str(REPO_DIR),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    encoding="utf-8",
                    errors="replace",
                    bufsize=1,
                )
                for line in self.running_proc.stdout:
                    if self.stop_flag.is_set():
                        self.running_proc.terminate()
                        self.root.after(0, self._append_output, "\n--- Stopped by user ---\n")
                        self.running_proc = None
                        self.root.after(0, self._set_running, False)
                        return
                    self.root.after(0, self._append_output, line)
                self.running_proc.wait()
                code = self.running_proc.returncode
                self.root.after(0, self._append_output, f"\n--- Exited with code {code} ---\n")
            except Exception as e:
                self.root.after(0, self._append_output, f"\n--- Error: {e} ---\n")
            finally:
                self.running_proc = None
                self.root.after(100, self._run_cmd_sequence, cmds, index + 1)

        threading.Thread(target=_worker, daemon=True).start()

    def _start_download(self):
        raw = self.url_entry.get("1.0", END).strip()
        urls = [u.strip() for u in raw.splitlines() if u.strip()]
        if not urls:
            messagebox.showwarning("No URLs", "Enter at least one URL.")
            return
        self._save_current_prefs()
        self._clear_output()
        cmd = self._build_cmd(urls)
        self._run_cmd(cmd, on_done=self._refresh_library)

    def _stop_process(self):
        self.stop_flag.set()
        if self.running_proc:
            try:
                self.running_proc.terminate()
            except Exception:
                pass

    # ── Library ──────────────────────────────────────────────
    def _load_cover_thumbnail(self, path: str, size: int = 45) -> "ImageTk.PhotoImage | None":
        if not HAS_PIL or not path:
            return None
        try:
            img = Image.open(path)
            # Resize preserving aspect ratio, fitting within size x size
            img.thumbnail((size, size), Image.LANCZOS)
            photo = ImageTk.PhotoImage(img)
            return photo
        except Exception:
            return None

    def _refresh_library(self):
        for item in self.lib_tree.get_children():
            self.lib_tree.delete(item)
        self._cover_images.clear()
        root_dir = self.output_dir.get()
        entries = scan_library(root_dir)
        total_size = 0
        for e in entries:
            thumb = self._load_cover_thumbnail(e.get("cover"))
            kwargs = {}
            if thumb:
                self._cover_images[e["folder"]] = thumb
                kwargs["image"] = thumb
            self.lib_tree.insert("", END, iid=e["folder"], text="", values=(
                e["name"],
                e["url"][:60] if e["url"] else "(no params)",
                e["format"],
                e["chapters"],
                e["highest"],
                e["files"],
                format_size(e["size"]),
            ), **kwargs)
            total_size += e["size"]
        self.lib_summary.set(f"  {len(entries)} series  |  {format_size(total_size)} total")

    def _get_selected_series(self) -> list:
        selected = self.lib_tree.selection()
        if not selected:
            messagebox.showinfo("No Selection", "Select one or more series from the library.")
            return []
        results = []
        for folder in selected:
            params_file = os.path.join(folder, SAVED_PARAMS_FILE)
            if not os.path.isfile(params_file):
                name = os.path.basename(folder)
                messagebox.showwarning("No Params", f"'{name}' has no saved download parameters.\n"
                                       "Download it first with 'Save params' enabled.")
                continue
            try:
                with open(params_file, "r", encoding="utf-8") as f:
                    params = json.load(f)
            except Exception:
                continue
            if not params.get("url"):
                continue
            # Detect highest chapter
            nums = set()
            for fn in os.listdir(folder):
                m = CH_FILE_RE.search(fn)
                if m:
                    nums.add(int(m.group(1)))
            highest = 0
            while (highest + 1) in nums:
                highest += 1
            results.append({"folder": folder, "params": params, "highest": highest})
        return results

    def _build_update_cmd(self, series: dict) -> list:
        p = series["params"]
        highest = series["highest"]
        chapters_arg = f"{highest + 1}-" if highest > 0 else "all"
        cmd = [
            sys.executable, str(AIO_DL), p["url"],
            "--chapters", chapters_arg,
            "--format", p.get("format", "epub"),
            "--language", p.get("language", "en"),
            "--quality", str(p.get("quality", 85)),
            "--scaling", str(p.get("scaling", 100)),
            "--output-dir", self.output_dir.get(),
            "--save-params", "--keep-chapters",
        ]
        if p.get("epub_layout"):
            cmd.extend(["--epub-layout", p["epub_layout"]])
        if p.get("cookies"):
            cmd.extend(["--cookies", p["cookies"]])
        if p.get("group"):
            for g in p["group"]:
                cmd.extend(["--group", g])
        if p.get("mix_by_upvote"):
            cmd.append("--mix-by-upvote")
        if p.get("no_partials"):
            cmd.append("--no-partials")
        if p.get("keep_images"):
            cmd.append("--keep-images")
        if p.get("verbose", True):
            cmd.append("--verbose")
        if p.get("debug"):
            cmd.append("--debug")
        return cmd

    def _update_selected(self):
        series_list = self._get_selected_series()
        if not series_list:
            return
        self._save_current_prefs()
        self._clear_output()
        cmds = []
        for s in series_list:
            title = s["params"].get("title", os.path.basename(s["folder"]))
            start_ch = s["highest"] + 1 if s["highest"] > 0 else 1
            label = f"Updating: {title} (from chapter {start_ch})"
            cmds.append((label, self._build_update_cmd(s)))
        self._set_running(True)
        self.notebook.select(2)
        self._run_cmd_sequence(cmds)

    def _update_all(self):
        entries = scan_library(self.output_dir.get())
        series_list = []
        for e in entries:
            if not e["has_params"] or not e["url"]:
                continue
            series_list.append({
                "folder": e["folder"],
                "params": e["params"],
                "highest": e["highest"],
            })
        if not series_list:
            messagebox.showinfo("Nothing to Update", "No series with saved parameters found.")
            return
        self._save_current_prefs()
        self._clear_output()
        cmds = []
        for s in series_list:
            title = s["params"].get("title", os.path.basename(s["folder"]))
            start_ch = s["highest"] + 1 if s["highest"] > 0 else 1
            label = f"Updating: {title} (from chapter {start_ch})"
            cmds.append((label, self._build_update_cmd(s)))
        self._set_running(True)
        self.notebook.select(2)
        self._append_output(f"Updating {len(cmds)} series...\n")
        self._run_cmd_sequence(cmds)

    def _open_library_folder(self):
        selected = self.lib_tree.selection()
        folder = selected[0] if selected else self.output_dir.get()
        if sys.platform == "darwin":
            subprocess.Popen(["open", folder])
        elif sys.platform == "win32":
            os.startfile(folder)
        else:
            subprocess.Popen(["xdg-open", folder])

    # ── Lifecycle ────────────────────────────────────────────
    def on_close(self):
        self._save_current_prefs()
        if self.running_proc:
            self.stop_flag.set()
            try:
                self.running_proc.terminate()
            except Exception:
                pass
        self.root.destroy()


def main():
    if not AIO_DL.exists():
        print(f"Error: aio-dl.py not found at {AIO_DL}", file=sys.stderr)
        sys.exit(1)

    root = Tk()
    app = AIODownloaderGUI(root)
    root.protocol("WM_DELETE_WINDOW", app.on_close)

    # Load library on startup
    root.after(100, app._refresh_library)

    root.mainloop()


if __name__ == "__main__":
    main()
