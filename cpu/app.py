"""
ImageProcessor — batch background-removal + compositing tool.

Features:
- GUI with drag-and-drop (links.txt or image files) and CLI mode
- Pause/Stop with graceful drain of in-flight work
- Persistent settings in %APPDATA%\\ImageProcessor\\config.json
- Per-stage progress bars (download / process / save)
- Live preview of the last saved result
- Retry with exponential backoff for flaky downloads
- Settings dialog for workers, sizes, retry policy

Run modes:
    python app.py                                      # GUI
    python app.py --links links.txt --bg bg.jpg        # CLI batch
    python app.py --images a.jpg b.jpg --bg bg.jpg     # CLI on local files
"""

import os
import sys
import json
import time
import argparse
import threading
from pathlib import Path
from io import BytesIO
from queue import Queue, Empty
from threading import Thread

import requests
from PIL import Image, ImageTk
import rembg

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

from tkinterdnd2 import DND_FILES, TkinterDnD


# ================= DEFAULTS =================

DEFAULT_CONFIG = {
    "output": str(Path.home() / "Desktop" / "ImageProcessorOutput"),
    "mode": "AUTO",
    "model": "u2net",
    "max_input_size": 512,
    "upscale_factor": 0.85,
    "bg_width": 900,
    "bg_height": 1200,
    "download_workers": 4,
    "process_workers": 2,
    "save_workers": 1,
    "retry_attempts": 3,
    "retry_delay": 1.0,
    "window_geometry": "950x720",
}

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tiff", ".tif"}


# ================= CONFIG PERSISTENCE =================

def get_config_path():
    appdata = os.environ.get("APPDATA") or str(Path.home())
    d = Path(appdata) / "ImageProcessor"
    d.mkdir(parents=True, exist_ok=True)
    return d / "config.json"


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    p = get_config_path()
    if p.exists():
        try:
            saved = json.loads(p.read_text(encoding="utf-8"))
            cfg.update(saved)
        except Exception:
            pass
    return cfg


def save_config(cfg):
    try:
        get_config_path().write_text(
            json.dumps(cfg, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception:
        pass  # don't crash app on config save failure


# ================= MODEL CACHE =================

session_cache = {}
session_lock = threading.Lock()


def get_session(name):
    with session_lock:
        if name not in session_cache:
            session_cache[name] = rembg.new_session(name)
        return session_cache[name]


def choose_model_fast(img):
    pixels = img.resize((30, 30))
    data = list(pixels.getdata())
    avg = sum(sum(p[:3]) for p in data) / (30 * 30 * 3)
    return "u2net" if avg > 200 else "isnet-general-use"


# ================= INPUT PARSING =================

def parse_links(path):
    entries = []
    current = None
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        if line.endswith(":"):
            current = line[:-1]
        elif line.startswith("http"):
            if current is None:
                current = "unknown"
            entries.append((current, line))
    return entries


def parse_dropped_paths(root, data):
    """tkinterdnd2 returns space-separated paths, with {} around any with spaces."""
    try:
        return list(root.tk.splitlist(data))
    except Exception:
        # fallback
        return [data.strip("{}").strip()]


def smart_upscale(subject, orig_size, factor):
    return subject.resize(
        (int(orig_size[0] * factor), int(orig_size[1] * factor)),
        Image.LANCZOS,
    )


# ================= PIPELINE =================

class Pipeline:
    """All the heavy lifting, decoupled from the GUI.

    Communicates with the caller through callbacks. Callbacks are invoked
    from worker threads, so a GUI caller must marshal them onto the main
    thread (see App._wrap_callbacks).
    """

    def __init__(self, links_path, image_paths, background, output,
                 model_choice, settings, callbacks):
        self.links_path = links_path
        self.image_paths = image_paths or []
        self.background = background
        self.output = Path(output)
        self.model_choice = model_choice
        self.s = settings
        self.cb = callbacks  # dict with 'log', 'progress', 'preview', 'finished'

        self.stop_event = threading.Event()

        # Per-stage counters; (done, total)
        self.totals = {"download": 0, "process": 0, "save": 0}
        self.done = {"download": 0, "process": 0, "save": 0}
        self._counter_lock = threading.Lock()

        self._log_lock = threading.Lock()
        self._processed_lock = threading.Lock()

    # ------- public -------

    def stop(self):
        self.stop_event.set()
        self.cb["log"]("Stop requested — finishing in-flight tasks...")

    def run(self):
        try:
            self._run_inner()
        except Exception as e:
            self.cb["log"](f"FATAL: {e}")
        finally:
            self.cb["finished"]()

    # ------- internals -------

    def _log(self, msg):
        self.cb["log"](msg)
        try:
            with self._log_lock:
                with open(self.output / "errors.log", "a", encoding="utf-8") as f:
                    f.write(msg + "\n")
        except Exception:
            pass

    def _bump(self, stage):
        with self._counter_lock:
            self.done[stage] += 1
            self.cb["progress"](stage, self.done[stage], self.totals[stage])

    def _load_processed(self):
        f = self.output / "processed.txt"
        if not f.exists():
            return set()
        try:
            return set(f.read_text(encoding="utf-8").splitlines())
        except Exception:
            return set()

    def _save_processed(self, key):
        try:
            with self._processed_lock:
                with open(self.output / "processed.txt", "a", encoding="utf-8") as f:
                    f.write(key + "\n")
        except Exception:
            pass

    def _get_index(self, folder, art):
        if not folder.exists():
            return 0
        nums = []
        for f in folder.glob(f"{art}*.png"):
            try:
                nums.append(int(f.stem.replace(art, "")))
            except Exception:
                pass
        return max(nums) if nums else 0

    def _build_tasks(self):
        """Returns list of (art, idx, source_kind, source_value, key)."""
        self.output.mkdir(parents=True, exist_ok=True)
        processed = self._load_processed()

        raw = []  # (art, kind, value, key)

        if self.links_path:
            for art, url in parse_links(self.links_path):
                key = f"url:{url}"
                if key in processed:
                    continue
                raw.append((art, "url", url, key))

        for path in self.image_paths:
            p = Path(path)
            if not p.exists():
                continue
            art = p.parent.name or "images"
            key = f"file:{p.resolve()}"
            if key in processed:
                continue
            raw.append((art, "file", str(p), key))

        # Assign per-art indices that continue from existing files on disk.
        counter = {}
        tasks = []
        for art, kind, value, key in raw:
            if art not in counter:
                counter[art] = self._get_index(self.output / art, art)
            counter[art] += 1
            tasks.append((art, counter[art], kind, value, key))

        return tasks

    def _download_one(self, session, url):
        """Download with retry + exponential backoff. Honors stop_event."""
        last_err = None
        for attempt in range(self.s["retry_attempts"]):
            if self.stop_event.is_set():
                return None
            try:
                r = session.get(url, timeout=15)
                r.raise_for_status()
                return Image.open(BytesIO(r.content)).convert("RGBA")
            except Exception as e:
                last_err = e
                if attempt < self.s["retry_attempts"] - 1:
                    time.sleep(self.s["retry_delay"] * (2 ** attempt))
        raise last_err if last_err else Exception("download failed")

    def _run_inner(self):
        tasks = self._build_tasks()
        n = len(tasks)
        self.totals["download"] = self.totals["process"] = self.totals["save"] = n

        for stage in ("download", "process", "save"):
            self.cb["progress"](stage, 0, n)

        if n == 0:
            self._log("Nothing to do (no new tasks)")
            return

        try:
            bg = (
                Image.open(self.background)
                .convert("RGBA")
                .resize((self.s["bg_width"], self.s["bg_height"]))
            )
        except Exception as e:
            self._log(f"BACKGROUND ERROR: {e}")
            return

        self._log(f"Starting: {n} tasks")

        q1, q2, q3 = Queue(), Queue(), Queue()

        # ----- workers -----

        def download_worker():
            s = requests.Session()
            while True:
                item = q1.get()
                try:
                    if item is None:
                        break
                    if self.stop_event.is_set():
                        # drain without working
                        continue
                    art, idx, kind, value, key = item
                    img = None
                    try:
                        if kind == "url":
                            img = self._download_one(s, value)
                        else:  # file
                            img = Image.open(value).convert("RGBA")
                    except Exception as e:
                        self._log(f"DOWNLOAD FAIL: {value} | {e}")
                    self._bump("download")
                    q2.put((art, idx, key, value, img))
                finally:
                    q1.task_done()

        def process_worker():
            while True:
                item = q2.get()
                try:
                    if item is None:
                        break
                    if self.stop_event.is_set():
                        self._bump("process")
                        continue
                    art, idx, key, value, img = item
                    subject = None
                    try:
                        if img is None:
                            raise Exception("no image")
                        orig = img.size
                        img.thumbnail((self.s["max_input_size"], self.s["max_input_size"]))
                        model = self.model_choice
                        if model == "AUTO":
                            model = choose_model_fast(img)
                        session = get_session(model)
                        subject = rembg.remove(img, session=session)
                        subject = smart_upscale(subject, orig, self.s["upscale_factor"])
                    except Exception as e:
                        self._log(f"PROCESS FAIL: {value} | {e}")
                    self._bump("process")
                    q3.put((art, idx, key, value, subject))
                finally:
                    q2.task_done()

        def save_worker():
            while True:
                item = q3.get()
                try:
                    if item is None:
                        break
                    if self.stop_event.is_set():
                        self._bump("save")
                        continue
                    art, idx, key, value, subject = item
                    try:
                        if subject is not None:
                            canvas = bg.copy()
                            scale = min(
                                canvas.size[0] / subject.size[0],
                                canvas.size[1] / subject.size[1],
                            )
                            sub = subject.resize(
                                (int(subject.size[0] * scale),
                                 int(subject.size[1] * scale))
                            )
                            x = (canvas.size[0] - sub.size[0]) // 2
                            y = (canvas.size[1] - sub.size[1]) // 2
                            canvas.paste(sub, (x, y), sub)
                            folder = self.output / art
                            folder.mkdir(exist_ok=True)
                            out_path = folder / f"{art}{idx}.png"
                            canvas.save(out_path)
                            self._save_processed(key)
                            self.cb["preview"](canvas, out_path.name)
                    except Exception as e:
                        self._log(f"SAVE FAIL: {value} | {e}")
                    self._bump("save")
                finally:
                    q3.task_done()

        # ----- spin up -----

        threads = []
        for _ in range(self.s["download_workers"]):
            t = Thread(target=download_worker, daemon=True)
            t.start()
            threads.append(t)
        for _ in range(self.s["process_workers"]):
            t = Thread(target=process_worker, daemon=True)
            t.start()
            threads.append(t)
        for _ in range(self.s["save_workers"]):
            t = Thread(target=save_worker, daemon=True)
            t.start()
            threads.append(t)

        for task in tasks:
            q1.put(task)

        # poison pills
        for _ in range(self.s["download_workers"]):
            q1.put(None)
        q1.join()
        for _ in range(self.s["process_workers"]):
            q2.put(None)
        q2.join()
        for _ in range(self.s["save_workers"]):
            q3.put(None)
        q3.join()

        self._log("STOPPED" if self.stop_event.is_set() else "DONE")


# ================= GUI =================

class SettingsDialog(tk.Toplevel):
    """Modal settings editor."""

    FIELDS = [
        ("download_workers", "Download workers", int, 1, 16),
        ("process_workers", "Process workers", int, 1, 8),
        ("save_workers", "Save workers", int, 1, 4),
        ("max_input_size", "Max input size (px)", int, 64, 4096),
        ("upscale_factor", "Upscale factor", float, 0.1, 4.0),
        ("bg_width", "Background width (px)", int, 100, 8000),
        ("bg_height", "Background height (px)", int, 100, 8000),
        ("retry_attempts", "Retry attempts", int, 1, 10),
        ("retry_delay", "Retry delay (sec)", float, 0.0, 30.0),
    ]

    def __init__(self, parent, config, on_save):
        super().__init__(parent)
        self.title("Settings")
        self.transient(parent)
        self.grab_set()
        self.resizable(False, False)

        self.config_in = config
        self.on_save = on_save
        self.vars = {}

        body = tk.Frame(self, padx=15, pady=15)
        body.pack()

        for row, (key, label, type_, lo, hi) in enumerate(self.FIELDS):
            tk.Label(body, text=label).grid(row=row, column=0, sticky="w", pady=3)
            v = tk.StringVar(value=str(config.get(key, DEFAULT_CONFIG[key])))
            self.vars[key] = (v, type_, lo, hi)
            tk.Entry(body, textvariable=v, width=12).grid(row=row, column=1, padx=10)

        btns = tk.Frame(self, pady=10)
        btns.pack()
        ttk.Button(btns, text="Save", command=self._save).pack(side="left", padx=5)
        ttk.Button(btns, text="Cancel", command=self.destroy).pack(side="left", padx=5)
        ttk.Button(btns, text="Reset to defaults", command=self._reset).pack(side="left", padx=5)

    def _reset(self):
        for key, (v, *_), in self.vars.items():
            v.set(str(DEFAULT_CONFIG[key]))

    def _save(self):
        out = {}
        for key, (v, type_, lo, hi) in self.vars.items():
            try:
                val = type_(v.get())
            except ValueError:
                messagebox.showerror("Bad value", f"{key} must be {type_.__name__}", parent=self)
                return
            if val < lo or val > hi:
                messagebox.showerror("Out of range", f"{key} must be between {lo} and {hi}", parent=self)
                return
            out[key] = val
        self.on_save(out)
        self.destroy()


class App:

    def __init__(self, root, cfg, prefilled=None):
        self.root = root
        self.cfg = cfg

        self.root.title("Image Processor")
        self.root.geometry(cfg.get("window_geometry", DEFAULT_CONFIG["window_geometry"]))
        self.root.minsize(750, 600)

        # State
        self.links_path = ""
        self.image_paths = []
        self.bg_path = ""
        self.output = cfg["output"]
        self.mode = tk.StringVar(value=cfg.get("mode", "AUTO"))
        self.pipeline = None
        self._preview_imgtk = None  # keep ref so PhotoImage isn't GC'd

        self._build()

        # Apply prefilled CLI values to GUI
        if prefilled:
            if prefilled.get("links"):
                self._set_links(prefilled["links"])
            if prefilled.get("images"):
                self._set_images(prefilled["images"])
            if prefilled.get("bg"):
                self._set_bg(prefilled["bg"])
            if prefilled.get("output"):
                self.output = prefilled["output"]
                self.output_label.config(text=self.output)

        # Save geometry on close
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ------- layout -------

    def _build(self):
        outer = tk.Frame(self.root)
        outer.pack(fill="both", expand=True, padx=10, pady=10)

        # ===== Left column: controls =====
        left = tk.Frame(outer)
        left.pack(side="left", fill="both", expand=True)

        self.input_label = tk.Label(
            left,
            text="DROP links.txt OR images HERE",
            relief="ridge", height=3,
        )
        self.input_label.pack(fill="x", pady=4)
        self.input_label.drop_target_register(DND_FILES)
        self.input_label.dnd_bind("<<Drop>>", self._on_drop_input)

        self.bg_label = tk.Label(
            left, text="DROP BACKGROUND HERE",
            relief="ridge", height=3,
        )
        self.bg_label.pack(fill="x", pady=4)
        self.bg_label.drop_target_register(DND_FILES)
        self.bg_label.dnd_bind("<<Drop>>", self._on_drop_bg)

        self.output_label = tk.Label(left, text=self.output, relief="ridge")
        self.output_label.pack(fill="x", pady=4)

        row = tk.Frame(left)
        row.pack(fill="x", pady=4)
        ttk.Button(row, text="Choose Output", command=self._pick_output).pack(side="left", expand=True, fill="x", padx=2)
        ttk.Button(row, text="Settings...", command=self._open_settings).pack(side="left", expand=True, fill="x", padx=2)

        # Mode
        mode_box = tk.LabelFrame(left, text="Mode", padx=5, pady=5)
        mode_box.pack(fill="x", pady=4)
        ttk.Radiobutton(mode_box, text="AUTO  (pick model per image)", variable=self.mode, value="AUTO").pack(anchor="w")
        ttk.Radiobutton(mode_box, text="MANUAL (use selected below)", variable=self.mode, value="MANUAL").pack(anchor="w")
        self.combo = ttk.Combobox(
            mode_box, state="readonly",
            values=["u2net", "isnet-general-use", "birefnet-general-lite",
                    "silueta", "isnet-anime"],
        )
        self.combo.set(self.cfg.get("model", "u2net"))
        self.combo.pack(fill="x", pady=3)

        # Per-stage progress
        prog_box = tk.LabelFrame(left, text="Progress", padx=5, pady=5)
        prog_box.pack(fill="x", pady=4)
        self.progress = {}
        self.progress_labels = {}
        for stage in ("download", "process", "save"):
            row = tk.Frame(prog_box)
            row.pack(fill="x", pady=1)
            tk.Label(row, text=stage.capitalize().ljust(9), width=10, anchor="w").pack(side="left")
            self.progress[stage] = ttk.Progressbar(row)
            self.progress[stage].pack(side="left", fill="x", expand=True, padx=4)
            self.progress_labels[stage] = tk.Label(row, text="0/0", width=8, anchor="e")
            self.progress_labels[stage].pack(side="left")

        # Buttons (anchored bottom so they never get clipped)
        btn_row = tk.Frame(left)
        btn_row.pack(side="bottom", fill="x", pady=8)
        self.start_btn = ttk.Button(btn_row, text="START", command=self._start)
        self.start_btn.pack(side="left", expand=True, fill="x", padx=2)
        self.stop_btn = ttk.Button(btn_row, text="STOP", command=self._stop, state="disabled")
        self.stop_btn.pack(side="left", expand=True, fill="x", padx=2)

        # Log (fills the middle)
        log_frame = tk.Frame(left)
        log_frame.pack(fill="both", expand=True, pady=4)
        log_scroll = ttk.Scrollbar(log_frame, orient="vertical")
        log_scroll.pack(side="right", fill="y")
        self.log = tk.Text(log_frame, height=8, yscrollcommand=log_scroll.set)
        self.log.pack(side="left", fill="both", expand=True)
        log_scroll.config(command=self.log.yview)

        # ===== Right column: preview =====
        right = tk.Frame(outer, width=300)
        right.pack(side="right", fill="y", padx=(10, 0))
        right.pack_propagate(False)

        tk.Label(right, text="Last result").pack(anchor="w")
        self.preview = tk.Label(
            right, text="(no preview yet)\n\nLast saved image\nappears here.",
            relief="sunken", bg="#222", fg="#aaa",
            width=30, height=20,
        )
        self.preview.pack(fill="both", expand=True, pady=4)
        self.preview_name = tk.Label(right, text="", anchor="w")
        self.preview_name.pack(fill="x")

    # ------- thread-safe UI bridges -------

    def _ui_log(self, msg):
        self.root.after(0, self._log_main, msg)

    def _log_main(self, msg):
        try:
            self.log.insert("end", msg + "\n")
            self.log.see("end")
        except tk.TclError:
            pass

    def _ui_progress(self, stage, done, total):
        self.root.after(0, self._progress_main, stage, done, total)

    def _progress_main(self, stage, done, total):
        try:
            pct = (done / total * 100) if total else 0
            self.progress[stage]["value"] = pct
            self.progress_labels[stage].config(text=f"{done}/{total}")
        except tk.TclError:
            pass

    def _ui_preview(self, pil_img, name):
        self.root.after(0, self._preview_main, pil_img, name)

    def _preview_main(self, pil_img, name):
        try:
            w = max(self.preview.winfo_width(), 100)
            h = max(self.preview.winfo_height(), 100)
            img = pil_img.copy()
            img.thumbnail((w - 10, h - 10))
            self._preview_imgtk = ImageTk.PhotoImage(img)
            self.preview.config(image=self._preview_imgtk, text="")
            self.preview_name.config(text=name)
        except tk.TclError:
            pass

    def _ui_finished(self):
        self.root.after(0, self._finished_main)

    def _finished_main(self):
        try:
            self.start_btn.config(state="normal", text="START")
            self.stop_btn.config(state="disabled")
        except tk.TclError:
            pass
        self.pipeline = None

    # ------- drag handlers -------

    def _on_drop_input(self, event):
        paths = parse_dropped_paths(self.root, event.data)
        txts = [p for p in paths if p.lower().endswith(".txt")]
        imgs = [p for p in paths if Path(p).suffix.lower() in IMAGE_EXTS]

        if txts:
            self._set_links(txts[0])
        if imgs:
            self._set_images(imgs)
        if not txts and not imgs:
            messagebox.showwarning("Unrecognised", "Drop a .txt file or image files.")

    def _on_drop_bg(self, event):
        paths = parse_dropped_paths(self.root, event.data)
        if paths:
            self._set_bg(paths[0])

    def _set_links(self, path):
        self.links_path = path
        self.image_paths = []
        self.input_label.config(text=f"Links: {os.path.basename(path)}")
        self._log_main(f"Links: {path}")

    def _set_images(self, paths):
        self.image_paths = paths
        self.links_path = ""
        if len(paths) == 1:
            label = f"Image: {os.path.basename(paths[0])}"
        else:
            label = f"{len(paths)} images selected"
        self.input_label.config(text=label)
        self._log_main(f"Images: {len(paths)}")

    def _set_bg(self, path):
        self.bg_path = path
        self.bg_label.config(text=f"BG: {os.path.basename(path)}")
        self._log_main(f"Background: {path}")

    # ------- actions -------

    def _pick_output(self):
        path = filedialog.askdirectory(initialdir=self.output)
        if path:
            self.output = path
            self.output_label.config(text=path)

    def _open_settings(self):
        SettingsDialog(self.root, self.cfg, self._apply_settings)

    def _apply_settings(self, new_values):
        self.cfg.update(new_values)
        save_config(self.cfg)
        self._log_main("Settings saved")

    def _start(self):
        if self.pipeline is not None:
            return
        if not (self.links_path or self.image_paths):
            messagebox.showerror("Missing input", "Drop links.txt or images first.")
            return
        if not self.bg_path:
            messagebox.showerror("Missing background", "Drop a background image first.")
            return

        # Persist current settings
        self.cfg["output"] = self.output
        self.cfg["mode"] = self.mode.get()
        self.cfg["model"] = self.combo.get()
        save_config(self.cfg)

        model = self.combo.get() if self.mode.get() == "MANUAL" else "AUTO"

        # Reset progress UI
        for stage in ("download", "process", "save"):
            self.progress[stage]["value"] = 0
            self.progress_labels[stage].config(text="0/0")

        self.start_btn.config(state="disabled", text="WORKING...")
        self.stop_btn.config(state="normal")

        callbacks = {
            "log": self._ui_log,
            "progress": self._ui_progress,
            "preview": self._ui_preview,
            "finished": self._ui_finished,
        }

        self.pipeline = Pipeline(
            links_path=self.links_path or None,
            image_paths=self.image_paths,
            background=self.bg_path,
            output=self.output,
            model_choice=model,
            settings=self.cfg,
            callbacks=callbacks,
        )

        Thread(target=self.pipeline.run, daemon=True).start()

    def _stop(self):
        if self.pipeline:
            self.pipeline.stop()
            self.stop_btn.config(state="disabled")

    def _on_close(self):
        try:
            self.cfg["window_geometry"] = self.root.geometry()
            save_config(self.cfg)
        except Exception:
            pass
        self.root.destroy()


# ================= CLI =================

def run_cli(args, cfg):
    """Headless run; prints progress to stdout."""
    log_lock = threading.Lock()

    def cli_log(msg):
        with log_lock:
            print(msg, flush=True)

    last = {"download": -1, "process": -1, "save": -1}

    def cli_progress(stage, done, total):
        # Avoid spamming: only print on change
        if done != last[stage]:
            last[stage] = done
            with log_lock:
                print(f"  [{stage}] {done}/{total}", flush=True)

    finished = threading.Event()

    callbacks = {
        "log": cli_log,
        "progress": cli_progress,
        "preview": lambda *a, **k: None,  # no-op in CLI
        "finished": finished.set,
    }

    if args.images:
        # Expand directories into image files
        expanded = []
        for p in args.images:
            pp = Path(p)
            if pp.is_dir():
                for f in pp.iterdir():
                    if f.suffix.lower() in IMAGE_EXTS:
                        expanded.append(str(f))
            else:
                expanded.append(p)
        args.images = expanded

    pipeline = Pipeline(
        links_path=args.links,
        image_paths=args.images or [],
        background=args.bg,
        output=args.output or cfg["output"],
        model_choice=args.model,
        settings=cfg,
        callbacks=callbacks,
    )

    # Allow Ctrl+C to stop gracefully
    import signal

    def on_sigint(signum, frame):
        cli_log("\nCtrl+C — stopping...")
        pipeline.stop()

    signal.signal(signal.SIGINT, on_sigint)

    pipeline.run()
    return 0


# ================= ENTRY =================

def main():
    parser = argparse.ArgumentParser(description="ImageProcessor — batch BG-removal + compositing")
    parser.add_argument("--links", help="Path to links.txt")
    parser.add_argument("--images", nargs="+", help="Local image files or folders")
    parser.add_argument("--bg", help="Background image")
    parser.add_argument("--output", help="Output folder")
    parser.add_argument("--model", default="AUTO",
                        help="Model name or AUTO (default: AUTO)")
    args = parser.parse_args()

    cfg = load_config()

    headless = bool(args.links or args.images)

    if headless:
        if not args.bg:
            print("--bg is required when running headless", file=sys.stderr)
            return 2
        return run_cli(args, cfg)

    # Guard for windowed PyInstaller builds where stdio can be None.
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w")

    root = TkinterDnD.Tk()
    prefilled = {
        "links": args.links,
        "images": args.images,
        "bg": args.bg,
        "output": args.output,
    }
    App(root, cfg, prefilled=prefilled)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
