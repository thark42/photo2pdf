"""Shared Tk helpers: DPI scaling, drawn icons, tooltips, result dialog, keep-awake, ETA."""

from __future__ import annotations

import logging
import os
import sys
import time
from typing import Callable, Optional

# --------------------------------------------------------------------------
# Windows integration
# --------------------------------------------------------------------------


def enable_dpi_awareness() -> None:
    """Without this, Windows bitmap-stretches the whole window on 125-150% displays (blurry)."""
    if os.name != "nt":
        return
    try:
        import ctypes

        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)  # system-DPI aware
        except Exception:  # noqa: BLE001 - older Windows
            ctypes.windll.user32.SetProcessDPIAware()
    except Exception:  # noqa: BLE001
        logging.exception("could not enable DPI awareness")


def main_thread_gc(root, fast_ms: int = 1000, full_every: int = 30) -> None:
    """Run Python's garbage collector only on the Tk (main) thread.

    Tkinter objects (PhotoImage, StringVar, ...) that end up in reference cycles are
    freed by the cyclic GC, which otherwise runs in whichever thread happens to be
    allocating -- often a scan/save worker. Tk must only be touched from its own
    thread; on Windows a collection in a worker crashed the process (seen in CI as
    'Garbage-collecting' in a worker thread inside a Tk-using test run). Automatic
    GC is switched off and collections are scheduled on the Tk event loop instead:
    young generation every second, full every `full_every` seconds.
    """
    import gc

    gc.disable()
    state = {"n": 0}

    def tick():
        try:
            state["n"] += 1
            gc.collect(2 if state["n"] % full_every == 0 else 0)
        finally:
            try:
                root.after(fast_ms, tick)
            except Exception:  # noqa: BLE001 - root destroyed
                pass

    root.after(fast_ms, tick)


def keep_awake(on: bool) -> None:
    """Stop Windows sleeping during a long scan or save (the screen may still turn off)."""
    if os.name != "nt":
        return
    try:
        import ctypes

        ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
        ctypes.windll.kernel32.SetThreadExecutionState(
            ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if on else 0))
    except Exception:  # noqa: BLE001
        logging.exception("keep_awake failed")


# --------------------------------------------------------------------------
# Scaling and window fitting
# --------------------------------------------------------------------------


def scale_of(widget) -> float:
    """1.0 at 96 dpi, 1.5 at 144 dpi, ... (never below 1)."""
    try:
        return max(1.0, float(widget.winfo_fpixels("1i")) / 96.0)
    except Exception:  # noqa: BLE001
        return 1.0


def fit_window(win, want_w: int, want_h: int, min_w: int = 640, min_h: int = 460) -> None:
    """Size a window to the screen: never larger than the visible area, centred."""
    win.update_idletasks()
    s = scale_of(win)
    sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
    # Leave room for the taskbar and title bar.
    w = min(int(want_w * s), sw - int(40 * s))
    h = min(int(want_h * s), sh - int(110 * s))
    w, h = max(w, min(int(min_w * s), sw)), max(h, min(int(min_h * s), sh - 60))
    x, y = max(0, (sw - w) // 2), max(0, (sh - h) // 2 - int(20 * s))
    win.geometry(f"{w}x{h}+{x}+{y}")
    win.minsize(min(int(min_w * s), w), min(int(min_h * s), h))


# --------------------------------------------------------------------------
# Icons (drawn with Pillow so they don't depend on fonts having the glyphs)
# --------------------------------------------------------------------------

_icon_cache: dict = {}


def icon(widget, name: str, size: int = 18):
    from PIL import Image, ImageDraw, ImageTk

    s = scale_of(widget)
    px = max(12, int(size * s))
    key = (str(widget.winfo_toplevel()), name, px)
    if key in _icon_cache:
        return _icon_cache[key]
    ss = 4  # supersample, then shrink: smooth lines
    n = px * ss
    im = Image.new("RGBA", (n, n), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    col = (40, 40, 40, 255)
    w = max(2, n // 9)
    m = n // 6
    if name in ("rot_left", "rot_right"):
        box = [m, m, n - m, n - m]
        if name == "rot_right":
            d.arc(box, start=200, end=500, fill=col, width=w)
            tip = (n - m, n // 2)
            d.polygon([(tip[0] - 3 * w, tip[1] - w), (tip[0] + 2 * w, tip[1] - w), (tip[0] - w // 2, tip[1] + 3 * w)],
                      fill=col)
        else:
            d.arc(box, start=40, end=340, fill=col, width=w)
            tip = (m, n // 2)
            d.polygon([(tip[0] - 2 * w, tip[1] - w), (tip[0] + 3 * w, tip[1] - w), (tip[0] + w // 2, tip[1] + 3 * w)],
                      fill=col)
    elif name == "remove":
        c = (170, 40, 40, 255)
        d.line([(m, m), (n - m, n - m)], fill=c, width=w)
        d.line([(n - m, m), (m, n - m)], fill=c, width=w)
    elif name == "split":
        # vertical dashed line with arrows pointing apart: "separate here"
        for y in range(m, n - m, 3 * w):
            d.line([(n // 2, y), (n // 2, min(y + 2 * w, n - m))], fill=col, width=w)
        d.polygon([(n // 2 - 2 * w, n // 2), (m, n // 2 - 2 * w), (m, n // 2 + 2 * w)], fill=col)
        d.polygon([(n // 2 + 2 * w, n // 2), (n - m, n // 2 - 2 * w), (n - m, n // 2 + 2 * w)], fill=col)
    elif name == "zoom":
        r = n // 3
        d.ellipse([m, m, m + 2 * r, m + 2 * r], outline=col, width=w)
        d.line([(m + 2 * r - w, m + 2 * r - w), (n - m, n - m)], fill=col, width=int(w * 1.5))
    elif name == "undo":
        d.arc([m, m + n // 8, n - m, n - m + n // 8], start=180, end=360, fill=col, width=w)
        d.polygon([(m - w, n // 2 + n // 8), (m + 3 * w, n // 2 + n // 8), (m + w, n // 2 + n // 8 + 3 * w)],
                  fill=col)
    img = ImageTk.PhotoImage(im.resize((px, px), Image.LANCZOS), master=widget)
    _icon_cache[key] = img
    return img


# --------------------------------------------------------------------------
# Tooltip
# --------------------------------------------------------------------------


class Tooltip:
    def __init__(self, widget, text: str, delay_ms: int = 450):
        self.widget, self.text, self.delay = widget, text, delay_ms
        self.tip = None
        self._after = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")

    def _schedule(self, _e=None):
        self._after = self.widget.after(self.delay, self._show)

    def _show(self):
        import tkinter as tk

        if self.tip or not self.widget.winfo_exists():
            return
        x = self.widget.winfo_rootx() + 10
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 4
        self.tip = tk.Toplevel(self.widget)
        self.tip.wm_overrideredirect(True)
        self.tip.wm_geometry(f"+{x}+{y}")
        tk.Label(self.tip, text=self.text, bg="#ffffe6", fg="#222", relief="solid", bd=1,
                 font=("Segoe UI", 9), padx=6, pady=3, justify="left", wraplength=320).pack()

    def _hide(self, _e=None):
        if self._after:
            self.widget.after_cancel(self._after)
            self._after = None
        if self.tip:
            self.tip.destroy()
            self.tip = None


# --------------------------------------------------------------------------
# Time estimate
# --------------------------------------------------------------------------


class Eta:
    """Estimate time left from progress events. Shows nothing until it has a few samples."""

    def __init__(self):
        self.t0: Optional[float] = None
        self.d0 = 0

    def text(self, done: int, total: int) -> str:
        now = time.monotonic()
        if self.t0 is None or done < self.d0:
            self.t0, self.d0 = now, done
            return ""
        n = done - self.d0
        if n < 3 or total <= done:
            return ""
        left = (now - self.t0) / n * (total - done)
        if left < 60:
            return "less than a minute left"
        mins = round(left / 60)
        if mins < 90:
            return f"about {mins} minute{'s' if mins != 1 else ''} left"
        return f"about {left / 3600:.1f} hours left"


# --------------------------------------------------------------------------
# Result dialog: plain summary, optional details, report only when useful
# --------------------------------------------------------------------------


def show_result(parent, title: str, lines: list[str], details: list[str],
                offer_report: Optional[Callable[[str], None]] = None,
                open_label: Optional[str] = None, on_open: Optional[Callable[[], None]] = None) -> None:
    import tkinter as tk
    from tkinter import ttk

    win = tk.Toplevel(parent)
    win.title(title)
    win.transient(parent)
    win.resizable(False, False)
    frm = ttk.Frame(win, padding=16)
    frm.pack(fill="both", expand=True)
    ttk.Label(frm, text="\n".join(lines), wraplength=int(420 * scale_of(win)), justify="left",
              font=("Segoe UI", 11)).pack(anchor="w")
    det_box = None
    if details:
        det_box = tk.Text(frm, height=min(10, len(details) + 1), width=60, wrap="word",
                          font=("Segoe UI", 9), relief="solid", bd=1)
        det_box.insert("1.0", "\n".join(details))
        det_box.configure(state="disabled")

        def toggle():
            if det_box.winfo_ismapped():
                det_box.pack_forget()
                btn_det.configure(text="Show details")
            else:
                det_box.pack(fill="both", pady=(10, 0), before=row)
                btn_det.configure(text="Hide details")
        btn_det = ttk.Button(frm, text="Show details", command=toggle)
        btn_det.pack(anchor="w", pady=(10, 0))
    row = ttk.Frame(frm)
    row.pack(fill="x", pady=(14, 0))
    ok = ttk.Button(row, text="OK", command=win.destroy)
    ok.pack(side="right")
    if open_label and on_open:
        ttk.Button(row, text=open_label, command=lambda: (win.destroy(), on_open())).pack(side="right", padx=6)
    if offer_report:
        ttk.Button(row, text="Report this problem…",
                   command=lambda: (win.destroy(), offer_report("\n".join(lines + details)))).pack(side="left")
    win.bind("<Return>", lambda e: win.destroy())
    win.bind("<Escape>", lambda e: win.destroy())
    ok.focus_set()
    center_on(win, parent)
    win.grab_set()


def center_on(win, parent) -> None:
    """Place a dialog over its parent (Tk otherwise drops it in a screen corner)."""
    win.update_idletasks()
    px = parent.winfo_rootx() + (parent.winfo_width() - win.winfo_width()) // 2
    py = parent.winfo_rooty() + (parent.winfo_height() - win.winfo_height()) // 3
    win.geometry(f"+{max(0, px)}+{max(0, py)}")


def is_windows() -> bool:
    return sys.platform.startswith("win")
