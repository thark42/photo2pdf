"""Tkinter front end.

Ways in:
  * Double-click the app: "Organize a folder of photos" or "Make one PDF from a few photos".
  * Drag a folder or photos onto the window or the desktop shortcut.
  * Explorer: select photos or a folder -> right-click -> Send to -> Photos to PDF.
Anything that includes a folder opens folder (organize) mode; plain photos
make one PDF with the last-used settings.

`--selftest <out.pdf>` runs a headless end-to-end check (used by CI on the
built .exe) and exits 0/1.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import subprocess
import sys
import threading
import traceback
from dataclasses import asdict
from pathlib import Path

from photo2pdf import __version__
from photo2pdf.core import IMAGE_EXTS, Options, Result, Tesseract, convert
from photo2pdf.detect import ScanSettings

APP_NAME = "Photos to PDF"


# --------------------------------------------------------------------------
# Settings + logging
# --------------------------------------------------------------------------

def _data_dir() -> Path:
    base = os.environ.get("APPDATA") if os.name == "nt" else None
    d = Path(base) if base else Path.home() / ".config"
    d = d / "Photo2PDF"
    d.mkdir(parents=True, exist_ok=True)
    return d


def load_options() -> Options:
    opts = Options()
    try:
        saved = json.loads((_data_dir() / "settings.json").read_text("utf-8"))
        # Only what the Settings dialog shows. Internal values (OCR language, sizes) always come
        # from the current version's defaults, so an old settings file can't pin outdated ones.
        for k in ("combine", "ocr", "grayscale", "straighten", "organize"):
            if k in saved:
                setattr(opts, k, saved[k])
    except (OSError, ValueError):
        pass
    return opts


def load_scan_settings() -> ScanSettings:
    """Advanced folder-scan knobs; only editable in settings.json under "scan"."""
    s = ScanSettings()
    try:
        saved = json.loads((_data_dir() / "settings.json").read_text("utf-8")).get("scan", {})
        for k in ("min_words", "gap_seconds", "recursive", "workers"):
            if k in saved:
                setattr(s, k, type(getattr(s, k))(saved[k]))
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    return s


def save_options(opts: Options, scan: ScanSettings | None = None) -> None:
    try:
        data = asdict(opts)
        data["scan"] = {k: v for k, v in asdict(scan or load_scan_settings()).items() if k != "workers"}
        (_data_dir() / "settings.json").write_text(json.dumps(data, indent=2), "utf-8")
    except OSError:
        logging.exception("could not save settings")


def _setup_logging() -> Path:
    from photo2pdf import diag

    return diag.setup()


def reveal(path: Path) -> None:
    """Open Explorer with the file selected (or the platform equivalent)."""
    try:
        if os.name == "nt":
            subprocess.Popen(["explorer", "/select,", str(path)])
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-R", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path.parent)])
    except OSError:
        logging.exception("reveal failed")


def open_file(path: Path) -> None:
    try:
        if os.name == "nt":
            os.startfile(path)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])
    except OSError:
        logging.exception("open failed")


# --------------------------------------------------------------------------
# GUI
# --------------------------------------------------------------------------

class App:
    """Main window: two clear choices, drop anywhere, settings tucked away."""

    def __init__(self, initial_files: list[str]):
        import tkinter as tk
        from tkinter import ttk

        from photo2pdf import diag, ui

        self.tk, self.ttk, self.ui = tk, ttk, ui
        self.dnd_files = None
        try:
            from tkinterdnd2 import DND_FILES, TkinterDnD
            self.root = TkinterDnD.Tk()
            self.dnd_files = DND_FILES
        except Exception:  # noqa: BLE001 - drag-and-drop is optional
            logging.exception("tkinterdnd2 unavailable; drag-and-drop disabled")
            self.root = tk.Tk()
        self.s = ui.scale_of(self.root)

        self.root.title(APP_NAME)
        diag.hook_tk(self.root)
        ui.main_thread_gc(self.root)
        self.opts = load_options()
        self.scan = load_scan_settings()
        self.events: queue.Queue = queue.Queue()
        self.busy = False
        self.cancel = threading.Event()
        self.last_outputs: list[Path] = []
        self.scan_window = None
        self.eta = ui.Eta()
        # Worker threads must never hold a reference to a window: if a thread drops the last
        # one, Tk is torn down from that thread, which crashes on Windows ("Tcl_AsyncDelete:
        # async handler deleted by the wrong thread"). Pass plain data only.
        threading.Thread(target=_log_header, args=(self.opts,), name="session-header", daemon=True).start()

        menubar = tk.Menu(self.root)
        filem = tk.Menu(menubar, tearoff=0)
        filem.add_command(label="Organize a folder of photos…", command=self.choose_folder)
        filem.add_command(label="Make one PDF from photos…", command=self.choose)
        filem.add_separator()
        filem.add_command(label="Settings…", command=self.settings_dialog)
        filem.add_separator()
        filem.add_command(label="Exit", command=self.on_close)
        menubar.add_cascade(label="File", menu=filem)
        helpm = tk.Menu(menubar, tearoff=0)
        helpm.add_command(label="How to use", command=self._open_help)
        helpm.add_command(label="Report a problem or idea…", command=self.report_dialog)
        helpm.add_command(label="Open the log folder", command=lambda: open_file(diag.log_dir()))
        helpm.add_separator()
        helpm.add_command(label=f"About {APP_NAME}", command=self._about)
        menubar.add_cascade(label="Help", menu=helpm)
        self.root.configure(menu=menubar)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

        style = ttk.Style(self.root)
        style.configure("Big.TButton", font=("Segoe UI", 13, "bold"), padding=(14, 12))
        style.configure("Mid.TButton", font=("Segoe UI", 11), padding=(12, 8))

        outer = ttk.Frame(self.root, padding=(20, 16, 20, 14))
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text="Photos to PDF", font=("Segoe UI", 18, "bold")).pack(anchor="w")
        ttk.Label(outer, foreground="#444", font=("Segoe UI", 10), wraplength=int(500 * self.s), text=(
            "Turns photos of letters and documents into tidy, searchable PDFs with clear names. "
            "Everything stays on this computer.")).pack(anchor="w", pady=(2, 14))

        def choice(parent, button_text, style_name, desc, command):
            box = ttk.Frame(parent)
            box.pack(fill="x", pady=(0, 12))
            b = ttk.Button(box, text=button_text, style=style_name, command=command)
            b.pack(fill="x")
            ttk.Label(box, text=desc, foreground="#444", wraplength=int(470 * self.s),
                      justify="left").pack(anchor="w", pady=(4, 0))
            return b

        self.b_folder = choice(
            outer, "Organize a folder of photos", "Big.TButton",
            "Finds the letters and documents among your photos, straightens them, names them by "
            "date and sender, and saves them as PDFs. You check everything before anything is saved. "
            "Your photos are never moved or deleted.", self.choose_folder)
        self.b_files = choice(
            outer, "Make one PDF from a few photos", "Mid.TButton",
            "For a single letter: pick the photos of its pages and get one PDF next to them.",
            self.choose)

        self.hint = ttk.Label(outer, foreground="#555", text=(
            "Tip: you can also drag a folder, or some photos, onto this window." if self.dnd_files
            else ""))
        self.hint.pack(anchor="w", pady=(0, 8))

        # Quick-mode progress area (hidden until used)
        self.work = ttk.Frame(outer)
        self.progress = ttk.Progressbar(self.work, mode="determinate")
        self.progress.pack(fill="x")
        self.status = ttk.Label(self.work, text="", wraplength=int(470 * self.s), justify="left")
        self.status.pack(anchor="w", pady=(4, 4))
        row = ttk.Frame(self.work)
        row.pack(fill="x")
        self.b_stop = ttk.Button(row, text="Stop", command=self._stop)
        self.b_open = ttk.Button(row, text="Open the PDF", command=self._open_last)
        self.b_show = ttk.Button(row, text="Show in folder", command=self._reveal_last)

        foot = ttk.Frame(outer)
        foot.pack(side="bottom", fill="x")
        ttk.Button(foot, text="Settings…", command=self.settings_dialog).pack(side="left")
        ttk.Button(foot, text="Help", command=self._open_help).pack(side="left", padx=6)

        if self.dnd_files:
            for w in (self.root, outer):
                w.drop_target_register(self.dnd_files)
                w.dnd_bind("<<Drop>>", self._on_drop)

        ui.fit_window(self.root, 560, 470, 480, 420)
        self.root.after(100, self._poll)
        if initial_files:
            self.root.after(200, lambda: self.start(initial_files))

    # -- input --------------------------------------------------------------

    def _focus_scan_window(self) -> bool:
        sw = self.scan_window
        if sw is not None and sw.win.winfo_exists():
            sw.win.deiconify()
            sw.win.lift()
            sw.win.focus_force()
            return True
        self.scan_window = None
        return False

    def choose_folder(self) -> None:
        if self.busy or self._focus_scan_window():
            return
        from tkinter import filedialog

        d = filedialog.askdirectory(parent=self.root, title="Choose the folder with your photos")
        if d:
            self.open_folders([Path(d)])

    def open_folders(self, inputs: list[Path]) -> None:
        if self.busy or self._focus_scan_window():
            return
        from photo2pdf.review import ScanWindow

        logging.info("organize: %d inputs, recursive=%s", len(inputs), self.scan.recursive)
        self.scan_window = ScanWindow(self, inputs, self.opts, self.scan)

    def choose(self) -> None:
        if self.busy or self._focus_scan_window():
            return
        from tkinter import filedialog

        pattern = " ".join(f"*{e}" for e in sorted(IMAGE_EXTS))
        files = filedialog.askopenfilenames(
            parent=self.root, title="Choose the photos of one document",
            filetypes=[("Photos", pattern), ("All files", "*.*")],
        )
        if files:
            self.start(list(files))

    def _on_drop(self, event) -> None:
        self.start(list(self.root.tk.splitlist(event.data)))

    def start(self, files: list[str]) -> None:
        """Entry for drops, Send To and the file picker. Any folder -> organize mode."""
        if self.busy or self._focus_scan_window():
            return
        paths = [Path(f) for f in files]
        if any(p.is_dir() for p in paths):
            self.open_folders(paths)
            return
        self._start_quick(files)

    # -- quick mode ---------------------------------------------------------

    def _start_quick(self, files: list[str]) -> None:
        opts = self.opts
        self.busy = True
        self.cancel.clear()
        self.eta = self.ui.Eta()
        for b in (self.b_open, self.b_show):
            b.pack_forget()
        self.b_stop.pack(side="left")
        self.work.pack(fill="x", pady=(4, 8), before=self.hint)
        self.progress.configure(value=0, maximum=1)
        self.status.configure(text="Starting…")
        for b in (self.b_folder, self.b_files):
            b.state(["disabled"])
        self.ui.keep_awake(True)
        logging.info("quick: %d inputs, opts=%s", len(files), asdict(opts))

        events, cancel = self.events, self.cancel   # plain objects only: no window in the thread

        def work():
            try:
                res = convert(files, opts, cancel=cancel,
                              progress=lambda d, t, m: events.put(("progress", d, t, m)))
                events.put(("done", res))
            except Exception as exc:  # noqa: BLE001
                logging.error("conversion crashed:\n%s", traceback.format_exc())
                events.put(("error", str(exc)))

        threading.Thread(target=work, name="quick", daemon=True).start()

    def _stop(self) -> None:
        self.cancel.set()
        self.status.configure(text="Stopping…")

    def _poll(self) -> None:
        try:
            while True:
                ev = self.events.get_nowait()
                if ev[0] == "progress":
                    _, done, total, msg = ev
                    self.progress.configure(maximum=max(total, 1), value=done)
                    eta = self.eta.text(done, total)
                    self.status.configure(text=msg + (f"  ·  {eta}" if eta else ""))
                elif ev[0] == "done":
                    self._finish(ev[1])
                elif ev[0] == "error":
                    self._finish(Result(errors=[ev[1]]))
        except queue.Empty:
            pass
        self.root.after(100, self._poll)

    def _finish(self, res: Result) -> None:
        self.busy = False
        self.ui.keep_awake(False)
        for b in (self.b_folder, self.b_files):
            b.state(["!disabled"])
        self.b_stop.pack_forget()
        self.last_outputs = res.outputs
        for w in res.warnings + res.detail_lines():
            logging.warning(w)
        if res.outputs:
            first = res.outputs[0]
            n = len(res.outputs)
            self.status.configure(text=f"Saved {first.name}\nin {first.parent}" if n == 1
                                  else f"Saved {n} PDFs in {first.parent}")
            self.b_open.pack(side="left")
            self.b_show.pack(side="left", padx=6)
            if not res.problems():
                reveal(first)
        else:
            self.status.configure(text=" ".join(res.summary_lines()))
        if res.problems() or res.stopped:
            self.ui.show_result(self.root, APP_NAME, res.summary_lines(), res.detail_lines(),
                                offer_report=self._report_from if res.errors else None,
                                open_label="Show in folder" if res.outputs else None,
                                on_open=self._reveal_last)

    # -- settings -------------------------------------------------------------

    def settings_dialog(self) -> None:
        SettingsDialog(self)

    # -- problem reports / help ---------------------------------------------------

    def _about(self) -> None:
        from tkinter import messagebox

        from photo2pdf import diag

        messagebox.showinfo(APP_NAME, f"{APP_NAME} {__version__}\n\nWorks entirely on this computer; "
                            f"nothing is uploaded.\n\nLog folder:\n{diag.log_dir()}", parent=self.root)

    def _open_help(self) -> None:
        for root in ([Path(sys.executable).parent] if getattr(sys, "frozen", False) else []) + \
                [Path(__file__).resolve().parent.parent]:
            f = root / "HOW_TO_USE.txt"
            if f.exists():
                open_file(f)
                return
        self._about()

    def report_dialog(self, preset: str = "") -> None:
        ReportDialog(self, preset)

    def _report_from(self, text: str) -> None:
        self.report_dialog(preset="Something went wrong:\n" + text[:800] + "\n\nWhat I was doing: ")

    def _open_last(self) -> None:
        if self.last_outputs:
            open_file(self.last_outputs[0])

    def _reveal_last(self) -> None:
        if self.last_outputs:
            reveal(self.last_outputs[0])

    def on_close(self) -> None:
        from tkinter import messagebox

        sw = self.scan_window
        why = ""
        if self.busy:
            why = "A PDF is still being made."
        elif sw is not None and sw.win.winfo_exists():
            why = sw.pending_work()
        if why and not messagebox.askyesno(
                APP_NAME, f"{why}\n\nClose Photos to PDF anyway?", parent=self.root,
                icon="warning", default="no"):
            return
        self.cancel.set()
        if sw is not None and sw.win.winfo_exists():
            sw.close(force=True)
        self.ui.keep_awake(False)
        self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()


def _log_header(opts) -> None:
    from photo2pdf import diag

    diag.log_session_header(Tesseract.find(), opts)


class SettingsDialog:
    """Everything optional, in one place, with the recommended defaults pre-set."""

    def __init__(self, app: App):
        import tkinter as tk
        from tkinter import ttk

        from photo2pdf.detect import ORGANIZE_CHOICES

        self.app = app
        o, s = app.opts, app.scan
        w = self.win = tk.Toplevel(app.root)
        w.title("Settings")
        w.transient(app.root)
        w.resizable(False, False)
        f = ttk.Frame(w, padding=16)
        f.pack(fill="both", expand=True)

        def section(text):
            ttk.Label(f, text=text, font=("Segoe UI", 10, "bold")).pack(anchor="w", pady=(10, 2))

        section("When organizing a folder")
        ttk.Label(f, text="Arrange the PDFs:").pack(anchor="w")
        self.v_org = tk.StringVar(value=ORGANIZE_CHOICES.get(o.organize, ORGANIZE_CHOICES["year"]))
        ttk.Combobox(f, textvariable=self.v_org, state="readonly", width=30,
                     values=list(ORGANIZE_CHOICES.values())).pack(anchor="w", pady=(2, 4))
        self.v_rec = tk.BooleanVar(value=s.recursive)
        ttk.Checkbutton(f, text="Also look in folders inside the folder", variable=self.v_rec).pack(anchor="w")

        section("When making a PDF from a few photos")
        self.v_comb = tk.BooleanVar(value=o.combine)
        ttk.Radiobutton(f, text="Put all the photos into one PDF", variable=self.v_comb, value=True).pack(anchor="w")
        ttk.Radiobutton(f, text="Make a separate PDF for each photo", variable=self.v_comb, value=False).pack(anchor="w")

        section("Pages")
        self.v_ocr = tk.BooleanVar(value=o.ocr)
        ttk.Checkbutton(f, text="Make PDFs searchable (also needed for automatic names)",
                        variable=self.v_ocr).pack(anchor="w")
        self.v_str = tk.BooleanVar(value=o.straighten)
        ttk.Checkbutton(f, text="Straighten pages photographed at an angle", variable=self.v_str).pack(anchor="w")
        self.v_gray = tk.BooleanVar(value=o.grayscale)
        ttk.Checkbutton(f, text="Black and white, cleaned up like a scan (smaller files)",
                        variable=self.v_gray).pack(anchor="w")

        row = ttk.Frame(f)
        row.pack(fill="x", pady=(16, 0))
        ttk.Button(row, text="Cancel", command=w.destroy).pack(side="right")
        ttk.Button(row, text="OK", command=self.ok).pack(side="right", padx=6)
        ttk.Button(row, text="Restore recommended", command=self.defaults).pack(side="left")
        w.bind("<Escape>", lambda e: w.destroy())
        from photo2pdf import ui

        ui.center_on(w, app.root)
        w.grab_set()

    def defaults(self):
        from photo2pdf.detect import ORGANIZE_CHOICES

        d = Options()
        self.v_org.set(ORGANIZE_CHOICES[d.organize])
        self.v_rec.set(ScanSettings().recursive)
        self.v_comb.set(d.combine)
        self.v_ocr.set(d.ocr)
        self.v_str.set(d.straighten)
        self.v_gray.set(d.grayscale)

    def ok(self):
        from photo2pdf.detect import ORGANIZE_CHOICES

        o, s = self.app.opts, self.app.scan
        o.organize = next((k for k, v in ORGANIZE_CHOICES.items() if v == self.v_org.get()), "year")
        s.recursive = bool(self.v_rec.get())
        o.combine = bool(self.v_comb.get())
        o.ocr = bool(self.v_ocr.get())
        o.straighten = bool(self.v_str.get())
        o.grayscale = bool(self.v_gray.get())
        save_options(o, s)
        logging.info("settings saved: %s scan=%s", asdict(o), asdict(s))
        self.win.destroy()


# --------------------------------------------------------------------------
# Self-test (headless)
# --------------------------------------------------------------------------

def selftest(out_pdf: Path) -> int:
    """Render text into a sideways photo, convert with OCR, check the text came back."""
    import tempfile

    from PIL import Image, ImageDraw, ImageFont
    from pypdf import PdfReader

    tess = Tesseract.find()
    print(f"tesseract: {tess.exe if tess else None}")
    if not tess:
        return 1
    langs = tess.languages()
    print(f"languages: {sorted(langs)}")
    if not {"eng", "nld", "osd"} <= langs:
        return 1

    with tempfile.TemporaryDirectory() as td:
        img = Image.new("RGB", (1700, 2200), "white")
        draw = ImageDraw.Draw(img)
        try:
            font = ImageFont.truetype("arial.ttf" if os.name == "nt" else "DejaVuSans.ttf", 48)
        except OSError:
            font = ImageFont.load_default(48)
        for i in range(12):
            draw.text((120, 150 + i * 80), f"Selftest line {i} quick brown fox", fill="black", font=font)
        src = Path(td) / "IMG_0001.jpg"
        img.rotate(90, expand=True).save(src)  # sideways: exercises auto-rotate

        res = convert([src], Options(combine=True, ocr=True), tesseract=tess)
        print(f"outputs: {res.outputs} warnings: {res.warnings}")
        if len(res.outputs) != 1 or res.warnings:
            return 1
        text = PdfReader(str(res.outputs[0])).pages[0].extract_text() or ""
        out_pdf.parent.mkdir(parents=True, exist_ok=True)
        out_pdf.write_bytes(res.outputs[0].read_bytes())
    ok = "brown" in text.lower()
    print(f"extracted ok={ok}: {text[:80]!r}")
    if not ok:
        return 1

    # Folder mode: two documents separated by a non-document photo.
    from photo2pdf.detect import Plan, ScanSettings, scan_folder

    with tempfile.TemporaryDirectory() as td:
        folder = Path(td)
        page = img
        page.save(folder / "IMG_20260301_100000.jpg")
        page2 = Image.new("RGB", (1700, 2200), "white")   # a different second page, not a duplicate
        d2 = ImageDraw.Draw(page2)
        for i in range(12):
            d2.text((120, 150 + i * 80), f"Second page line {i} lazy dog harbour lantern", fill="black",
                    font=font)
        page2.rotate(180).save(folder / "IMG_20260301_100030.jpg")
        noise = Image.new("RGB", (900, 700))
        nd = ImageDraw.Draw(noise)
        for y in range(0, 700, 7):
            nd.line([(0, y), (900, y)], fill=((y * 3) % 255, (y * 7) % 255, 120))
        noise.save(folder / "IMG_20260301_100100.jpg")
        page3 = Image.new("RGB", (1700, 2200), "white")
        d3 = ImageDraw.Draw(page3)
        for i in range(12):
            d3.text((120, 150 + i * 80), f"Third letter line {i} meadow copper violet", fill="black", font=font)
        page3.save(folder / "IMG_20260301_100200.jpg")
        infos = scan_folder(folder, ScanSettings(workers=2), tess, use_cache=False)
        plan = Plan.from_scan(infos, 180)
        shape = [len(g) for g in plan.groups]
        print(f"folder scan: words={[i.words for i in infos]} groups={shape}")
        if shape != [2, 1]:
            return 1

    # Problem report: zip builds and holds the log.
    import zipfile

    from photo2pdf import diag

    with tempfile.TemporaryDirectory() as td:
        rep = diag.build_report("selftest", include_fields=False, tess=tess, out_dir=Path(td))
        names = zipfile.ZipFile(rep).namelist()
        print(f"report: {sorted(names)}")
        if "message.txt" not in names or "system.json" not in names:
            return 1

    # Angled, upside-down Dutch letter: straightening, orientation, naming.
    import numpy as np

    import cv2

    letter = Image.new("RGB", (1240, 1754), (250, 250, 246))
    ld = ImageDraw.Draw(letter)
    try:
        f_big = ImageFont.truetype("arial.ttf" if os.name == "nt" else "DejaVuSans.ttf", 48)
        f_txt = ImageFont.truetype("arial.ttf" if os.name == "nt" else "DejaVuSans.ttf", 30)
    except OSError:
        f_big = f_txt = ImageFont.load_default(36)
    ld.text((90, 80), "Belastingdienst", fill="black", font=f_big)
    y = 240
    for line in ["Datum 14 maart 2025", "", "Voorlopige aanslag inkomstenbelasting 2025", "",
                 "Geachte heer, mevrouw,", "Hierbij ontvangt u de voorlopige aanslag voor 2025.",
                 "Het bedrag dat u moet betalen staat hieronder vermeld.",
                 "U betaalt dit bedrag in maandelijkse termijnen.",
                 "Met vriendelijke groet, de inspecteur"]:
        ld.text((90, y), line, fill="black", font=f_txt)
        y += 62
    src = np.asarray(letter)
    W, H = 2400, 1800
    # Upside down (corners listed BR,BL,TL,TR of the page) and in perspective.
    corners = np.float32([[1560, 1420], [880, 1500], [760, 260], [1640, 330]])
    m = cv2.getPerspectiveTransform(np.float32([[0, 0], [1240, 0], [1240, 1754], [0, 1754]]), corners)
    bg = np.full((H, W, 3), (70, 55, 40), np.uint8)
    warped = cv2.warpPerspective(src, m, (W, H))
    mask = cv2.warpPerspective(np.full((1754, 1240), 255, np.uint8), m, (W, H))
    shot = np.where(mask[..., None] > 0, warped, bg).astype(np.uint8)
    # Saved the way an iPhone does: HEIC, pixels stored sideways with an orientation tag,
    # capture time only in EXIF. Checks the bundled HEIC decoder in the built app.
    from datetime import datetime

    exif = Image.Exif()
    exif[0x0112] = 6                                   # display = stored rotated 90 deg clockwise
    exif[0x0132] = "2025:03:15 10:15:00"
    exif.get_ifd(0x8769)[0x9003] = "2025:03:15 10:15:00"
    with tempfile.TemporaryDirectory() as td:
        folder = Path(td)
        try:
            import pillow_heif

            pillow_heif.register_heif_opener()
            Image.fromarray(shot).rotate(90, expand=True).save(
                folder / "IMG_0001.HEIC", format="HEIF", exif=exif.tobytes(), quality=90)
        except Exception as exc:  # noqa: BLE001
            print(f"heic: cannot write test file: {exc!r}")
            return 1
        infos = scan_folder(folder, ScanSettings(workers=1), tess, use_cache=False)
        plan = Plan.from_scan(infos, 180)
        names = plan.output_names()
        print(f"letter (HEIC): doc={infos[0].is_document} rotation={infos[0].rotation} "
              f"taken={infos[0].taken} error={infos[0].error} names={names}")
        if names != ["2025-03-14 Belastingdienst - Voorlopige aanslag inkomstenbelasting 2025.pdf"]:
            return 1
        if infos[0].taken != datetime(2025, 3, 15, 10, 15):
            return 1

    # Card: insurance card photographed at an angle -> card kind, card folder, real-size name.
    cardimg = Image.new("RGB", (1012, 638), (235, 242, 250))
    cd = ImageDraw.Draw(cardimg)
    y = 50
    for k, line in enumerate(["Zilveren Kruis", "Zorgpas", "Polisnummer 123456789", "Geldig tot 31-12-2026"]):
        cd.text((60, y), line, fill="black", font=f_big if k == 0 else f_txt)
        y += 70 if k == 0 else 52
    csrc = np.asarray(cardimg)
    cm = cv2.getPerspectiveTransform(np.float32([[0, 0], [1012, 0], [1012, 638], [0, 638]]),
                                     np.float32([[620, 420], [1760, 470], [1720, 1230], [580, 1160]]))
    cshot = np.where(cv2.warpPerspective(np.full((638, 1012), 255, np.uint8), cm, (W, H))[..., None] > 0,
                     cv2.warpPerspective(csrc, cm, (W, H)), bg).astype(np.uint8)
    with tempfile.TemporaryDirectory() as td:
        folder = Path(td)
        Image.fromarray(cshot).save(folder / "IMG_20250601_120000.jpg")
        infos = scan_folder(folder, ScanSettings(workers=1), tess, use_cache=False)
        plan = Plan.from_scan(infos, 180)
        paths = plan.output_paths("year")
        print(f"card: kind={infos[0].kind} shape={infos[0].card_shape} paths={paths}")
        if paths != ["Cards/Health insurance card - Zilveren Kruis - valid until 2026-12-31.pdf"]:
            return 1

    # Receipt + ticket screenshot with a QR code (checks the bundled QR detector too).
    from photo2pdf.features import find_codes

    rec = Image.new("RGB", (700, 1300), (248, 247, 242))
    rd = ImageDraw.Draw(rec)
    for k, line in enumerate(["ALBERT HEIJN", "BANANEN 1,99", "MELK 1,19", "BROOD 2,49", "KAAS 6,49",
                              "SUBTOTAAL 12,16", "TOTAAL 12,16", "PINNEN 12,16", "BTW 9% 1,00",
                              "02-11-2024 14:32 Kassa 3"]):
        rd.text((40, 40 + k * 70), line, fill="black", font=f_txt)
    qr = cv2.QRCodeEncoder.create().encode("SELFTEST")
    qr = cv2.resize(qr, (qr.shape[1] * 14, qr.shape[0] * 14), interpolation=cv2.INTER_NEAREST)
    shot = Image.new("RGB", (1080, 2340), "white")
    sd = ImageDraw.Draw(shot)
    for k, line in enumerate(["KLM", "Boarding pass", "Flight KL1234 Gate D7", "Date 14 July 2026"]):
        sd.text((70, 220 + k * 90), line, fill="black", font=f_big)
    shot.paste(Image.fromarray(qr).convert("RGB"), (300, 800))
    print(f"codes: {find_codes(shot)}")
    with tempfile.TemporaryDirectory() as td:
        folder = Path(td)
        rec.save(folder / "IMG_20250601_130000.jpg")
        shot.save(folder / "Screenshot_20250601-131500.png")
        infos = scan_folder(folder, ScanSettings(workers=1), tess, use_cache=False)
        plan = Plan.from_scan(infos, 180)
        paths = sorted(plan.output_paths("year"))
        print(f"receipt/ticket: kinds={[i.kind for i in infos]} paths={paths}")
        if paths != ["Receipts/2024/Receipt - Albert Heijn - 2024-11-02 - EUR 12.16.pdf",
                     "Tickets/2026/Boarding pass - KLM - 2026-07-14.pdf"]:
            return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    log = _setup_logging()
    logging.info("%s %s starting, args=%d", APP_NAME, __version__, len(argv))

    if argv[:1] == ["--selftest"]:
        target = Path(argv[1]) if len(argv) > 1 else Path("selftest.pdf")
        # Windowed .exe has no console; mirror output into a file CI can read.
        report = target.with_suffix(".txt")
        import contextlib
        import io

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            try:
                code = selftest(target)
            except Exception:  # noqa: BLE001
                traceback.print_exc(file=buf)
                code = 1
        report.write_text(buf.getvalue() + f"exit={code}\n", "utf-8")
        return code

    from photo2pdf import ui

    ui.enable_dpi_awareness()
    try:
        App(argv).run()
    except Exception:  # noqa: BLE001
        logging.critical("fatal:\n%s", traceback.format_exc())
        try:
            from tkinter import messagebox

            from photo2pdf import diag
            if messagebox.askyesno(APP_NAME, "The app hit an error and has to close.\n\n"
                                   "Make a problem report you can email to the person who set this up?"):
                out = diag.build_report("(automatic report after a crash)", include_fields=False)
                reveal(out)
        except Exception:  # noqa: BLE001
            pass
        return 1
    return 0


def offer_report(app, text: str, parent) -> None:
    """Show warnings/errors with a one-click way to send a report."""
    from tkinter import messagebox

    if messagebox.askyesno(APP_NAME, text + "\n\nMake a problem report about this?", parent=parent,
                           icon="warning", default="no"):
        app.report_dialog(preset="Something went wrong:\n" + text[:500] + "\n\nWhat I was doing: ")


class ReportDialog:
    """Her message + an explicit choice about name fields -> zip on the Desktop."""

    def __init__(self, app, preset: str = ""):
        import tkinter as tk
        from tkinter import ttk

        self.app = app
        w = self.win = tk.Toplevel(app.root)
        w.title("Report a problem or idea")
        w.geometry("560x520")
        w.transient(app.root)
        from photo2pdf import ui

        ui.center_on(w, app.root)
        ttk.Label(w, wraplength=520, justify="left", text=(
            "Describe the problem or the idea. If something went wrong, say what you did "
            "just before, and which photo or folder it was.")).pack(fill="x", padx=16, pady=(16, 6))
        self.text = tk.Text(w, height=10, wrap="word", font=("Segoe UI", 10))
        self.text.pack(fill="both", expand=True, padx=16)
        if preset:
            self.text.insert("1.0", preset)
        self.v_fields = tk.BooleanVar(value=True)
        ttk.Checkbutton(w, variable=self.v_fields, text="Include recognised dates, senders and "
                        "document types").pack(anchor="w", padx=16, pady=(8, 0))
        ttk.Label(w, foreground="#555", text="(helps fix naming problems; untick to leave them out)"
                  ).pack(anchor="w", padx=40)
        ttk.Label(w, wraplength=520, justify="left", foreground="#555", text=(
            "The report is a .zip file saved on your Desktop. It contains the app's log "
            "(what it did, timings and errors), your settings and this message. It does NOT "
            "contain your photos, your PDFs, or the text of your documents. Your user name "
            "is replaced by <user>. Nothing is sent anywhere: you email the file yourself."
        )).pack(fill="x", padx=16, pady=(6, 8))
        row = ttk.Frame(w)
        row.pack(fill="x", padx=16, pady=(0, 16))
        ttk.Button(row, text="Cancel", command=w.destroy).pack(side="right")
        ttk.Button(row, text="Create report", command=self.create).pack(side="right", padx=6)

    def create(self) -> None:
        from tkinter import messagebox

        from photo2pdf import diag

        msg = self.text.get("1.0", "end").strip()
        try:
            out = diag.build_report(msg, include_fields=bool(self.v_fields.get()),
                                    tess=Tesseract.find())
        except Exception as exc:  # noqa: BLE001
            logging.exception("report failed")
            messagebox.showerror(APP_NAME, f"Could not create the report: {exc}", parent=self.win)
            return
        self.win.destroy()
        messagebox.showinfo(APP_NAME, f"Report saved as:\n{out}\n\nAttach this file to an email "
                            "to the person who set up this app for you.", parent=self.app.root)
        reveal(out)
