"""Folder mode window: scan -> review/adjust -> save.

Everything shown here is driven by detect.Plan; this module only renders it
and forwards clicks to Plan methods (with an undo checkpoint), then redraws.

Layout (top to bottom): heading, tab row (new documents / already saved /
not documents) with paging, scrollable cards, and an action bar that is
packed first so it stays visible on small screens.
"""

from __future__ import annotations

import logging
import queue
import threading
import traceback
from pathlib import Path

from photo2pdf import ui
from photo2pdf.core import Options, PageSpec, Tesseract, fallback_dir, is_writable, save_documents
from photo2pdf.detect import ORGANIZE_CHOICES, Plan, ScanSettings, scan_paths

COLUMNS = 6
DOCS_PER_PAGE = 12
OTHERS_PER_PAGE = 36
THUMB_DISPLAY = 130  # px at 96 dpi

TAB_NEW, TAB_SAVED, TAB_OTHER = "new", "saved", "other"


class ScanWindow:
    def __init__(self, app, inputs: list[Path], opts: Options, scan: ScanSettings):
        import tkinter as tk
        from tkinter import ttk

        self.tk, self.ttk = tk, ttk
        self.app = app
        self.inputs = [Path(p) for p in inputs]
        self.opts = opts
        self.scan = scan
        self.out_dir = self._default_out_dir()
        self.plan: Plan | None = None
        self.cancel = threading.Event()
        self.events: queue.Queue = queue.Queue()
        self.photos: dict[int, object] = {}   # id(PIL thumb) -> PhotoImage (fresh after rotate)
        self.busy = False
        self.unsaved_edits = False
        self.saved_this_session = 0
        self.tab = TAB_NEW
        self.page = 0
        self.eta = ui.Eta()
        self.s = ui.scale_of(app.root)

        self.win = tk.Toplevel(app.root)
        label = self.inputs[0].name if len(self.inputs) == 1 else f"{len(self.inputs)} folders"
        self.win.title(f"Organize photos — {label}")
        ui.fit_window(self.win, 1060, 820)
        self.win.protocol("WM_DELETE_WINDOW", self.close)
        self.win.bind("<Control-z>", lambda e: self.undo())

        # Action bar first (side=bottom) so it is never pushed off-screen.
        self._build_action_bar()

        top = ttk.Frame(self.win)
        top.pack(fill="x", padx=12, pady=(12, 0))
        self.title = ttk.Label(top, text="Looking through your photos…", font=("Segoe UI", 13, "bold"))
        self.title.pack(anchor="w")
        self.hint = ttk.Label(top, text="", foreground="#444")
        self.hint.pack(anchor="w", pady=(2, 6))
        self.tabrow = ttk.Frame(top)
        self.tabrow.pack(fill="x")

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

        self._show_scanning_panel()
        self.win.after(100, self._poll)
        self._start_scan()

    # -- layout pieces ----------------------------------------------------------

    def _default_out_dir(self) -> Path:
        folders = [p if p.is_dir() else p.parent for p in self.inputs]
        base = folders[0] if len({str(f) for f in folders}) == 1 else Path(
            __import__("os").path.commonpath([str(f) for f in folders]))
        return base / "PDFs"

    def _build_action_bar(self):
        tk, ttk = self.tk, self.ttk
        bar = ttk.Frame(self.win)
        bar.pack(side="bottom", fill="x", padx=12, pady=(4, 12))
        self.progress = ttk.Progressbar(bar, mode="determinate")
        self.progress.pack(fill="x")
        self.status = ttk.Label(bar, text="", wraplength=int(900 * self.s))
        self.status.pack(anchor="w", pady=(4, 6))

        row1 = ttk.Frame(bar)
        row1.pack(fill="x")
        ttk.Label(row1, text="Save PDFs in:").pack(side="left")
        self.dest = ttk.Label(row1, text=str(self.out_dir), foreground="#333")
        self.dest.pack(side="left", padx=(4, 4))
        ttk.Button(row1, text="Change…", command=self._choose_dest).pack(side="left")
        ttk.Label(row1, text="   Sort into:").pack(side="left")
        self.v_org = tk.StringVar(value=ORGANIZE_CHOICES.get(self.opts.organize, ORGANIZE_CHOICES["year"]))
        org = ttk.Combobox(row1, textvariable=self.v_org, state="readonly", width=24,
                           values=list(ORGANIZE_CHOICES.values()))
        org.pack(side="left", padx=4)
        org.bind("<<ComboboxSelected>>", self._organize_changed)
        ui.Tooltip(org, "How your PDFs are arranged inside the save folder.")

        row2 = ttk.Frame(bar)
        row2.pack(fill="x", pady=(8, 0))
        self.b_undo = ttk.Button(row2, text=" Undo", image=ui.icon(self.win, "undo", 14), compound="left",
                                 command=self.undo, state="disabled")
        self.b_undo.pack(side="left")
        ui.Tooltip(self.b_undo, "Undo the last change (Ctrl+Z)")
        self.b_close = ttk.Button(row2, text="Stop", command=self._stop)
        self.b_close.pack(side="right")
        self.b_save = ttk.Button(row2, text="Save", command=self.save, state="disabled")
        self.b_save.pack(side="right", padx=6)

    def _show_scanning_panel(self):
        tk = self.tk
        for w in self.inner.winfo_children():
            w.destroy()
        tk.Label(self.inner, bg="#ffffff", justify="left", font=("Segoe UI", 11), fg="#333",
                 wraplength=int(760 * self.s), text=(
                     "The app is looking at each photo to find the letters and documents, "
                     "straighten them, turn them the right way up and read their date and sender.\n\n"
                     "This takes about a second or two per photo, so a big folder can take a while. "
                     "Your computer won't go to sleep while this runs.\n\n"
                     "You can press Stop at any time. Photos already checked are remembered, so "
                     "next time it carries on where it left off.\n\n"
                     "Nothing is saved, moved or deleted until you press Save.")
                 ).pack(anchor="w", padx=24, pady=24)

    # -- scrolling ---------------------------------------------------------------

    def _wheel(self, e):
        if self.win.winfo_exists():
            self._scroll(int(-e.delta / 120) or (-1 if e.delta > 0 else 1))

    def _scroll(self, units):
        try:
            self.canvas.yview_scroll(units, "units")
        except self.tk.TclError:
            pass

    # -- scan ----------------------------------------------------------------------

    def _start_scan(self):
        tess = Tesseract.find()
        if tess is None:
            self.title.configure(text="The text reader is missing, so documents can't be found.")
            self.status.configure(text="Please reinstall Photos to PDF.")
            self.b_close.configure(text="Close", command=self.close)
            return
        self.busy = True
        ui.keep_awake(True)

        def work():
            try:
                infos = scan_paths(self.inputs, self.scan, tess,
                                   progress=lambda d, t, m: self.events.put(("progress", d, t, m)),
                                   cancel=self.cancel, opts=self.opts)
                self.events.put(("scanned", infos))
            except Exception as exc:  # noqa: BLE001
                logging.error("scan crashed:\n%s", traceback.format_exc())
                self.events.put(("error", str(exc)))

        threading.Thread(target=work, name="scan", daemon=True).start()

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
                    eta = self.eta.text(d, t)
                    self.status.configure(text=m + (f"  ·  {eta}" if eta else ""))
                elif kind == "scanned":
                    self._finish_busy()
                    self._on_scanned(ev[1])
                elif kind == "saved":
                    self._finish_busy()
                    self._on_saved(*ev[1:])
                elif kind == "error":
                    self._finish_busy()
                    self.status.configure(text="Something went wrong.")
                    self.b_close.configure(text="Close", command=self.close)
                    ui.show_result(self.win, "Photos to PDF", ["Something unexpected went wrong."],
                                   [str(ev[1])], offer_report=self._report)
        except queue.Empty:
            pass
        self.win.after(100, self._poll)

    def _finish_busy(self):
        self.busy = False
        ui.keep_awake(False)
        self.eta = ui.Eta()

    def _on_scanned(self, infos):
        self.plan = Plan.from_scan(infos, self.scan.gap_seconds)
        logging.info("plan: %d documents (%d saved earlier), page counts %s, %d other photos",
                     len(self.plan.groups), sum(self.plan.is_saved(g) for g in range(len(self.plan.groups))),
                     [len(g) for g in self.plan.groups], len(self.plan.others))
        errors = [i for i in infos if i.error]
        msg = (f"Stopped early: showing the {len(infos)} photos checked so far. Run it again later to "
               f"check the rest." if self.cancel.is_set() else f"Checked {len(infos)} photos.")
        if errors:
            msg += f" {len(errors)} could not be opened."
        self.cancel.clear()
        self.status.configure(text=msg)
        self.b_close.configure(text="Close", command=self.close)
        self.tab = TAB_NEW if self._tab_items(TAB_NEW) or not self._tab_items(TAB_SAVED) else TAB_SAVED
        self.redraw()

    # -- tabs and paging -------------------------------------------------------------

    def _tab_items(self, tab) -> list[int]:
        """Group indices (documents tabs) or other-photo indices (not-documents tab)."""
        plan = self.plan
        if tab == TAB_OTHER:
            return list(range(len(plan.others)))
        want_saved = tab == TAB_SAVED
        return [g for g in range(len(plan.groups)) if plan.is_saved(g) == want_saved]

    def _set_tab(self, tab):
        self._commit_names()
        self.tab, self.page = tab, 0
        self.redraw()
        self.canvas.yview_moveto(0)

    def _set_page(self, page):
        self._commit_names()
        self.page = page
        self.redraw()
        self.canvas.yview_moveto(0)

    def _build_tabs(self):
        ttk = self.ttk
        for w in self.tabrow.winfo_children():
            w.destroy()
        labels = [(TAB_NEW, "New documents"), (TAB_SAVED, "Already saved"), (TAB_OTHER, "Not documents")]
        for key, text in labels:
            n = len(self._tab_items(key))
            b = ttk.Button(self.tabrow, text=f"{text} ({n})", command=lambda k=key: self._set_tab(k))
            b.pack(side="left", padx=(0, 4))
            if key == self.tab:
                b.state(["pressed", "disabled"])
        per = OTHERS_PER_PAGE if self.tab == TAB_OTHER else DOCS_PER_PAGE
        pages = max(1, -(-len(self._tab_items(self.tab)) // per))
        self.page = min(self.page, pages - 1)
        if pages > 1:
            nxt = ttk.Button(self.tabrow, text="Next ›", command=lambda: self._set_page(self.page + 1))
            nxt.pack(side="right")
            ttk.Label(self.tabrow, text=f"  Page {self.page + 1} of {pages}  ").pack(side="right")
            prv = ttk.Button(self.tabrow, text="‹ Previous", command=lambda: self._set_page(self.page - 1))
            prv.pack(side="right")
            if self.page == 0:
                prv.state(["disabled"])
            if self.page >= pages - 1:
                nxt.state(["disabled"])

    # -- rendering ----------------------------------------------------------------

    def _columns(self) -> int:
        """How many thumbnails fit side by side in the visible width."""
        self.canvas.update_idletasks()
        width = max(self.canvas.winfo_width(), int(600 * self.s))
        return max(2, width // int((THUMB_DISPLAY + 46) * self.s))

    def _thumb(self, info):
        from PIL import ImageTk

        if info.get_thumb() is None:
            return None
        key = id(info.thumb)
        img = self.photos.get(key)
        if img is None:
            t = info.thumb.copy()
            target = int(THUMB_DISPLAY * self.s)
            t.thumbnail((target, target))
            img = ImageTk.PhotoImage(t, master=self.win)
            self.photos[key] = img
        return img

    def _organize(self) -> str:
        label = self.v_org.get()
        return next((k for k, v in ORGANIZE_CHOICES.items() if v == label), "year")

    def redraw(self):
        tk, ttk = self.tk, self.ttk
        plan = self.plan
        if plan is None:
            return
        for w in self.inner.winfo_children():
            w.destroy()
        self._build_tabs()
        n_new = len(self._tab_items(TAB_NEW))
        n_inc = len(plan.included_indices())
        unsaved_new = [g for g in self._tab_items(TAB_NEW)]
        if unsaved_new:
            self.title.configure(text=f"Found {n_new} new document{'s' if n_new != 1 else ''}")
            self.hint.configure(text=(
                "Check the pages, then press Save. Click a page to see it large; use the small buttons "
                "under it to turn, split or remove it."))
        elif self.saved_this_session:
            k = self.saved_this_session
            self.title.configure(text=f"All done: {k} document{'s' if k != 1 else ''} saved")
            self.hint.configure(text="You can close this window, or look through the other tabs.")
        else:
            self.title.configure(text="No new documents found")
            self.hint.configure(text=("Everything here was saved before, or nothing looked like a document. "
                                      "Check \"Not documents\" if you expected more."))
        self.b_save.configure(state="normal" if n_inc else "disabled",
                              text=f"Save {n_inc} PDF{'s' if n_inc != 1 else ''}" if n_inc else "Nothing to save")
        self.b_undo.configure(state="normal" if plan.can_undo() else "disabled")

        items = self._tab_items(self.tab)
        if self.tab == TAB_OTHER:
            self._draw_others(items)
            return
        if not items:
            if self.tab == TAB_NEW:
                msg = ("Everything found here is saved. See \"Already saved\"." if self._tab_items(TAB_SAVED)
                       else "No documents found. See \"Not documents\" if you expected some.")
            else:
                msg = "Nothing from these photos has been saved yet."
            tk.Label(self.inner, text=msg, bg="#ffffff", fg="#444", font=("Segoe UI", 11)).pack(pady=30)
            return
        names = plan.output_paths(self._organize())
        self.name_vars = {}
        start = self.page * DOCS_PER_PAGE
        for g in items[start:start + DOCS_PER_PAGE]:
            self._draw_card(g, names[g])

    def _draw_card(self, g, relpath):
        tk, ttk = self.tk, self.ttk
        plan = self.plan
        grp = plan.groups[g]
        saved = plan.is_saved(g)
        box = tk.Frame(self.inner, bg="#ffffff", highlightbackground="#c8c8c8", highlightthickness=1,
                       padx=10, pady=8)
        box.pack(fill="x", padx=4, pady=6)

        head = tk.Frame(box, bg="#ffffff")
        head.pack(fill="x")
        v_inc = tk.BooleanVar(value=plan.is_included(g))
        cb = ttk.Checkbutton(head, variable=v_inc, text="Save again" if saved else "Save this document",
                             command=lambda: self._edit(plan.set_included, g, bool(v_inc.get())))
        cb.pack(side="left")
        from photo2pdf.classify import LABELS

        if plan.is_card(g):
            pages = "card, front and back" if len(grp) == 2 else "card"
        else:
            what = LABELS.get(plan.kind(g), "")
            pages = (f"{what} · " if what else "") + f"{len(grp)} page{'s' if len(grp) != 1 else ''}"
        tk.Label(head, text=f"   {pages}", bg="#ffffff", fg="#555").pack(side="left")
        if plan.hard_to_read(g):
            hint = tk.Label(head, text="   ⚠ hard to read: retake if you still have the paper",
                            bg="#ffffff", fg="#8a4b00")
            hint.pack(side="left")
        if not saved or plan.is_included(g):
            ttk.Button(head, text="Not a document",
                       command=lambda: self._edit(plan.remove_group, g)).pack(side="right", padx=(6, 0))
        if g > 0 and not saved:
            b = ttk.Button(head, text="Join with document before",
                           command=lambda: self._edit(plan.join_previous, g))
            b.pack(side="right", padx=(6, 0))
            ui.Tooltip(b, "Use this when one letter was split into two documents by mistake.")

        if saved:
            tk.Label(box, text=f"Saved as: {grp[0].saved_as.name}", bg="#ffffff", fg="#1e6b3c",
                     anchor="w").pack(fill="x", pady=(4, 0))
            if not plan.is_included(g):
                # Finished documents: just show the pages; editing appears when "Save again" is ticked.
                grid = tk.Frame(box, bg="#ffffff")
                grid.pack(anchor="w", pady=(4, 0))
                for i, info in enumerate(grp):
                    lbl = tk.Label(grid, image=self._thumb(info), bg="#ffffff", cursor="hand2")
                    cols = self._columns()
                    lbl.grid(row=i // cols, column=i % cols, padx=6, pady=4)
                    lbl.bind("<Button-1>", lambda e, i=i: self._preview(g, i))
                return

        namerow = tk.Frame(box, bg="#ffffff")
        namerow.pack(fill="x", pady=(6, 4))
        folder, _, name = relpath.rpartition("/")
        tk.Label(namerow, text="Name:", bg="#ffffff").pack(side="left")
        var = tk.StringVar(value=name[:-4])
        self.name_vars[g] = var
        ent = ttk.Entry(namerow, textvariable=var)
        ent.pack(side="left", fill="x", expand=True, padx=(6, 6))
        ent.bind("<Return>", lambda e: (self._rename(g), self.redraw()))
        ent.bind("<FocusOut>", lambda e: self._rename(g))
        if folder:
            tk.Label(namerow, text=f"in folder  {folder.replace('/', ' › ')}", bg="#ffffff", fg="#555").pack(
                side="left")

        grid = tk.Frame(box, bg="#ffffff")
        grid.pack(anchor="w")
        for i, info in enumerate(grp):
            self._draw_page(grid, g, i, info)

    def _draw_page(self, grid, g, i, info):
        tk, ttk = self.tk, self.ttk
        plan = self.plan
        cell = tk.Frame(grid, bg="#ffffff")
        cols = self._columns()
        cell.grid(row=i // cols, column=i % cols, padx=6, pady=4, sticky="n")
        img = self._thumb(info)
        lbl = tk.Label(cell, image=img, text="(can't show)" if img is None else "", bg="#ffffff",
                       cursor="hand2", relief="solid", bd=1)
        lbl.pack()
        lbl.bind("<Button-1>", lambda e: self._preview(g, i))
        lbl.bind("<Button-3>", lambda e: self._page_menu(e, g, i))
        ui.Tooltip(lbl, "Click to see this page large. Right-click for more.")
        tools = tk.Frame(cell, bg="#ffffff")
        tools.pack(pady=(3, 0))

        def tool(name, tip, fn):
            b = ttk.Button(tools, image=ui.icon(self.win, name, 16), width=2, command=fn)
            b.pack(side="left", padx=1)
            ui.Tooltip(b, tip)
        tool("rot_left", "Turn left", lambda: self._edit(plan.rotate, g, i, 270))
        tool("rot_right", "Turn right", lambda: self._edit(plan.rotate, g, i, 90))
        if i > 0:
            tool("split", "Start a new document from this page", lambda: self._edit(plan.split, g, i))
        tool("remove", "Remove this page from the document", lambda: self._edit(plan.remove, g, i))
        tk.Label(cell, text=f"Page {i + 1}", bg="#ffffff", fg="#555").pack()

    def _draw_others(self, items):
        tk = self.tk
        plan = self.plan
        if not items:
            tk.Label(self.inner, text="Every photo was recognised as a document.", bg="#ffffff",
                     fg="#444", font=("Segoe UI", 11)).pack(pady=30)
            return
        tk.Label(self.inner, bg="#ffffff", fg="#444", anchor="w", justify="left", text=(
            "These photos will be left alone: ordinary photos, envelopes, signs, screenshots that "
            "aren't confirmations, and second copies of a document. Click a photo if it should be saved."
        ), wraplength=int(900 * self.s)).pack(fill="x", padx=8, pady=(8, 4))
        grid = tk.Frame(self.inner, bg="#ffffff")
        grid.pack(anchor="w", padx=4)
        start = self.page * OTHERS_PER_PAGE
        for n, k in enumerate(items[start:start + OTHERS_PER_PAGE]):
            info = plan.others[k]
            cell = tk.Frame(grid, bg="#ffffff")
            cols = self._columns()
            cell.grid(row=n // cols, column=n % cols, padx=6, pady=6, sticky="n")
            img = self._thumb(info)
            lbl = tk.Label(cell, image=img, text=info.path.name if img is None else "",
                           bg="#ffffff", cursor="hand2")
            lbl.pack()
            name = info.path.name if len(info.path.name) <= 20 else info.path.name[:9] + "…" + info.path.name[-9:]
            tk.Label(cell, text=name, bg="#ffffff", fg="#666").pack()
            if info.reason:
                tk.Label(cell, text=_short_reason(info.reason), bg="#ffffff", fg="#8a4b00",
                         wraplength=int(150 * self.s)).pack()
            lbl.bind("<Button-1>", lambda e, k=k: self._other_menu(e, k))
            lbl.bind("<Button-3>", lambda e, k=k: self._other_menu(e, k))

    # -- edits --------------------------------------------------------------------

    def _rename(self, g):
        if not self.plan or g >= len(self.plan.groups) or g not in getattr(self, "name_vars", {}):
            return
        typed = self.name_vars[g].get().strip()
        suggested = self.plan.suggested_name(g)[:-4]
        current = self.plan.custom_names.get(self.plan.groups[g][0].path)
        if typed and typed != suggested and current != typed + ".pdf":
            from photo2pdf import diag

            diag.fields("review rename doc=%d suggested=%r typed=%r", g + 1, suggested, typed)
            self.plan.checkpoint()
            self.plan.set_name(g, typed)
            self.unsaved_edits = True
            self.b_undo.configure(state="normal")
        elif not typed and current:
            self.plan.checkpoint()
            self.plan.set_name(g, "")

    def _commit_names(self):
        for g in list(getattr(self, "name_vars", {})):
            self._rename(g)

    def _edit(self, fn, *args):
        if self.busy or self.plan is None:
            return
        self._commit_names()
        self._log_edit(fn.__name__, args)
        self.plan.checkpoint()
        fn(*args)
        self.unsaved_edits = True
        self.redraw()

    def undo(self):
        if self.busy or not self.plan or not self.plan.can_undo():
            return
        logging.info("review undo")
        self.plan.undo()
        self.redraw()

    def _log_edit(self, action, args):
        """Record user corrections: they show where the automatic decisions went wrong."""
        try:
            plan = self.plan
            if action in ("rotate", "move", "split", "remove") and len(args) >= 2:
                g, i = args[0], args[1]
                ph = plan.groups[g][i]
                logging.info("review %s doc=%d page=%d file=%s words=%d rotation_before=%d args=%s",
                             action, g + 1, i + 1, ph.path.name, ph.words, ph.rotation, args[2:])
            elif action in ("join_previous", "remove_group"):
                g = args[0]
                logging.info("review %s doc=%d pages=%s", action, g + 1,
                             [p.path.name for p in plan.groups[g]])
            elif action == "add":
                ph = plan.others[args[0]]
                logging.info("review add-as-document file=%s words=%d (detector said no)",
                             ph.path.name, ph.words)
            elif action == "set_included":
                logging.info("review %s doc=%d include=%s saved_before=%s", action, args[0] + 1,
                             args[1], plan.is_saved(args[0]))
        except Exception:  # noqa: BLE001 - logging must never break the UI
            logging.exception("could not log review edit")

    def _page_menu(self, event, g, i):
        from photo2pdf.app import open_file

        plan = self.plan
        m = self.tk.Menu(self.win, tearoff=0)
        m.add_command(label="See this page large", command=lambda: self._preview(g, i))
        m.add_separator()
        m.add_command(label="Turn right", command=lambda: self._edit(plan.rotate, g, i, 90))
        m.add_command(label="Turn left", command=lambda: self._edit(plan.rotate, g, i, 270))
        m.add_command(label="Turn upside down", command=lambda: self._edit(plan.rotate, g, i, 180))
        m.add_separator()
        if i > 0:
            m.add_command(label="Move left", command=lambda: self._edit(plan.move, g, i, -1))
        if i < len(plan.groups[g]) - 1:
            m.add_command(label="Move right", command=lambda: self._edit(plan.move, g, i, +1))
        if i > 0:
            m.add_command(label="Start a new document from this page",
                          command=lambda: self._edit(plan.split, g, i))
        m.add_command(label="Remove this page", command=lambda: self._edit(plan.remove, g, i))
        m.add_separator()
        m.add_command(label="Open the original photo", command=lambda: open_file(plan.groups[g][i].path))
        m.tk_popup(event.x_root, event.y_root)

    def _other_menu(self, event, k):
        from photo2pdf.app import open_file

        plan = self.plan
        m = self.tk.Menu(self.win, tearoff=0)
        m.add_command(label="This is a document", command=lambda: self._edit(plan.add, k))
        m.add_separator()
        m.add_command(label="Open the photo", command=lambda: open_file(plan.others[k].path))
        m.tk_popup(event.x_root, event.y_root)

    # -- large preview -----------------------------------------------------------------

    def _preview(self, g, i):
        Preview(self, g, i)

    # -- save ------------------------------------------------------------------------

    def _organize_changed(self, _e=None):
        self.opts.organize = self._organize()
        from photo2pdf.app import save_options

        save_options(self.opts)
        logging.info("organize mode -> %s", self.opts.organize)
        self.redraw()

    def _choose_dest(self):
        from tkinter import filedialog

        d = filedialog.askdirectory(parent=self.win, title="Save PDFs in…", initialdir=str(self.out_dir.parent))
        if d:
            self.out_dir = Path(d)
            self.dest.configure(text=str(self.out_dir))

    def _stop(self):
        self.cancel.set()
        self.status.configure(text="Stopping after the current photo…")

    def save(self):
        from tkinter import messagebox

        if self.busy or not self.plan:
            return
        self._commit_names()
        idx = self.plan.included_indices()
        if not idx:
            return
        if not is_writable(self.out_dir):
            alt = fallback_dir()
            if not messagebox.askyesno(
                    "Photos to PDF", f"PDFs can't be saved in:\n{self.out_dir}\n\n"
                    f"Save them in your Documents folder instead?\n{alt}", parent=self.win):
                return
            self.out_dir = alt
            self.dest.configure(text=str(alt))
        paths = self.plan.output_paths(self._organize())
        # Real card-size pages only where the card's outline was found; everything else A4.
        def page_kind(p):
            if p.screenshot:
                return "screenshot"
            if p.kind == "card" and p.card_shape:
                return "card"
            return "receipt" if p.kind == "receipt" else "page"
        docs = [(paths[g], [PageSpec(p.path, p.rotation, page_kind(p)) for p in self.plan.groups[g]])
                for g in idx]
        opts, out_dir = self.opts, self.out_dir
        self.busy = True
        ui.keep_awake(True)
        self.b_save.configure(state="disabled")
        self.b_close.configure(text="Stop", command=self._stop)

        def work():
            try:
                res = save_documents(docs, out_dir, opts,
                                     progress=lambda d, t, m: self.events.put(("progress", d, t, m)),
                                     cancel=self.cancel)
                self.events.put(("saved", res, idx))
            except Exception as exc:  # noqa: BLE001
                logging.error("save crashed:\n%s", traceback.format_exc())
                self.events.put(("error", str(exc)))

        threading.Thread(target=work, name="save", daemon=True).start()

    def _on_saved(self, res, idx):
        from photo2pdf.app import open_file

        self.cancel.clear()
        for doc_i, pdf in res.saved:
            self.plan.mark_saved(idx[doc_i], pdf)
        self.saved_this_session += len(res.saved)
        self.plan.history.clear()   # saved files can't be un-saved; undo starts fresh
        self.unsaved_edits = False
        self.b_close.configure(text="Close", command=self.close)
        n = len(res.outputs)
        self.status.configure(text=f"Saved {n} PDF{'s' if n != 1 else ''} in {self.out_dir}")
        for w in res.warnings + res.detail_lines():
            logging.warning(w)
        self.redraw()
        lines = res.summary_lines()
        if n:
            lines.insert(1, f"They are in: {self.out_dir}")
        ui.show_result(self.win, "Photos to PDF", lines, res.detail_lines(),
                       offer_report=self._report if res.errors else None,
                       open_label="Open the PDFs folder" if n else None,
                       on_open=lambda: open_file(self.out_dir))

    def _report(self, text):
        self.app.report_dialog(preset="Something went wrong:\n" + text[:800] + "\n\nWhat I was doing: ")

    # -- closing ----------------------------------------------------------------------

    def pending_work(self) -> str:
        """Why closing now would lose something, or '' if it wouldn't."""
        if self.busy:
            return "The app is still working."
        if self.plan:
            self._commit_names()
            unsaved = [g for g in self.plan.included_indices() if not self.plan.is_saved(g)]
            if unsaved:
                return f"{len(unsaved)} document{'s are' if len(unsaved) != 1 else ' is'} not saved yet."
            if self.unsaved_edits:
                return "Your changes have not been saved."
        return ""

    def close(self, force: bool = False):
        from tkinter import messagebox

        why = "" if force else self.pending_work()
        if why and not messagebox.askyesno(
                "Photos to PDF", f"{why}\n\nClose anyway? Your changes on this screen will be lost "
                "(photos already checked are remembered).", parent=self.win, icon="warning", default="no"):
            return
        self.cancel.set()
        ui.keep_awake(False)
        try:
            self.win.unbind_all("<MouseWheel>")
            self.win.unbind_all("<Button-4>")
            self.win.unbind_all("<Button-5>")
        except self.tk.TclError:
            pass
        self.win.destroy()
        if getattr(self.app, "scan_window", None) is self:
            self.app.scan_window = None


def _short_reason(reason: str) -> str:
    return "Duplicate (clearer copy kept)" if reason.startswith("Duplicate of") else reason


class Preview:
    """Large view of one processed page (straightened and turned as it will be saved)."""

    def __init__(self, sw: ScanWindow, g: int, i: int):
        import tkinter as tk
        from tkinter import ttk

        self.sw, self.g, self.i = sw, g, i
        self.tk = tk
        self.win = tk.Toplevel(sw.win)
        self.win.transient(sw.win)
        ui.fit_window(self.win, 760, 900, 480, 420)
        self.win.bind("<Escape>", lambda e: self.win.destroy())
        self.win.bind("<Left>", lambda e: self.step(-1))
        self.win.bind("<Right>", lambda e: self.step(1))
        bar = ttk.Frame(self.win, padding=8)
        bar.pack(side="bottom", fill="x")
        self.b_prev = ttk.Button(bar, text="‹ Previous page", command=lambda: self.step(-1))
        self.b_prev.pack(side="left")
        self.b_next = ttk.Button(bar, text="Next page ›", command=lambda: self.step(1))
        self.b_next.pack(side="left", padx=6)
        ttk.Button(bar, text="Close", command=self.win.destroy).pack(side="right")
        ttk.Button(bar, text=" Turn right", image=ui.icon(self.win, "rot_right", 16), compound="left",
                   command=lambda: self.rotate(90)).pack(side="right", padx=6)
        ttk.Button(bar, text=" Turn left", image=ui.icon(self.win, "rot_left", 16), compound="left",
                   command=lambda: self.rotate(270)).pack(side="right")
        self.label = tk.Label(self.win, bg="#e9e9e9", text="Loading…", font=("Segoe UI", 11))
        self.label.pack(fill="both", expand=True)
        self.img = None
        self.base = None   # processed page at rotation 0, cached per page
        self.win.grab_set()  # page indices must not change underneath the preview
        self.load()

    def _page(self):
        return self.sw.plan.groups[self.g][self.i]

    def load(self):
        ph = self._page()
        grp = self.sw.plan.groups[self.g]
        self.win.title(f"Page {self.i + 1} of {len(grp)}")
        self.b_prev.state(["!disabled"] if self.i > 0 else ["disabled"])
        self.b_next.state(["!disabled"] if self.i < len(grp) - 1 else ["disabled"])
        self.label.configure(image="", text="Loading…")
        self.base = None
        opts = self.sw.opts

        def work():
            from photo2pdf.core import load_page
            try:
                im = load_page(ph.path, opts, max_edge=2400)
            except Exception as exc:  # noqa: BLE001
                logging.exception("preview failed")
                im = exc
            try:
                self.win.after(0, lambda: self._loaded(ph, im))
            except Exception:  # noqa: BLE001 - window closed meanwhile
                pass
        threading.Thread(target=work, name="preview", daemon=True).start()

    def _loaded(self, ph, im):
        if not self.win.winfo_exists() or ph is not self._page():
            return
        if isinstance(im, Exception):
            self.label.configure(text=f"This page can't be shown.\n{im}")
            return
        self.base = im
        self.render()

    def render(self):
        from PIL import ImageTk

        from photo2pdf.core import rotate_cw

        if self.base is None:
            return
        im = rotate_cw(self.base, self._page().rotation)
        self.win.update_idletasks()
        w, h = max(200, self.label.winfo_width() - 16), max(200, self.label.winfo_height() - 16)
        im = im.copy()
        im.thumbnail((w, h))
        self.img = ImageTk.PhotoImage(im, master=self.win)
        self.label.configure(image=self.img, text="")

    def rotate(self, deg):
        self.sw._edit(self.sw.plan.rotate, self.g, self.i, deg)
        self.render()

    def step(self, d):
        grp = self.sw.plan.groups[self.g]
        j = self.i + d
        if 0 <= j < len(grp):
            self.i = j
            self.load()
