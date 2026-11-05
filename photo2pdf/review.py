"""Folder mode window: scan -> review/adjust -> save.

Everything shown here is driven by detect.Plan; this module only renders it
and forwards clicks to Plan methods, then redraws.
"""

from __future__ import annotations

import logging
import queue
import threading
import traceback
from pathlib import Path

from photo2pdf.core import Options, Tesseract, save_documents
from photo2pdf.detect import Plan, ScanSettings, scan_folder

COLUMNS = 6
MAX_OTHERS_SHOWN = 60


class ScanWindow:
    def __init__(self, app, folder: Path, opts: Options, scan: ScanSettings):
        import tkinter as tk
        from tkinter import ttk

        self.tk, self.ttk = tk, ttk
        self.app = app
        self.folder = folder
        self.opts = opts
        self.scan = scan
        self.out_dir = folder / "PDFs"
        self.plan: Plan | None = None
        self.cancel = threading.Event()
        self.events: queue.Queue = queue.Queue()
        self.photos: dict[Path, object] = {}  # PhotoImage cache, keeps references alive
        self.busy = False

        self.win = tk.Toplevel(app.root)
        self.win.title(f"Find documents — {folder.name}")
        self.win.geometry("980x720")
        self.win.minsize(720, 480)
        self.win.protocol("WM_DELETE_WINDOW", self.close)

        top = ttk.Frame(self.win)
        top.pack(fill="x", padx=12, pady=(12, 4))
        self.title = ttk.Label(top, text=f"Looking at photos in {folder}…", font=("Segoe UI", 12),
                               wraplength=900)
        self.title.pack(anchor="w")
        self.hint = ttk.Label(top, text="", wraplength=900)
        self.hint.pack(anchor="w", pady=(2, 0))

        # Scrollable body
        body = ttk.Frame(self.win)
        body.pack(fill="both", expand=True, padx=12, pady=4)
        self.canvas = tk.Canvas(body, highlightthickness=0, bg="#ffffff")
        sb = ttk.Scrollbar(body, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.inner = tk.Frame(self.canvas, bg="#ffffff")
        self._inner_id = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.inner.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(self._inner_id, width=e.width))
        self.win.bind_all("<MouseWheel>", self._wheel, add="+")
        self.win.bind_all("<Button-4>", lambda e: self._scroll(-3), add="+")
        self.win.bind_all("<Button-5>", lambda e: self._scroll(3), add="+")

        # Bottom bar
        bottom = ttk.Frame(self.win)
        bottom.pack(fill="x", padx=12, pady=(4, 12))
        self.progress = ttk.Progressbar(bottom, mode="determinate")
        self.progress.pack(fill="x")
        self.status = ttk.Label(bottom, text="")
        self.status.pack(anchor="w", pady=(4, 6))
        row = ttk.Frame(bottom)
        row.pack(fill="x")
        self.dest = ttk.Label(row, text=f"Save to: {self.out_dir}")
        self.dest.pack(side="left")
        ttk.Button(row, text="Change…", command=self._choose_dest).pack(side="left", padx=6)
        self.b_cancel = ttk.Button(row, text="Stop", command=self.cancel.set)
        self.b_cancel.pack(side="right")
        self.b_save = ttk.Button(row, text="Save", command=self.save, state="disabled")
        self.b_save.pack(side="right", padx=6)

        self.win.after(100, self._poll)
        self._start_scan()

    # -- scrolling ------------------------------------------------------------

    def _wheel(self, e):
        if self.win.winfo_exists():
            self._scroll(int(-e.delta / 120) or (-1 if e.delta > 0 else 1))

    def _scroll(self, units):
        try:
            self.canvas.yview_scroll(units, "units")
        except self.tk.TclError:
            pass

    # -- scan -----------------------------------------------------------------

    def _start_scan(self):
        tess = Tesseract.find()
        if tess is None:
            self.title.configure(text="The text recognition engine is missing, so documents can't be detected.")
            return
        self.busy = True

        def work():
            try:
                infos = scan_folder(self.folder, self.scan, tess,
                                    progress=lambda d, t, m: self.events.put(("progress", d, t, m)),
                                    cancel=self.cancel)
                self.events.put(("scanned", infos))
            except Exception as exc:  # noqa: BLE001
                logging.error("scan crashed:\n%s", traceback.format_exc())
                self.events.put(("error", str(exc)))

        threading.Thread(target=work, daemon=True).start()

    def _poll(self):
        if not self.win.winfo_exists():
            return
        try:
            while True:
                ev = self.events.get_nowait()
                kind = ev[0]
                if kind == "progress":
                    _, d, t, m = ev
                    self.progress.configure(maximum=max(t, 1), value=d)
                    self.status.configure(text=m)
                elif kind == "scanned":
                    self.busy = False
                    self._on_scanned(ev[1])
                elif kind == "saved":
                    self.busy = False
                    self._on_saved(ev[1])
                elif kind == "error":
                    self.busy = False
                    self.status.configure(text=f"Something went wrong: {ev[1]}")
                    self.b_cancel.configure(text="Close", command=self.close)
        except queue.Empty:
            pass
        self.win.after(100, self._poll)

    def _on_scanned(self, infos):
        self.plan = Plan.from_scan(infos, self.scan.gap_seconds)
        errors = [i for i in infos if i.error]
        if self.cancel.is_set():
            self.status.configure(text=f"Stopped early: showing the {len(infos)} photos checked so far.")
            self.cancel.clear()
        else:
            self.status.configure(text=f"Checked {len(infos)} photos."
                                  + (f" {len(errors)} could not be read." if errors else ""))
        self.b_cancel.configure(text="Close", command=self.close)
        self.redraw()

    # -- rendering ------------------------------------------------------------

    def _thumb(self, info):
        from PIL import ImageTk

        img = self.photos.get(info.path)
        if img is None and info.thumb is not None:
            img = ImageTk.PhotoImage(info.thumb)
            self.photos[info.path] = img
        return img

    def redraw(self):
        tk, ttk = self.tk, self.ttk
        plan = self.plan
        for w in self.inner.winfo_children():
            w.destroy()
        pages = sum(len(g) for g in plan.groups)
        n = len(plan.groups)
        self.title.configure(
            text=f"Found {n} document{'s' if n != 1 else ''} ({pages} page{'s' if pages != 1 else ''})"
            if n else "No documents found.")
        self.hint.configure(
            text="Photos taken close together are put into one document. "
                 "Click a page to move it, split the document there, or remove it. "
                 "Click a photo under \"Other photos\" if it's actually a document.")
        self.b_save.configure(state="normal" if n else "disabled",
                              text=f"Save {n} PDF{'s' if n != 1 else ''}")

        names = plan.output_names()
        for g, grp in enumerate(plan.groups):
            box = tk.LabelFrame(self.inner, text=f"  {names[g][:-4]}  ", bg="#ffffff",
                                font=("Segoe UI", 10, "bold"), padx=8, pady=6)
            box.pack(fill="x", padx=4, pady=6)
            bar = tk.Frame(box, bg="#ffffff")
            bar.grid(row=0, column=0, columnspan=COLUMNS, sticky="w", pady=(0, 4))
            if g > 0:
                ttk.Button(bar, text="Join with document above",
                           command=lambda g=g: self._edit(plan.join_previous, g)).pack(side="left")
            ttk.Button(bar, text="Not a document",
                       command=lambda g=g: self._edit(plan.remove_group, g)).pack(side="left", padx=6)
            for i, info in enumerate(grp):
                cell = tk.Frame(box, bg="#ffffff")
                cell.grid(row=1 + i // COLUMNS, column=i % COLUMNS, padx=4, pady=4, sticky="n")
                lbl = tk.Label(cell, image=self._thumb(info), text="?" if info.thumb is None else "",
                               bg="#ffffff", cursor="hand2", relief="solid", bd=1)
                lbl.pack()
                tk.Label(cell, text=f"Page {i + 1}", bg="#ffffff", fg="#555").pack()
                lbl.bind("<Button-1>", lambda e, g=g, i=i: self._page_menu(e, g, i))

        others = plan.others
        if others:
            box = tk.LabelFrame(self.inner, text=f"  Other photos ({len(others)}) — not included  ",
                                bg="#ffffff", fg="#555", padx=8, pady=6)
            box.pack(fill="x", padx=4, pady=(16, 6))
            for k, info in enumerate(others[:MAX_OTHERS_SHOWN]):
                cell = tk.Frame(box, bg="#ffffff")
                cell.grid(row=k // COLUMNS, column=k % COLUMNS, padx=4, pady=4, sticky="n")
                lbl = tk.Label(cell, image=self._thumb(info), text=info.path.name if info.thumb is None else "",
                               bg="#ffffff", cursor="hand2")
                lbl.pack()
                tk.Label(cell, text=info.path.name[:22], bg="#ffffff", fg="#777").pack()
                lbl.bind("<Button-1>", lambda e, k=k: self._other_menu(e, k))
            if len(others) > MAX_OTHERS_SHOWN:
                tk.Label(box, text=f"…and {len(others) - MAX_OTHERS_SHOWN} more not shown.",
                         bg="#ffffff", fg="#777").grid(row=99, column=0, columnspan=COLUMNS, sticky="w")

    def _edit(self, fn, *args):
        if self.busy:
            return
        fn(*args)
        self.redraw()

    def _page_menu(self, event, g, i):
        from photo2pdf.app import open_file

        plan = self.plan
        m = self.tk.Menu(self.win, tearoff=0)
        if i > 0:
            m.add_command(label="Move left", command=lambda: self._edit(plan.move, g, i, -1))
        if i < len(plan.groups[g]) - 1:
            m.add_command(label="Move right", command=lambda: self._edit(plan.move, g, i, +1))
        if i > 0:
            m.add_command(label="Start a new document from this page",
                          command=lambda: self._edit(plan.split, g, i))
        m.add_command(label="Remove this page", command=lambda: self._edit(plan.remove, g, i))
        m.add_separator()
        m.add_command(label="Open photo", command=lambda: open_file(plan.groups[g][i].path))
        m.tk_popup(event.x_root, event.y_root)

    def _other_menu(self, event, k):
        from photo2pdf.app import open_file

        plan = self.plan
        m = self.tk.Menu(self.win, tearoff=0)
        m.add_command(label="This is a document", command=lambda: self._edit(plan.add, k))
        m.add_separator()
        m.add_command(label="Open photo", command=lambda: open_file(plan.others[k].path))
        m.tk_popup(event.x_root, event.y_root)

    # -- save -----------------------------------------------------------------

    def _choose_dest(self):
        from tkinter import filedialog

        d = filedialog.askdirectory(parent=self.win, title="Save PDFs in…", initialdir=str(self.folder))
        if d:
            self.out_dir = Path(d)
            self.dest.configure(text=f"Save to: {self.out_dir}")

    def save(self):
        if self.busy or not self.plan or not self.plan.groups:
            return
        self.busy = True
        self.b_save.configure(state="disabled")
        self.b_cancel.configure(text="Stop", command=self.cancel.set)
        docs = [(name, [p.path for p in grp])
                for name, grp in zip(self.plan.output_names(), self.plan.groups)]
        opts, out_dir = self.opts, self.out_dir

        def work():
            try:
                res = save_documents(docs, out_dir, opts,
                                     progress=lambda d, t, m: self.events.put(("progress", d, t, m)),
                                     cancel=self.cancel)
                self.events.put(("saved", res))
            except Exception as exc:  # noqa: BLE001
                logging.error("save crashed:\n%s", traceback.format_exc())
                self.events.put(("error", str(exc)))

        threading.Thread(target=work, daemon=True).start()

    def _on_saved(self, res):
        from tkinter import messagebox

        from photo2pdf.app import open_file

        self.cancel.clear()
        self.b_cancel.configure(text="Close", command=self.close)
        self.b_save.configure(state="normal")
        n = len(res.outputs)
        self.status.configure(text=f"Saved {n} PDF{'s' if n != 1 else ''} in {self.out_dir}")
        for w in res.warnings:
            logging.warning(w)
        if n:
            open_file(self.out_dir)
        if res.warnings:
            messagebox.showwarning("Photos to PDF", "\n\n".join(res.warnings[:10]), parent=self.win)

    def close(self):
        self.cancel.set()
        try:
            self.win.unbind_all("<MouseWheel>")
            self.win.unbind_all("<Button-4>")
            self.win.unbind_all("<Button-5>")
        except self.tk.TclError:
            pass
        self.win.destroy()
