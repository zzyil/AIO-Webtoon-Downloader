#!/usr/bin/env python3
"""
AIO Webtoon/Manga Downloader — GUI
Tkinter-based graphical interface for aio-dl.py.
"""

import json
import math
import os
import shlex
import signal
import subprocess
import sys
import threading
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
import tkinter.font as tkfont

import sites
from library_state import (
    build_update_chapters_arg,
    list_saved_books,
    scan_library,
)

try:
    from PIL import Image, ImageTk
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

REPO_DIR = Path(__file__).resolve().parent
AIO_DL = REPO_DIR / "aio-dl.py"
GUI_PREFS_FILE = REPO_DIR / ".gui_prefs.json"
BRAND_IMAGE = REPO_DIR / "AIO.png"
READ_ICON = "📖"
FOLDER_ICON = "📁"


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
        self._configure_style()
        self.root.title("AIO Webtoon/Manga Downloader")
        self.root.geometry("1160x820")
        self.root.minsize(920, 680)

        self.prefs = load_prefs()
        self.running_proc = None
        self.stop_flag = threading.Event()
        self.sequence_cancelled = False
        self.library_entries = []
        self.library_index = {}
        self.handler_names = sorted(
            {handler.name for handler in getattr(sites, "_REGISTERED_HANDLERS", [])}
        )
        self.brand_image = self._load_brand_image()

        # ── Variables ──
        self.output_dir = StringVar(value=self.prefs.get("output_dir", str(REPO_DIR / "comics")))
        self.chapters = StringVar(value="all")
        self.site_name = StringVar(value=self.prefs.get("site_name", ""))
        self.fmt = StringVar(value=self.prefs.get("format", "epub"))
        self.epub_layout = StringVar(value=self.prefs.get("epub_layout", "vertical"))
        self.language = StringVar(value=self.prefs.get("language", "en"))
        self.width = StringVar(value=str(self.prefs.get("width") or ""))
        self.aspect_ratio = StringVar(value=self.prefs.get("aspect_ratio") or "")
        self.quality = IntVar(value=int(self.prefs.get("quality", 85)))
        self.scaling = IntVar(value=int(self.prefs.get("scaling", 100)))
        self.cookies = StringVar(value=self.prefs.get("cookies", ""))
        self.group = StringVar(value=self.prefs.get("group", ""))
        self.split = StringVar(value=self.prefs.get("split", ""))
        self.jobs = IntVar(value=int(self.prefs.get("jobs", 1)))
        self.library_filter = StringVar(value="")
        self.status_text = StringVar(value="Ready")
        self.handler_count_text = StringVar(value=str(len(self.handler_names)))
        self.library_size_text = StringVar(value="0 B")
        self.library_overview_text = StringVar(value="Library snapshot: 0 series • 0 tracked for updates")
        self.library_detail = StringVar(value="Select a series to inspect its saved settings.")

        self.save_params = BooleanVar(value=bool(self.prefs.get("save_params", True)))
        self.keep_chapters = BooleanVar(value=self.prefs.get("keep_chapters", True))
        self.keep_images = BooleanVar(value=self.prefs.get("keep_images", False))
        self.mix_by_upvote = BooleanVar(value=self.prefs.get("mix_by_upvote", False))
        self.no_group_fallback = BooleanVar(value=self.prefs.get("no_group_fallback", False))
        self.no_partials = BooleanVar(value=self.prefs.get("no_partials", False))
        self.no_processing = BooleanVar(value=self.prefs.get("no_processing", False))
        self.no_cleanup = BooleanVar(value=self.prefs.get("no_cleanup", False))
        self.verbose = BooleanVar(value=self.prefs.get("verbose", True))
        self.debug_mode = BooleanVar(value=self.prefs.get("debug_mode", False))

        self._build_ui()
        self._bind_state()

    def _configure_style(self):
        style = ttk.Style(self.root)
        if "clam" in style.theme_names():
            style.theme_use("clam")

        self.colors = {
            "bg": "#e8eef5",
            "panel": "#fbfdff",
            "panel_soft": "#f3f7fb",
            "field": "#ffffff",
            "text": "#132033",
            "muted": "#5c6c80",
            "border": "#c7d2de",
            "hero": "#16273d",
            "hero_soft": "#223650",
            "hero_border": "#35506f",
            "hero_text": "#f8fafc",
            "hero_muted": "#cad5e2",
            "accent": "#0f766e",
            "accent_hover": "#115e59",
            "accent_active": "#0b4f4a",
            "danger": "#c2410c",
            "danger_hover": "#9a3412",
            "danger_active": "#7c2d12",
            "status_bg": "#d9e5f2",
            "output_bg": "#0f172a",
            "output_fg": "#e2e8f0",
            "editor_bg": "#122135",
            "editor_fg": "#f8fafc",
        }

        self.root.configure(background=self.colors["bg"])

        base_font = tkfont.nametofont("TkDefaultFont")
        text_font = tkfont.nametofont("TkTextFont")
        fixed_font = tkfont.nametofont("TkFixedFont")
        heading_font = tkfont.nametofont("TkHeadingFont")

        base_font.configure(size=11)
        text_font.configure(size=11)
        heading_font.configure(size=12, weight="bold")

        heading_family = heading_font.actual("family")
        body_family = base_font.actual("family")
        fixed_size = abs(int(fixed_font.actual("size"))) or 11

        self.title_font = tkfont.Font(family=heading_family, size=22, weight="bold")
        self.subtitle_font = tkfont.Font(family=body_family, size=11)
        self.metric_value_font = tkfont.Font(family=heading_family, size=15, weight="bold")
        self.metric_label_font = tkfont.Font(family=body_family, size=10)
        self.section_font = tkfont.Font(family=heading_family, size=11, weight="bold")
        self.button_font = tkfont.Font(family=body_family, size=10, weight="bold")
        self.mono_font = tkfont.Font(family=fixed_font.actual("family"), size=fixed_size)

        style.configure(".", background=self.colors["bg"], foreground=self.colors["text"])
        style.configure("App.TFrame", background=self.colors["bg"])
        style.configure("Card.TFrame", background=self.colors["panel"])
        style.configure(
            "Card.TLabelframe",
            background=self.colors["panel"],
            bordercolor=self.colors["border"],
            lightcolor=self.colors["border"],
            darkcolor=self.colors["border"],
            borderwidth=1,
            relief="solid",
        )
        style.configure(
            "Card.TLabelframe.Label",
            background=self.colors["panel"],
            foreground=self.colors["text"],
            font=self.section_font,
        )
        style.configure("TLabel", background=self.colors["bg"], foreground=self.colors["text"])
        style.configure("Section.TLabel", background=self.colors["bg"], foreground=self.colors["text"], font=self.section_font)
        style.configure("Muted.TLabel", background=self.colors["bg"], foreground=self.colors["muted"])
        style.configure("CardMuted.TLabel", background=self.colors["panel"], foreground=self.colors["muted"])
        style.configure("Hero.TFrame", background=self.colors["hero"])
        style.configure(
            "HeroInner.TFrame",
            background=self.colors["hero_soft"],
            bordercolor=self.colors["hero_border"],
            lightcolor=self.colors["hero_border"],
            darkcolor=self.colors["hero_border"],
            borderwidth=1,
            relief="solid",
        )
        style.configure("HeroTitle.TLabel", background=self.colors["hero"], foreground=self.colors["hero_text"], font=self.title_font)
        style.configure("HeroBody.TLabel", background=self.colors["hero"], foreground=self.colors["hero_muted"], font=self.subtitle_font)
        style.configure("HeroMeta.TLabel", background=self.colors["hero"], foreground=self.colors["hero_muted"])
        style.configure("HeroCard.TLabel", background=self.colors["hero_soft"], foreground=self.colors["hero_muted"])
        style.configure("HeroArtwork.TLabel", background=self.colors["hero"])
        style.configure(
            "Metric.TFrame",
            background=self.colors["hero_soft"],
            bordercolor=self.colors["hero_border"],
            lightcolor=self.colors["hero_border"],
            darkcolor=self.colors["hero_border"],
            borderwidth=1,
            relief="solid",
        )
        style.configure("MetricValue.TLabel", background=self.colors["hero_soft"], foreground=self.colors["hero_text"], font=self.metric_value_font)
        style.configure("MetricLabel.TLabel", background=self.colors["hero_soft"], foreground=self.colors["hero_muted"], font=self.metric_label_font)

        style.configure(
            "TButton",
            background=self.colors["panel_soft"],
            foreground=self.colors["text"],
            padding=(12, 9),
            bordercolor=self.colors["border"],
            lightcolor=self.colors["border"],
            darkcolor=self.colors["border"],
            font=self.button_font,
        )
        style.map(
            "TButton",
            background=[("active", "#e9eef4"), ("pressed", "#dfe7f0"), ("disabled", "#edf2f7")],
            foreground=[("disabled", "#8fa0b4")],
        )
        style.configure(
            "Accent.TButton",
            background=self.colors["accent"],
            foreground="#ffffff",
            bordercolor=self.colors["accent"],
            lightcolor=self.colors["accent"],
            darkcolor=self.colors["accent"],
            font=self.button_font,
            padding=(14, 9),
        )
        style.map(
            "Accent.TButton",
            background=[
                ("active", self.colors["accent_hover"]),
                ("pressed", self.colors["accent_active"]),
                ("disabled", "#8bb7b2"),
            ],
            foreground=[("disabled", "#ecfdf5")],
        )
        style.configure(
            "Danger.TButton",
            background=self.colors["danger"],
            foreground="#ffffff",
            bordercolor=self.colors["danger"],
            lightcolor=self.colors["danger"],
            darkcolor=self.colors["danger"],
            font=self.button_font,
            padding=(14, 9),
        )
        style.map(
            "Danger.TButton",
            background=[
                ("active", self.colors["danger_hover"]),
                ("pressed", self.colors["danger_active"]),
                ("disabled", "#d4b2a7"),
            ],
            foreground=[("disabled", "#fff7ed")],
        )

        style.configure(
            "TEntry",
            fieldbackground=self.colors["field"],
            foreground=self.colors["text"],
            bordercolor=self.colors["border"],
            lightcolor=self.colors["border"],
            darkcolor=self.colors["border"],
            padding=7,
        )
        style.configure(
            "TCombobox",
            fieldbackground=self.colors["field"],
            foreground=self.colors["text"],
            bordercolor=self.colors["border"],
            lightcolor=self.colors["border"],
            darkcolor=self.colors["border"],
            padding=6,
            arrowsize=14,
        )
        style.map(
            "TCombobox",
            fieldbackground=[("readonly", self.colors["field"])],
            selectforeground=[("readonly", self.colors["text"])],
            selectbackground=[("readonly", self.colors["field"])],
        )
        style.configure(
            "TSpinbox",
            fieldbackground=self.colors["field"],
            foreground=self.colors["text"],
            bordercolor=self.colors["border"],
            lightcolor=self.colors["border"],
            darkcolor=self.colors["border"],
            padding=6,
            arrowsize=12,
        )
        style.configure(
            "Flag.TCheckbutton",
            background=self.colors["panel"],
            foreground=self.colors["text"],
            padding=(0, 4),
        )
        style.map("Flag.TCheckbutton", background=[("active", self.colors["panel"])])

        style.configure("TNotebook", background=self.colors["bg"], borderwidth=0, tabmargins=(0, 0, 0, 0))
        style.configure(
            "TNotebook.Tab",
            background="#d7e0eb",
            foreground=self.colors["text"],
            padding=(16, 10),
            borderwidth=0,
            width=11,
            anchor="center",
        )
        style.map(
            "TNotebook.Tab",
            background=[("selected", self.colors["panel"]), ("active", "#e6edf5")],
            foreground=[("disabled", self.colors["muted"])],
            padding=[("selected", (16, 10)), ("active", (16, 10))],
            expand=[("selected", (0, 0, 0, 0)), ("active", (0, 0, 0, 0))],
        )

        style.configure(
            "Treeview",
            background=self.colors["field"],
            fieldbackground=self.colors["field"],
            foreground=self.colors["text"],
            bordercolor=self.colors["border"],
            lightcolor=self.colors["border"],
            darkcolor=self.colors["border"],
            rowheight=52 if HAS_PIL else 30,
        )
        style.configure("Treeview.Heading", background=self.colors["panel_soft"], foreground=self.colors["text"], font=self.section_font)
        style.map("Treeview", background=[("selected", "#d3ede9")], foreground=[("selected", self.colors["text"])])
        style.map("Treeview.Heading", background=[("active", "#e9eef5")])

        style.configure("StatusBar.TFrame", background=self.colors["status_bg"])
        style.configure("Status.TLabel", background=self.colors["status_bg"], foreground=self.colors["text"])

    # ── UI Construction ──────────────────────────────────────
    def _build_ui(self):
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)

        shell = ttk.Frame(self.root, style="App.TFrame", padding=(18, 18, 18, 12))
        shell.grid(row=0, column=0, sticky="nsew")
        shell.columnconfigure(0, weight=1)
        shell.rowconfigure(1, weight=1)

        hero = ttk.Frame(shell, style="Hero.TFrame", padding=(22, 20))
        hero.grid(row=0, column=0, sticky="ew")
        hero.columnconfigure(0, weight=1)

        hero_copy = ttk.Frame(hero, style="Hero.TFrame")
        hero_copy.grid(row=0, column=0, sticky="nsew")
        ttk.Label(hero_copy, text="AIO Webtoon/Manga Downloader", style="HeroTitle.TLabel").pack(anchor="w")
        ttk.Label(
            hero_copy,
            text="Queue multiple series, reload tracked titles, and keep the CLI power without fighting the layout.",
            style="HeroBody.TLabel",
            justify="left",
            wraplength=640,
        ).pack(anchor="w", pady=(6, 4))
        ttk.Label(hero_copy, textvariable=self.library_overview_text, style="HeroMeta.TLabel").pack(anchor="w", pady=(0, 14))

        output_card = ttk.Frame(hero_copy, style="HeroInner.TFrame", padding=(14, 12))
        output_card.pack(fill="x")
        output_card.columnconfigure(0, weight=1)
        ttk.Label(output_card, text="Output Directory", style="HeroCard.TLabel").grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 8))
        ttk.Entry(output_card, textvariable=self.output_dir).grid(row=1, column=0, sticky="ew", padx=(0, 10))
        ttk.Button(output_card, text="Browse", command=self._browse_output, style="Accent.TButton").grid(row=1, column=1, sticky="e")

        hero_aside = ttk.Frame(hero, style="Hero.TFrame")
        hero_aside.grid(row=0, column=1, sticky="ne", padx=(18, 0))
        if self.brand_image is not None:
            ttk.Label(hero_aside, image=self.brand_image, style="HeroArtwork.TLabel").pack(anchor="e")
        metric_row = ttk.Frame(hero_aside, style="Hero.TFrame")
        metric_row.pack(anchor="e", pady=(12, 0))
        self._build_metric_card(metric_row, self.handler_count_text, "Supported sites").pack(side="left", padx=(0, 10))
        self._build_metric_card(metric_row, self.library_size_text, "Library size").pack(side="left")

        self.notebook = ttk.Notebook(shell)
        self.notebook.grid(row=1, column=0, sticky="nsew", pady=(14, 0))

        self._build_download_tab()
        self._build_library_tab()
        self._build_output_tab()

        status_bar = ttk.Frame(self.root, style="StatusBar.TFrame", padding=(18, 10))
        status_bar.grid(row=1, column=0, sticky="ew")
        ttk.Label(status_bar, textvariable=self.status_text, anchor="w", style="Status.TLabel").pack(fill="x")

    def _bind_state(self):
        self.fmt.trace_add("write", lambda *_: self._sync_option_states())
        self.no_processing.trace_add("write", lambda *_: self._sync_option_states())
        self.library_filter.trace_add("write", lambda *_: self._render_library())
        self.notebook.bind("<<NotebookTabChanged>>", self._on_tab_changed)
        self._sync_option_states()

    def _build_download_tab(self):
        tab = ttk.Frame(self.notebook, style="App.TFrame")
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(0, weight=1)
        self.notebook.add(tab, text="Download")
        content, download_canvas = self._create_scrolled_frame(tab)
        content.columnconfigure(0, weight=1)
        content.columnconfigure(1, weight=1)

        url_frame = ttk.LabelFrame(content, text="Series Queue", style="Card.TLabelframe", padding=(14, 14))
        url_frame.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 12))
        url_frame.columnconfigure(0, weight=1)

        ttk.Label(
            url_frame,
            text="Paste one or more series URLs. Multi-URL jobs can run in parallel and reuse the same option set.",
            style="CardMuted.TLabel",
            justify="left",
            wraplength=900,
        ).grid(row=0, column=0, sticky="w", pady=(0, 10))

        self.url_entry = tk.Text(
            url_frame,
            height=6,
            wrap="word",
            relief="flat",
            bd=0,
            highlightthickness=1,
            highlightbackground=self.colors["border"],
            highlightcolor=self.colors["accent"],
            background=self.colors["editor_bg"],
            foreground=self.colors["editor_fg"],
            insertbackground=self.colors["hero_text"],
            selectbackground=self.colors["accent"],
            selectforeground=self.colors["hero_text"],
            font=self.mono_font,
            padx=12,
            pady=10,
        )
        self.url_entry.grid(row=1, column=0, sticky="ew")
        self._register_mousewheel_area(
            self.url_entry,
            y_scroll=lambda units: self._scroll_text_or_parent(
                self.url_entry,
                units,
                lambda parent_units: download_canvas.yview_scroll(parent_units, "units"),
            ),
            include_children=False,
        )

        basic = ttk.LabelFrame(content, text="Basic Options", style="Card.TLabelframe", padding=(14, 14))
        basic.grid(row=1, column=0, sticky="nsew", padx=(0, 8), pady=(0, 12))
        basic.columnconfigure(1, weight=1)

        r = 0
        ttk.Label(basic, text="Chapters").grid(row=r, column=0, sticky="w", pady=(0, 6))
        ttk.Entry(basic, textvariable=self.chapters).grid(row=r, column=1, sticky="ew", padx=(12, 0), pady=(0, 6))
        r += 1
        ttk.Label(basic, text="Examples: all, 5, 1-10, 1,3,5-7", style="CardMuted.TLabel").grid(row=r, column=1, sticky="w", padx=(12, 0), pady=(0, 10))

        r += 1
        ttk.Label(basic, text="Format").grid(row=r, column=0, sticky="w", pady=(0, 6))
        ttk.Combobox(
            basic,
            textvariable=self.fmt,
            values=["epub", "cbz", "pdf", "none"],
            state="readonly",
        ).grid(row=r, column=1, sticky="ew", padx=(12, 0), pady=(0, 6))

        r += 1
        ttk.Label(basic, text="EPUB Layout").grid(row=r, column=0, sticky="w", pady=(0, 6))
        self.epub_layout_combo = ttk.Combobox(
            basic,
            textvariable=self.epub_layout,
            values=["vertical", "page"],
            state="readonly",
        )
        self.epub_layout_combo.grid(row=r, column=1, sticky="ew", padx=(12, 0), pady=(0, 6))

        r += 1
        ttk.Label(basic, text="Language").grid(row=r, column=0, sticky="w", pady=(0, 6))
        ttk.Entry(basic, textvariable=self.language).grid(row=r, column=1, sticky="ew", padx=(12, 0), pady=(0, 6))

        r += 1
        ttk.Label(basic, text="Width").grid(row=r, column=0, sticky="w", pady=(0, 6))
        self.width_entry = ttk.Entry(basic, textvariable=self.width)
        self.width_entry.grid(row=r, column=1, sticky="ew", padx=(12, 0), pady=(0, 6))
        r += 1
        ttk.Label(basic, text="Optional resize target in pixels.", style="CardMuted.TLabel").grid(row=r, column=1, sticky="w", padx=(12, 0), pady=(0, 10))

        r += 1
        ttk.Label(basic, text="Aspect Ratio").grid(row=r, column=0, sticky="w", pady=(0, 6))
        self.aspect_ratio_entry = ttk.Entry(basic, textvariable=self.aspect_ratio)
        self.aspect_ratio_entry.grid(row=r, column=1, sticky="ew", padx=(12, 0), pady=(0, 6))
        r += 1
        ttk.Label(basic, text="Examples: 4:3 or 2.5", style="CardMuted.TLabel").grid(row=r, column=1, sticky="w", padx=(12, 0), pady=(0, 10))

        r += 1
        ttk.Label(basic, text="Quality").grid(row=r, column=0, sticky="w", pady=(0, 6))
        quality_frame = ttk.Frame(basic, style="Card.TFrame")
        quality_frame.grid(row=r, column=1, sticky="ew", padx=(12, 0), pady=(0, 6))
        quality_frame.columnconfigure(0, weight=1)
        self.quality_scale = ttk.Scale(quality_frame, from_=1, to=100, variable=self.quality, orient="horizontal")
        self.quality_scale.grid(row=0, column=0, sticky="ew")
        ttk.Label(quality_frame, textvariable=self.quality, width=4).grid(row=0, column=1, padx=(10, 0))

        r += 1
        ttk.Label(basic, text="Scaling").grid(row=r, column=0, sticky="w", pady=(0, 6))
        scale_frame = ttk.Frame(basic, style="Card.TFrame")
        scale_frame.grid(row=r, column=1, sticky="ew", padx=(12, 0), pady=(0, 0))
        scale_frame.columnconfigure(0, weight=1)
        self.scaling_scale = ttk.Scale(scale_frame, from_=1, to=100, variable=self.scaling, orient="horizontal")
        self.scaling_scale.grid(row=0, column=0, sticky="ew")
        ttk.Label(scale_frame, textvariable=self.scaling, width=4).grid(row=0, column=1, padx=(10, 0))

        advanced = ttk.LabelFrame(content, text="Advanced Options", style="Card.TLabelframe", padding=(14, 14))
        advanced.grid(row=1, column=1, sticky="nsew", padx=(8, 0), pady=(0, 12))
        advanced.columnconfigure(1, weight=1)

        r = 0
        ttk.Label(advanced, text="Site").grid(row=r, column=0, sticky="w", pady=(0, 6))
        self.site_combo = ttk.Combobox(advanced, textvariable=self.site_name, values=[""] + self.handler_names)
        self.site_combo.grid(row=r, column=1, sticky="ew", padx=(12, 0), pady=(0, 6))

        r += 1
        ttk.Label(advanced, text="Cookies").grid(row=r, column=0, sticky="w", pady=(0, 6))
        cookie_frame = ttk.Frame(advanced, style="Card.TFrame")
        cookie_frame.grid(row=r, column=1, sticky="ew", padx=(12, 0), pady=(0, 6))
        cookie_frame.columnconfigure(0, weight=1)
        ttk.Entry(cookie_frame, textvariable=self.cookies).grid(row=0, column=0, sticky="ew")
        ttk.Button(cookie_frame, text="Load", command=self._browse_cookies).grid(row=0, column=1, padx=(8, 0))
        r += 1
        ttk.Label(
            advanced,
            text="Paste a raw cookie string, or import a text file and its contents will be copied in.",
            style="CardMuted.TLabel",
            justify="left",
            wraplength=380,
        ).grid(row=r, column=1, sticky="w", padx=(12, 0), pady=(0, 10))

        r += 1
        ttk.Label(advanced, text="Group").grid(row=r, column=0, sticky="w", pady=(0, 6))
        ttk.Entry(advanced, textvariable=self.group).grid(row=r, column=1, sticky="ew", padx=(12, 0), pady=(0, 6))

        r += 1
        ttk.Label(advanced, text="Split").grid(row=r, column=0, sticky="w", pady=(0, 6))
        ttk.Entry(advanced, textvariable=self.split).grid(row=r, column=1, sticky="ew", padx=(12, 0), pady=(0, 6))

        r += 1
        ttk.Label(advanced, text="Jobs").grid(row=r, column=0, sticky="w", pady=(0, 6))
        ttk.Spinbox(advanced, textvariable=self.jobs, from_=1, to=8, width=6).grid(row=r, column=1, sticky="w", padx=(12, 0), pady=(0, 6))
        r += 1
        ttk.Label(
            advanced,
            text="Used for multi-URL downloads and update-all runs.",
            style="CardMuted.TLabel",
        ).grid(row=r, column=1, sticky="w", padx=(12, 0))

        checks = ttk.LabelFrame(content, text="Flags", style="Card.TLabelframe", padding=(14, 14))
        checks.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(0, 12))
        checks.columnconfigure(0, weight=1)
        checks.columnconfigure(1, weight=1)

        flag_rows = [
            ("Save params (for --update-all)", self.save_params),
            ("Keep individual chapter files", self.keep_chapters),
            ("Keep raw images", self.keep_images),
            ("Mix by upvote", self.mix_by_upvote),
            ("No group fallback", self.no_group_fallback),
            ("Skip fractional chapters", self.no_partials),
            ("No image processing", self.no_processing),
            ("Keep temp folders", self.no_cleanup),
            ("Verbose", self.verbose),
            ("Debug", self.debug_mode),
        ]
        for idx, (label, variable) in enumerate(flag_rows):
            row = idx // 2
            column = idx % 2
            ttk.Checkbutton(checks, text=label, variable=variable, style="Flag.TCheckbutton").grid(
                row=row,
                column=column,
                sticky="w",
                padx=(0, 18 if column == 0 else 0),
            )

        action_frame = ttk.LabelFrame(content, text="Actions", style="Card.TLabelframe", padding=(14, 12))
        action_frame.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(0, 6))
        action_frame.columnconfigure(1, weight=1)

        button_group = ttk.Frame(action_frame, style="Card.TFrame")
        button_group.grid(row=0, column=0, sticky="w")
        self.download_btn = ttk.Button(button_group, text="Download", command=self._start_download, style="Accent.TButton")
        self.download_btn.pack(side="left", padx=(0, 8))
        self.stop_btn = ttk.Button(button_group, text="Stop", command=self._stop_process, state="disabled", style="Danger.TButton")
        self.stop_btn.pack(side="left")
        ttk.Label(
            action_frame,
            text="Every command streams to the Output tab automatically. Stop cancels the active download or update batch.",
            style="CardMuted.TLabel",
            justify="left",
            wraplength=500,
        ).grid(row=0, column=1, sticky="e")
        self._register_mousewheel_area(
            download_canvas,
            y_scroll=lambda units: download_canvas.yview_scroll(units, "units"),
            include_children=False,
        )
        self._register_mousewheel_area(
            content,
            y_scroll=lambda units: download_canvas.yview_scroll(units, "units"),
            include_children=False,
        )

    def _build_library_tab(self):
        tab = ttk.Frame(self.notebook, style="App.TFrame", padding=(10, 10, 10, 10))
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(1, weight=1)
        self.notebook.add(tab, text="Library")
        self._cover_images = {}  # Keep references to prevent GC

        toolbar = ttk.Frame(tab, style="Card.TFrame", padding=(12, 12))
        toolbar.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        toolbar.columnconfigure(1, weight=1)

        actions = ttk.Frame(toolbar, style="Card.TFrame")
        actions.grid(row=0, column=0, sticky="w")
        ttk.Button(actions, text="Refresh", command=self._refresh_library).pack(side="left", padx=(0, 6))
        self.load_selected_btn = ttk.Button(
            actions,
            text="Load Selected",
            command=self._load_selected_into_form,
        )
        self.load_selected_btn.pack(side="left", padx=(0, 6))
        self.update_selected_btn = ttk.Button(actions, text="Update Selected", command=self._update_selected, style="Accent.TButton")
        self.update_selected_btn.pack(side="left", padx=(0, 6))
        self.update_all_btn = ttk.Button(actions, text="Update All", command=self._update_all)
        self.update_all_btn.pack(side="left")

        filters = ttk.Frame(toolbar, style="Card.TFrame")
        filters.grid(row=0, column=1, sticky="e")
        ttk.Button(filters, text="Open Folder", command=self._open_library_folder).pack(side="right")
        ttk.Entry(filters, textvariable=self.library_filter, width=26).pack(side="right", padx=(8, 0))
        ttk.Label(filters, text="Filter", style="CardMuted.TLabel").pack(side="right", padx=(0, 6))

        tree_frame = ttk.Frame(tab, style="Card.TFrame", padding=(2, 2, 2, 2))
        tree_frame.grid(row=1, column=0, sticky="nsew")
        tree_frame.columnconfigure(0, weight=1)
        tree_frame.rowconfigure(0, weight=1)

        columns = ("title", "status", "latest", "next", "format", "chapters", "files", "size", "read", "folder")
        show_mode = "tree headings" if HAS_PIL else "headings"
        self.lib_tree = ttk.Treeview(tree_frame, columns=columns, show=show_mode, selectmode="extended")

        if HAS_PIL:
            self.lib_tree.heading("#0", text="Cover")
            self.lib_tree.column("#0", width=64, minwidth=54, anchor="center", stretch=False)

        self.lib_tree.heading("title", text="Title")
        self.lib_tree.heading("status", text="Status")
        self.lib_tree.heading("latest", text="Latest")
        self.lib_tree.heading("next", text="Next Update")
        self.lib_tree.heading("format", text="Fmt")
        self.lib_tree.heading("chapters", text="Saved Ch")
        self.lib_tree.heading("files", text="Files")
        self.lib_tree.heading("size", text="Size")
        self.lib_tree.heading("read", text="Read")
        self.lib_tree.heading("folder", text="Folder")

        self.lib_tree.column("title", width=320, minwidth=200, stretch=True)
        self.lib_tree.column("status", width=90, minwidth=70, anchor="center")
        self.lib_tree.column("latest", width=80, minwidth=65, anchor="center")
        self.lib_tree.column("next", width=100, minwidth=85, anchor="center")
        self.lib_tree.column("format", width=45, minwidth=40, anchor="center")
        self.lib_tree.column("chapters", width=70, minwidth=60, anchor="center")
        self.lib_tree.column("files", width=40, minwidth=35, anchor="center")
        self.lib_tree.column("size", width=70, minwidth=55, anchor="e")
        self.lib_tree.column("read", width=42, minwidth=42, anchor="center", stretch=False)
        self.lib_tree.column("folder", width=42, minwidth=42, anchor="center", stretch=False)

        y_scroll = ttk.Scrollbar(tree_frame, orient="vertical", command=self.lib_tree.yview)
        x_scroll = ttk.Scrollbar(tree_frame, orient="horizontal", command=self.lib_tree.xview)
        self.lib_tree.configure(yscrollcommand=y_scroll.set, xscrollcommand=x_scroll.set)
        self.lib_tree.grid(row=0, column=0, sticky="nsew")
        y_scroll.grid(row=0, column=1, sticky="ns")
        x_scroll.grid(row=1, column=0, columnspan=2, sticky="ew")
        self._register_mousewheel_area(
            tree_frame,
            y_scroll=lambda units: self.lib_tree.yview_scroll(units, "units"),
            x_scroll=lambda units: self.lib_tree.xview_scroll(units, "units"),
        )
        self.lib_tree.bind("<Button-1>", self._on_library_click, add="+")
        self.lib_tree.bind("<<TreeviewSelect>>", self._on_library_select)
        self.lib_tree.bind("<Double-1>", lambda *_: self._load_selected_into_form())
        self.lib_tree.tag_configure("even", background="#ffffff")
        self.lib_tree.tag_configure("odd", background="#f6f9fd")
        self.lib_tree.tag_configure("untracked", foreground="#9a3412")

        self.library_detail_label = ttk.Label(
            tab,
            textvariable=self.library_detail,
            anchor="w",
            style="Muted.TLabel",
            justify="left",
        )
        self.library_detail_label.grid(row=2, column=0, sticky="ew", pady=(8, 4))
        self.library_detail_label.bind(
            "<Configure>",
            lambda event: self.library_detail_label.configure(wraplength=max(260, event.width - 20)),
        )

        self.lib_summary = StringVar(value="")
        ttk.Label(tab, textvariable=self.lib_summary, anchor="w", style="Muted.TLabel").grid(row=3, column=0, sticky="ew")

    def _build_output_tab(self):
        tab = ttk.Frame(self.notebook, style="App.TFrame", padding=(10, 10, 10, 10))
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(1, weight=1)
        self.notebook.add(tab, text="Output")

        header = ttk.Frame(tab, style="App.TFrame")
        header.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        header.columnconfigure(0, weight=1)
        ttk.Label(header, text="Live Command Output", style="Section.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(
            header,
            text="Downloads, updates, and exit codes stream here in real time.",
            style="Muted.TLabel",
        ).grid(row=1, column=0, sticky="w")
        ttk.Button(header, text="Clear", command=self._clear_output).grid(row=0, column=1, rowspan=2, sticky="e")

        output_card = ttk.Frame(tab, style="Card.TFrame", padding=(2, 2, 2, 2))
        output_card.grid(row=1, column=0, sticky="nsew")
        output_card.columnconfigure(0, weight=1)
        output_card.rowconfigure(0, weight=1)

        self.output_text = tk.Text(
            output_card,
            wrap="word",
            state="disabled",
            bg=self.colors["output_bg"],
            fg=self.colors["output_fg"],
            insertbackground=self.colors["hero_text"],
            selectbackground=self.colors["accent"],
            selectforeground=self.colors["hero_text"],
            font=self.mono_font,
            relief="flat",
            bd=0,
            padx=12,
            pady=10,
        )
        scrollbar = ttk.Scrollbar(output_card, orient="vertical", command=self.output_text.yview)
        self.output_text.configure(yscrollcommand=scrollbar.set)
        self.output_text.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")
        self._register_mousewheel_area(
            self.output_text,
            y_scroll=lambda units: self.output_text.yview_scroll(units, "units"),
            include_children=False,
        )

    def _build_metric_card(self, parent, value_var: StringVar, label: str) -> ttk.Frame:
        card = ttk.Frame(parent, style="Metric.TFrame", padding=(14, 12))
        ttk.Label(card, textvariable=value_var, style="MetricValue.TLabel").pack(anchor="w")
        ttk.Label(card, text=label, style="MetricLabel.TLabel").pack(anchor="w", pady=(2, 0))
        return card

    def _create_scrolled_frame(self, parent: ttk.Frame) -> tuple[ttk.Frame, tk.Canvas]:
        wrapper = ttk.Frame(parent, style="App.TFrame")
        wrapper.grid(row=0, column=0, sticky="nsew")
        wrapper.columnconfigure(0, weight=1)
        wrapper.rowconfigure(0, weight=1)

        canvas = tk.Canvas(
            wrapper,
            background=self.colors["bg"],
            highlightthickness=0,
            bd=0,
        )
        scrollbar = ttk.Scrollbar(wrapper, orient="vertical", command=canvas.yview)
        content = ttk.Frame(canvas, style="App.TFrame", padding=(10, 10, 10, 14))
        window_id = canvas.create_window((0, 0), window=content, anchor="nw")

        content.bind("<Configure>", lambda _event: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda event: canvas.itemconfigure(window_id, width=event.width))

        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")
        return content, canvas

    def _register_mousewheel_area(self, widget, y_scroll=None, x_scroll=None, include_children: bool = True):
        callbacks = {"y": y_scroll, "x": x_scroll}

        for target in self._iter_mousewheel_widgets(widget, include_children):
            target.bind("<MouseWheel>", lambda event, cb=callbacks: self._dispatch_mousewheel(cb, event), add="+")
            target.bind("<Shift-MouseWheel>", lambda event, cb=callbacks: self._dispatch_mousewheel(cb, event), add="+")
            target.bind("<Button-4>", lambda event, cb=callbacks: self._dispatch_mousewheel(cb, event), add="+")
            target.bind("<Button-5>", lambda event, cb=callbacks: self._dispatch_mousewheel(cb, event), add="+")
            target.bind("<Shift-Button-4>", lambda event, cb=callbacks: self._dispatch_mousewheel(cb, event), add="+")
            target.bind("<Shift-Button-5>", lambda event, cb=callbacks: self._dispatch_mousewheel(cb, event), add="+")

    def _iter_mousewheel_widgets(self, widget, include_children: bool):
        yield widget
        if not include_children:
            return
        for child in widget.winfo_children():
            yield from self._iter_mousewheel_widgets(child, True)

    def _dispatch_mousewheel(self, callbacks, event):
        units = self._mousewheel_units(event)
        if not units:
            return None

        horizontal = bool(event.state & 0x0001)
        callback = callbacks["x"] if horizontal and callbacks.get("x") else callbacks.get("y")
        if callback is None:
            return None

        callback(units)
        return "break"

    def _mousewheel_units(self, event) -> int:
        num = getattr(event, "num", None)
        if num == 4:
            return -1
        if num == 5:
            return 1

        delta = getattr(event, "delta", 0)
        if delta == 0:
            return 0
        if sys.platform == "darwin":
            return -1 if delta > 0 else 1

        steps = int(delta / 120)
        if steps:
            return -steps
        return -1 if delta > 0 else 1

    def _scroll_text_or_parent(self, text_widget: tk.Text, units: int, parent_scroll):
        first, last = text_widget.yview()
        if last - first >= 0.999:
            parent_scroll(units)
            return
        if units < 0 and first <= 0.0:
            parent_scroll(units)
            return
        if units > 0 and last >= 1.0:
            parent_scroll(units)
            return
        text_widget.yview_scroll(units, "units")

    def _load_brand_image(self):
        if not BRAND_IMAGE.exists():
            return None
        try:
            max_width = 150
            if HAS_PIL:
                image = Image.open(BRAND_IMAGE)
                ratio = min(max_width / image.width, 90 / image.height, 1)
                size = (
                    max(1, int(image.width * ratio)),
                    max(1, int(image.height * ratio)),
                )
                image = image.resize(size, Image.LANCZOS)
                return ImageTk.PhotoImage(image)

            image = tk.PhotoImage(file=str(BRAND_IMAGE))
            shrink = max(1, math.ceil(image.width() / max_width))
            return image.subsample(shrink, shrink)
        except Exception:
            return None

    # ── Actions ──────────────────────────────────────────────
    def _set_status(self, text: str):
        self.status_text.set(text)

    def _sync_option_states(self):
        is_epub = self.fmt.get() == "epub"
        processing_enabled = not self.no_processing.get()
        self.epub_layout_combo.configure(state="readonly" if is_epub else "disabled")
        self.quality_scale.configure(state="normal" if processing_enabled else "disabled")
        self.scaling_scale.configure(state="normal" if processing_enabled else "disabled")
        self.width_entry.configure(state="normal" if processing_enabled else "disabled")
        self.aspect_ratio_entry.configure(state="normal" if processing_enabled else "disabled")

    def _browse_output(self):
        d = filedialog.askdirectory(initialdir=self.output_dir.get())
        if d:
            self.output_dir.set(d)
            self._refresh_library()

    def _browse_cookies(self):
        f = filedialog.askopenfilename(filetypes=[("Text files", "*.txt"), ("All", "*.*")])
        if f:
            try:
                with open(f, "r", encoding="utf-8") as handle:
                    self.cookies.set(handle.read().strip())
                self._set_status(f"Loaded cookies from {os.path.basename(f)}")
            except Exception as exc:
                messagebox.showerror("Cookie Import Failed", f"Could not read cookie file:\n{exc}")

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
            "site_name": self.site_name.get(),
            "format": self.fmt.get(),
            "epub_layout": self.epub_layout.get(),
            "language": self.language.get(),
            "width": self.width.get(),
            "aspect_ratio": self.aspect_ratio.get(),
            "quality": self.quality.get(),
            "scaling": self.scaling.get(),
            "cookies": self.cookies.get(),
            "group": self.group.get(),
            "split": self.split.get(),
            "jobs": self.jobs.get(),
            "save_params": self.save_params.get(),
            "keep_chapters": self.keep_chapters.get(),
            "keep_images": self.keep_images.get(),
            "mix_by_upvote": self.mix_by_upvote.get(),
            "no_group_fallback": self.no_group_fallback.get(),
            "no_partials": self.no_partials.get(),
            "no_processing": self.no_processing.get(),
            "no_cleanup": self.no_cleanup.get(),
            "verbose": self.verbose.get(),
            "debug_mode": self.debug_mode.get(),
        })
        save_prefs(self.prefs)

    def _build_cmd(self, urls: list, chapters: str = None) -> list:
        cmd = [sys.executable, "-u", str(AIO_DL)]
        cmd.extend(urls)
        cmd.extend(["--chapters", chapters or self.chapters.get()])
        cmd.extend(["--format", self.fmt.get()])
        cmd.extend(["--language", self.language.get()])
        cmd.extend(["--output-dir", self.output_dir.get()])

        if self.site_name.get().strip():
            cmd.extend(["--site", self.site_name.get().strip()])
        if self.fmt.get() == "epub":
            cmd.extend(["--epub-layout", self.epub_layout.get()])
        if self.width.get().strip():
            cmd.extend(["--width", self.width.get().strip()])
        if self.aspect_ratio.get().strip():
            cmd.extend(["--aspect-ratio", self.aspect_ratio.get().strip()])
        if not self.no_processing.get():
            cmd.extend(["--quality", str(self.quality.get())])
            cmd.extend(["--scaling", str(self.scaling.get())])
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
        if self.no_group_fallback.get():
            cmd.append("--no-group-fallback")
        if self.no_partials.get():
            cmd.append("--no-partials")
        if self.no_processing.get():
            cmd.append("--no-processing")
        if self.no_cleanup.get():
            cmd.append("--no-cleanup")
        if self.verbose.get():
            cmd.append("--verbose")
        if self.debug_mode.get():
            cmd.append("--debug")
        return cmd

    def _set_running(self, running: bool):
        state = "disabled" if running else "normal"
        self.download_btn.configure(state=state)
        self.load_selected_btn.configure(state=state)
        self.update_selected_btn.configure(state=state)
        self.update_all_btn.configure(state=state)
        self.stop_btn.configure(state="normal" if running else "disabled")

    def _format_cmd_for_output(self, cmd: list) -> str:
        return shlex.join([str(part) for part in cmd])

    def _spawn_process(self, cmd: list) -> subprocess.Popen:
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        kwargs = {
            "cwd": str(REPO_DIR),
            "stdout": subprocess.PIPE,
            "stderr": subprocess.STDOUT,
            "encoding": "utf-8",
            "errors": "replace",
            "bufsize": 1,
            "env": env,
        }
        if os.name == "nt":
            creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            if creationflags:
                kwargs["creationflags"] = creationflags
        else:
            kwargs["start_new_session"] = True
        return subprocess.Popen(cmd, **kwargs)

    def _terminate_running_process(self, force: bool = False):
        proc = self.running_proc
        if not proc:
            return
        try:
            if os.name == "nt":
                if force:
                    proc.kill()
                else:
                    proc.terminate()
            else:
                sig = signal.SIGKILL if force else signal.SIGTERM
                os.killpg(os.getpgid(proc.pid), sig)
        except Exception:
            try:
                if force:
                    proc.kill()
                else:
                    proc.terminate()
            except Exception:
                pass

    def _run_cmd(self, cmd: list, on_done=None, status_text: str = "Running command..."):
        self.stop_flag.clear()
        self.sequence_cancelled = False
        self._set_running(True)
        self._set_status(status_text)
        self.notebook.select(2)
        self._append_output(f"$ {self._format_cmd_for_output(cmd)}\n\n")

        def _worker():
            stopped = False
            try:
                self.running_proc = self._spawn_process(cmd)
                for line in self.running_proc.stdout:
                    if self.stop_flag.is_set() and self.running_proc.poll() is None:
                        stopped = True
                        self._terminate_running_process()
                    self.root.after(0, self._append_output, line)
                code = self.running_proc.wait()
                if self.stop_flag.is_set():
                    stopped = True
                    self.root.after(0, self._append_output, "\n--- Stopped by user ---\n\n")
                else:
                    self.root.after(0, self._append_output, f"\n--- Process exited with code {code} ---\n\n")
            except Exception as exc:
                self.root.after(0, self._append_output, f"\n--- Error: {exc} ---\n\n")
            finally:
                self.running_proc = None
                self.root.after(0, self._set_running, False)
                self.root.after(0, self._set_status, "Stopped" if stopped else "Ready")
                if on_done:
                    self.root.after(0, on_done)

        threading.Thread(target=_worker, daemon=True).start()

    def _run_cmd_sequence(self, cmds: list, index: int = 0):
        if self.sequence_cancelled or index >= len(cmds):
            self._set_running(False)
            self._set_status("Ready")
            self._refresh_library()
            return

        label, cmd = cmds[index]
        self._append_output(f"\n{'=' * 60}\n{label}\n{'=' * 60}\n")

        def _worker():
            try:
                self.running_proc = self._spawn_process(cmd)
                for line in self.running_proc.stdout:
                    if self.stop_flag.is_set() and self.running_proc.poll() is None:
                        self.sequence_cancelled = True
                        self._terminate_running_process()
                    self.root.after(0, self._append_output, line)
                code = self.running_proc.wait()
                if self.stop_flag.is_set():
                    self.sequence_cancelled = True
                    self.root.after(0, self._append_output, "\n--- Stopped by user ---\n")
                else:
                    self.root.after(0, self._append_output, f"\n--- Exited with code {code} ---\n")
            except Exception as exc:
                self.sequence_cancelled = True
                self.root.after(0, self._append_output, f"\n--- Error: {exc} ---\n")
            finally:
                self.running_proc = None
                if self.sequence_cancelled or self.stop_flag.is_set():
                    self.root.after(0, self._set_running, False)
                    self.root.after(0, self._set_status, "Stopped")
                    self.root.after(0, self._refresh_library)
                else:
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
        self._run_cmd(
            cmd,
            on_done=self._refresh_library,
            status_text=f"Downloading {len(urls)} URL(s)...",
        )

    def _stop_process(self):
        self.stop_flag.set()
        self.sequence_cancelled = True
        self._set_status("Stopping...")
        self._terminate_running_process()

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
        self._set_status("Scanning library...")
        self.library_entries = scan_library(self.output_dir.get())
        self.library_index = {entry["folder"]: entry for entry in self.library_entries}
        tracked_count = sum(1 for entry in self.library_entries if entry.get("has_params"))
        total_size = sum(entry["size"] for entry in self.library_entries)
        self.library_size_text.set(format_size(total_size))
        if self.library_entries:
            self.library_overview_text.set(
                f"Library snapshot: {len(self.library_entries)} series • {tracked_count} tracked for updates"
            )
        else:
            self.library_overview_text.set("Library snapshot: no downloaded series yet")
        self._render_library()
        self._set_status("Ready")

    def _render_library(self):
        for item in self.lib_tree.get_children():
            self.lib_tree.delete(item)
        self._cover_images.clear()

        needle = self.library_filter.get().strip().lower()
        visible_entries = []
        total_size = 0

        for entry in self.library_entries:
            haystack = " ".join(
                [
                    entry["name"],
                    entry.get("url", ""),
                    entry.get("latest_chapter", ""),
                    entry.get("next_update", ""),
                ]
            ).lower()
            if needle and needle not in haystack:
                continue

            visible_entries.append(entry)
            thumb = self._load_cover_thumbnail(entry.get("cover"))
            kwargs = {}
            if thumb:
                self._cover_images[entry["folder"]] = thumb
                kwargs["image"] = thumb

            row_tags = ["even" if len(visible_entries) % 2 == 0 else "odd"]
            if not entry["has_params"]:
                row_tags.append("untracked")

            self.lib_tree.insert(
                "",
                END,
                iid=entry["folder"],
                text="",
                values=(
                    entry["name"],
                    "Tracked" if entry["has_params"] else "Untracked",
                    entry["latest_chapter"] or "-",
                    entry["next_update"],
                    entry["format"],
                    entry["chapters"],
                    entry["files"],
                    format_size(entry["size"]),
                    READ_ICON if entry.get("primary_book") else "",
                    FOLDER_ICON,
                ),
                tags=tuple(row_tags),
                **kwargs,
            )
            total_size += entry["size"]

        self.lib_summary.set(
            f"  {len(visible_entries)} shown / {len(self.library_entries)} series  |  {format_size(total_size)} visible"
        )
        self._on_library_select()

    def _on_tab_changed(self, _event=None):
        current_tab = self.notebook.tab(self.notebook.select(), "text")
        if current_tab == "Library" and not self.running_proc:
            self._refresh_library()

    def _tree_column_name(self, column_id: str) -> str:
        if not column_id or column_id == "#0":
            return column_id
        try:
            index = int(column_id[1:]) - 1
        except (TypeError, ValueError):
            return ""
        columns = self.lib_tree["columns"]
        if 0 <= index < len(columns):
            return columns[index]
        return ""

    def _on_library_click(self, event):
        if self.lib_tree.identify("region", event.x, event.y) != "cell":
            return None

        row_id = self.lib_tree.identify_row(event.y)
        column_name = self._tree_column_name(self.lib_tree.identify_column(event.x))
        if column_name not in {"read", "folder"} or not row_id:
            return None

        entry = self.library_index.get(row_id)
        if not entry:
            return "break"

        if column_name == "read":
            self._open_series_book(entry)
        else:
            self._open_path(entry["folder"])
        return "break"

    def _on_library_select(self, _event=None):
        selected = self.lib_tree.selection()
        if not selected:
            self.library_detail.set("Select a series to inspect its saved settings.")
            return

        entry = self.library_index.get(selected[0])
        if not entry:
            self.library_detail.set("Select a series to inspect its saved settings.")
            return

        params = entry.get("params", {})
        groups = params.get("group") or []
        if isinstance(groups, str):
            groups = [groups]
        group_text = ", ".join(groups) if groups else "none"
        url_text = entry.get("url") or "(no saved URL)"
        latest = entry.get("latest_chapter") or "-"
        detail = (
            f"URL: {url_text} | format: {params.get('format', '?')} | latest saved chapter: {latest} "
            f"| next update starts at: {entry.get('next_update', 'all')} | groups: {group_text}"
        )
        self.library_detail.set(detail)

    def _load_selected_into_form(self):
        selected = self.lib_tree.selection()
        if not selected:
            messagebox.showinfo("No Selection", "Select a tracked series from the library.")
            return

        entry = self.library_index.get(selected[0])
        if not entry or not entry.get("has_params"):
            messagebox.showwarning(
                "No Params",
                "The selected series does not have saved download parameters yet.",
            )
            return

        params = entry.get("params", {})
        self.url_entry.delete("1.0", END)
        if params.get("url"):
            self.url_entry.insert("1.0", params["url"])
        self.chapters.set("all")
        self.site_name.set(params.get("site", ""))
        self.fmt.set(params.get("format", "epub"))
        self.epub_layout.set(params.get("epub_layout", "vertical"))
        self.language.set(params.get("language", "en"))
        self.width.set(str(params.get("width") or ""))
        self.aspect_ratio.set(params.get("aspect_ratio") or "")
        self.quality.set(int(params.get("quality", 85)))
        self.scaling.set(int(params.get("scaling", 100)))
        self.cookies.set(params.get("cookies", ""))
        groups = params.get("group", [])
        if isinstance(groups, str):
            groups = [groups]
        self.group.set(", ".join(groups))
        self.split.set(params.get("split", ""))
        self.save_params.set(True)
        self.keep_chapters.set(bool(params.get("keep_chapters", True)))
        self.keep_images.set(bool(params.get("keep_images", False)))
        self.mix_by_upvote.set(bool(params.get("mix_by_upvote", False)))
        self.no_group_fallback.set(bool(params.get("no_group_fallback", False)))
        self.no_partials.set(bool(params.get("no_partials", False)))
        self.no_processing.set(bool(params.get("no_processing", False)))
        self.no_cleanup.set(bool(params.get("no_cleanup", False)))
        self.verbose.set(bool(params.get("verbose", True)))
        self.debug_mode.set(bool(params.get("debug", False)))
        self._sync_option_states()
        self.notebook.select(0)
        self._set_status(f"Loaded settings for {entry['name']}")

    def _get_selected_series(self) -> list:
        selected = self.lib_tree.selection()
        if not selected:
            messagebox.showinfo("No Selection", "Select one or more series from the library.")
            return []
        results = []
        for folder in selected:
            entry = self.library_index.get(folder)
            if not entry:
                continue
            params = entry.get("params", {})
            if not entry.get("has_params"):
                messagebox.showwarning(
                    "No Params",
                    f"'{entry['name']}' has no saved download parameters.\nDownload it first with 'Save params' enabled.",
                )
                continue
            if not params.get("url"):
                continue
            results.append(
                {
                    "folder": folder,
                    "params": params,
                    "chapter_numbers": entry.get("chapter_numbers", set()),
                    "next_update": entry.get("next_update", "all"),
                    "latest_chapter": entry.get("latest_chapter", ""),
                }
            )
        return results

    def _build_update_cmd(self, series: dict) -> list:
        p = series["params"]
        chapters_arg = series.get("next_update") or build_update_chapters_arg(
            series.get("chapter_numbers", set())
        )
        cmd = [
            sys.executable, "-u", str(AIO_DL), p["url"],
            "--chapters", chapters_arg,
            "--format", p.get("format", "epub"),
            "--language", p.get("language", "en"),
            "--output-dir", self.output_dir.get(),
            "--save-params", "--keep-chapters",
        ]
        if p.get("site"):
            cmd.extend(["--site", p["site"]])
        if p.get("epub_layout"):
            cmd.extend(["--epub-layout", p["epub_layout"]])
        if p.get("width"):
            cmd.extend(["--width", str(p["width"])])
        if p.get("aspect_ratio"):
            cmd.extend(["--aspect-ratio", str(p["aspect_ratio"])])
        if not p.get("no_processing"):
            cmd.extend(["--quality", str(p.get("quality", 85))])
            cmd.extend(["--scaling", str(p.get("scaling", 100))])
        if p.get("cookies"):
            cmd.extend(["--cookies", p["cookies"]])
        if p.get("group"):
            groups = p["group"]
            if isinstance(groups, str):
                groups = [groups]
            for g in groups:
                cmd.extend(["--group", g])
        if p.get("split"):
            cmd.extend(["--split", str(p["split"])])
        if p.get("mix_by_upvote"):
            cmd.append("--mix-by-upvote")
        if p.get("no_group_fallback"):
            cmd.append("--no-group-fallback")
        if p.get("no_partials"):
            cmd.append("--no-partials")
        if p.get("keep_images"):
            cmd.append("--keep-images")
        if p.get("no_processing"):
            cmd.append("--no-processing")
        if p.get("no_cleanup"):
            cmd.append("--no-cleanup")
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
            start_ch = s.get("next_update", "all")
            label = f"Updating: {title} (from {start_ch})"
            cmds.append((label, self._build_update_cmd(s)))
        self._set_running(True)
        self.stop_flag.clear()
        self.sequence_cancelled = False
        self._set_status(f"Updating {len(cmds)} selected series...")
        self.notebook.select(2)
        self._run_cmd_sequence(cmds)

    def _update_all(self):
        entries = [
            entry
            for entry in scan_library(self.output_dir.get())
            if entry.get("has_params") and entry.get("url")
        ]
        if not entries:
            messagebox.showinfo("Nothing to Update", "No series with saved parameters found.")
            return
        self._save_current_prefs()
        self._clear_output()
        cmd = [sys.executable, "-u", str(AIO_DL), "--update-all", "--output-dir", self.output_dir.get()]
        if self.jobs.get() > 1:
            cmd.extend(["--jobs", str(self.jobs.get())])
        self._run_cmd(
            cmd,
            on_done=self._refresh_library,
            status_text=f"Updating all tracked series ({len(entries)})...",
        )

    def _open_library_folder(self):
        selected = self.lib_tree.selection()
        folder = selected[0] if selected else self.output_dir.get()
        self._open_path(folder)

    def _open_path(self, path: str):
        if sys.platform == "darwin":
            subprocess.Popen(["open", path])
        elif sys.platform == "win32":
            os.startfile(path)
        else:
            subprocess.Popen(["xdg-open", path])

    def _find_primary_book(self, entry: dict) -> str:
        primary_book = entry.get("primary_book") or ""
        if primary_book and os.path.isfile(primary_book):
            return primary_book

        books = list_saved_books(entry["folder"])
        if books:
            primary_book = books[0]
            entry["primary_book"] = primary_book
            return primary_book
        return ""

    def _open_series_book(self, entry: dict):
        book_path = self._find_primary_book(entry)
        if not book_path:
            messagebox.showinfo(
                "No Book Found",
                f"No saved book file was found for '{entry['name']}'.",
            )
            return

        self._open_path(book_path)

    # ── Lifecycle ────────────────────────────────────────────
    def on_close(self):
        self._save_current_prefs()
        if self.running_proc:
            self.stop_flag.set()
            self.sequence_cancelled = True
            self._terminate_running_process(force=True)
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
