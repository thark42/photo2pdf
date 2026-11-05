"""Tkinter front end.

Ways in:
  * Double-click the app, then drag photos onto the window or click to choose.
  * Select photos in Explorer -> right-click -> Send to -> Photos to PDF.
  * Drag photos onto the desktop shortcut.
The last two pass file paths as arguments; conversion starts immediately
with the last-used settings.

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
        for k in ("combine", "ocr", "grayscale", "auto_rotate", "language"):
            if k in saved:
                setattr(opts, k, saved[k])
    except (OSError, ValueError):
        pass
    return opts


def save_options(opts: Options) -> None:
    try:
        (_data_dir() / "settings.json").write_text(json.dumps(asdict(opts), indent=2), "utf-8")
    except OSError:
        logging.exception("could not save settings")


def _setup_logging() -> Path:
    log = _data_dir() / "log.txt"
    try:
        if log.exists() and log.stat().st_size > 1_000_000:
            log.unlink()
    except OSError:
        pass
    logging.basicConfig(
        filename=log, level=logging.INFO, encoding="utf-8",
        format="%(asctime)s %(levelname)s %(message)s",
    )
    return log


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
    def __init__(self, initial_files: list[str]):
        import tkinter as tk
        from tkinter import ttk

        self.tk = tk
        self.dnd_files = None
        try:
            from tkinterdnd2 import DND_FILES, TkinterDnD
            self.root = TkinterDnD.Tk()
            self.dnd_files = DND_FILES
        except Exception:  # noqa: BLE001 - drag-and-drop is optional
            logging.exception("tkinterdnd2 unavailable; drag-and-drop disabled")
            self.root = tk.Tk()

        self.root.title(APP_NAME)
        self.root.minsize(460, 420)
        self.opts = load_options()
        self.events: queue.Queue = queue.Queue()
        self.busy = False
        self.last_outputs: list[Path] = []

        pad = {"padx": 16, "pady": 6}
        style = ttk.Style(self.root)
        style.configure("Big.TLabel", font=("Segoe UI", 13))
        style.configure("Status.TLabel", font=("Segoe UI", 10))

        # Drop zone
        self.drop = tk.Label(
            self.root,
            text="Drag photos here\n\nor click to choose photos",
            font=("Segoe UI", 14), relief="groove", bd=2,
            bg="#f4f6f8", fg="#333", cursor="hand2", height=7,
        )
        self.drop.pack(fill="both", expand=True, padx=16, pady=(16, 8))
        self.drop.bind("<Button-1>", lambda e: self.choose())
        if self.dnd_files:
            self.drop.drop_target_register(self.dnd_files)
            self.drop.dnd_bind("<<Drop>>", self._on_drop)
        else:
            self.drop.configure(text="Click here to choose photos")

        # Options
        box = ttk.LabelFrame(self.root, text="Options")
        box.pack(fill="x", **pad)
        self.v_combine = tk.BooleanVar(value=self.opts.combine)
        self.v_ocr = tk.BooleanVar(value=self.opts.ocr)
        self.v_gray = tk.BooleanVar(value=self.opts.grayscale)
        ttk.Radiobutton(box, text="Put all photos into one PDF", variable=self.v_combine,
                        value=True).pack(anchor="w", padx=10, pady=2)
        ttk.Radiobutton(box, text="Make a separate PDF for each photo", variable=self.v_combine,
                        value=False).pack(anchor="w", padx=10, pady=2)
        ttk.Checkbutton(box, text="Make text searchable (slower)",
                        variable=self.v_ocr).pack(anchor="w", padx=10, pady=(8, 2))
        ttk.Checkbutton(box, text="Black and white (smaller files)",
                        variable=self.v_gray).pack(anchor="w", padx=10, pady=(2, 8))

        # Progress + result
        self.progress = ttk.Progressbar(self.root, mode="determinate")
        self.progress.pack(fill="x", **pad)
        self.status = ttk.Label(self.root, text="PDFs are saved next to your photos.",
                                style="Status.TLabel", wraplength=420)
        self.status.pack(fill="x", **pad)
        btns = ttk.Frame(self.root)
        btns.pack(fill="x", padx=16, pady=(0, 16))
        self.b_open = ttk.Button(btns, text="Open PDF", command=self._open_last, state="disabled")
        self.b_open.pack(side="left")
        self.b_folder = ttk.Button(btns, text="Show in folder", command=self._reveal_last,
                                   state="disabled")
        self.b_folder.pack(side="left", padx=8)

        self.root.after(100, self._poll)
        if initial_files:
            self.root.after(200, lambda: self.start(initial_files))

    # -- input --------------------------------------------------------------

    def choose(self) -> None:
        if self.busy:
            return
        from tkinter import filedialog

        pattern = " ".join(f"*{e}" for e in sorted(IMAGE_EXTS))
        files = filedialog.askopenfilenames(
            parent=self.root, title="Choose photos",
            filetypes=[("Photos", pattern), ("All files", "*.*")],
        )
        if files:
            self.start(list(files))

    def _on_drop(self, event) -> None:
        if not self.busy:
            self.start(list(self.root.tk.splitlist(event.data)))

    # -- work ---------------------------------------------------------------

    def _current_options(self) -> Options:
        self.opts.combine = bool(self.v_combine.get())
        self.opts.ocr = bool(self.v_ocr.get())
        self.opts.grayscale = bool(self.v_gray.get())
        return self.opts

    def start(self, files: list[str]) -> None:
        opts = self._current_options()
        save_options(opts)
        self.busy = True
        self.b_open.configure(state="disabled")
        self.b_folder.configure(state="disabled")
        self.drop.configure(text="Working…", cursor="watch")
        self.progress.configure(value=0, maximum=1)
        logging.info("start: %d inputs, opts=%s", len(files), asdict(opts))

        def work():
            try:
                res = convert(files, opts, progress=lambda d, t, m: self.events.put(("progress", d, t, m)))
                self.events.put(("done", res))
            except Exception as exc:  # noqa: BLE001
                logging.error("conversion crashed:\n%s", traceback.format_exc())
                self.events.put(("error", str(exc)))

        threading.Thread(target=work, daemon=True).start()

    def _poll(self) -> None:
        try:
            while True:
                ev = self.events.get_nowait()
                if ev[0] == "progress":
                    _, done, total, msg = ev
                    self.progress.configure(maximum=max(total, 1), value=done)
                    self.status.configure(text=msg)
                elif ev[0] == "done":
                    self._finish(ev[1])
                elif ev[0] == "error":
                    self._finish(Result(warnings=[f"Something went wrong: {ev[1]}"]))
        except queue.Empty:
            pass
        self.root.after(100, self._poll)

    def _finish(self, res: Result) -> None:
        from tkinter import messagebox

        self.busy = False
        self.drop.configure(
            text="Drag more photos here\n\nor click to choose photos" if self.dnd_files
            else "Click here to choose photos",
            cursor="hand2",
        )
        self.last_outputs = res.outputs
        for w in res.warnings:
            logging.warning(w)
        if res.outputs:
            n = len(res.outputs)
            first = res.outputs[0]
            msg = f"Saved {first.name}\nin {first.parent}" if n == 1 else \
                  f"Saved {n} PDFs in {first.parent}"
            self.status.configure(text=msg)
            self.b_open.configure(state="normal")
            self.b_folder.configure(state="normal")
            reveal(first)
        else:
            self.status.configure(text="Nothing was saved.")
        if res.warnings:
            messagebox.showwarning(APP_NAME, "\n\n".join(res.warnings[:10]), parent=self.root)

    def _open_last(self) -> None:
        if self.last_outputs:
            open_file(self.last_outputs[0])

    def _reveal_last(self) -> None:
        if self.last_outputs:
            reveal(self.last_outputs[0])

    def run(self) -> None:
        self.root.mainloop()


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
    if not {"eng", "osd"} <= langs:
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
    return 0 if ok else 1


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

    try:
        App(argv).run()
    except Exception:  # noqa: BLE001
        logging.critical("fatal:\n%s", traceback.format_exc())
        try:
            from tkinter import messagebox
            messagebox.showerror(APP_NAME, f"The app hit an error. Details were saved to:\n{log}")
        except Exception:  # noqa: BLE001
            pass
        return 1
    return 0
