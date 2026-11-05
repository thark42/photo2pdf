"""Diagnostics: rolling log, crash capture, and a problem-report zip.

What gets logged:
  * every session: app/OS/Python/Tesseract versions, bundled languages, settings
  * every page: file name, size, timings, straightening result (page found,
    skew), orientation decision and how it was reached, word count,
    document yes/no, warnings
  * review-screen corrections (rotate, split, join, remove, add, rename) —
    these show where the automatic decisions were wrong
  * every error with its traceback, including uncaught exceptions in any
    thread, in Tk callbacks, and native crashes (faulthandler)

What never gets logged: the photos, the PDFs, or the OCR text of a document.
Two kinds of personal detail are reduced:
  * paths: the user's home folder becomes "~", the Windows user name "<user>"
  * recognised name fields (date / sender / type, and names the user types)
    are tagged FIELDS so the report can leave them out on request

Logs: %LOCALAPPDATA%\\Photo2PDF\\logs\\photo2pdf.log, rolling at 1 MB with 5
old files kept (about 6 MB maximum), plus crash.log for native crashes.
"""

from __future__ import annotations

import faulthandler
import json
import logging
import logging.handlers
import os
import platform
import re
import sys
import threading
import time
import traceback
import zipfile
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Optional

LOG_NAME = "photo2pdf.log"
MAX_BYTES = 1_000_000
BACKUPS = 5
FIELDS_TAG = "FIELDS"

_crash_file = None


def log_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") if os.name == "nt" else None
    d = (Path(base) if base else Path.home() / ".cache") / "Photo2PDF" / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def settings_dir() -> Path:
    base = os.environ.get("APPDATA") if os.name == "nt" else None
    d = (Path(base) if base else Path.home() / ".config") / "Photo2PDF"
    d.mkdir(parents=True, exist_ok=True)
    return d


class Redactor(logging.Filter):
    """Replace the home folder and user name in every log line."""

    def __init__(self):
        super().__init__()
        subs = []
        home = str(Path.home())
        if len(home) > 3:
            subs.append((re.compile(re.escape(home), re.I), "~"))
            subs.append((re.compile(re.escape(home.replace("\\", "/")), re.I), "~"))
        user = os.environ.get("USERNAME") or os.environ.get("USER")
        if user and len(user) >= 3:
            subs.append((re.compile(rf"(?<![\w]){re.escape(user)}(?![\w])", re.I), "<user>"))
        self.subs = subs

    def scrub(self, text: str) -> str:
        for rx, rep in self.subs:
            text = rx.sub(rep, text)
        return text

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        if record.exc_info and not record.exc_text:
            record.exc_text = "".join(traceback.format_exception(*record.exc_info))
        record.msg = self.scrub(msg)
        record.args = ()
        if record.exc_text:
            record.exc_text = self.scrub(record.exc_text)
        return True


def setup(level: int = logging.INFO) -> Path:
    """Configure the rolling log once per process. Returns the log file path."""
    root = logging.getLogger()
    path = log_dir() / LOG_NAME
    if any(getattr(h, "_photo2pdf", False) for h in root.handlers):
        return path
    handler = logging.handlers.RotatingFileHandler(
        path, maxBytes=MAX_BYTES, backupCount=BACKUPS, encoding="utf-8", delay=True)
    handler._photo2pdf = True  # type: ignore[attr-defined]
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)-7s [%(threadName)s] %(module)s: %(message)s"))
    handler.addFilter(Redactor())
    root.addHandler(handler)
    root.setLevel(level)
    _install_hooks()
    return path


def _install_hooks() -> None:
    global _crash_file

    def excepthook(exc_type, exc, tb):
        logging.critical("uncaught exception", exc_info=(exc_type, exc, tb))

    def thread_hook(args):
        logging.critical("uncaught exception in thread %s",
                         getattr(args.thread, "name", "?"),
                         exc_info=(args.exc_type, args.exc_value, args.exc_traceback))

    sys.excepthook = excepthook
    threading.excepthook = thread_hook
    try:
        _crash_file = open(log_dir() / "crash.log", "a", encoding="utf-8")  # noqa: SIM115 - kept open
        _crash_file.write(f"\n--- session {datetime.now():%Y-%m-%d %H:%M:%S} pid {os.getpid()} ---\n")
        _crash_file.flush()
        faulthandler.enable(file=_crash_file, all_threads=True)
    except OSError:
        pass


def hook_tk(root) -> None:
    """Log exceptions raised inside Tk callbacks (Tk otherwise prints them to a console she can't see)."""
    def report(exc, val, tb):
        logging.error("error in window callback", exc_info=(exc, val, tb))
    root.report_callback_exception = report


def fields(msg: str, *args) -> None:
    """Log something derived from document content (name fields, typed names)."""
    logging.info(f"{FIELDS_TAG} " + msg, *args)


@contextmanager
def timed(label: str, **extra):
    t = time.perf_counter()
    try:
        yield
    finally:
        bits = " ".join(f"{k}={v}" for k, v in extra.items())
        logging.info("%s took %.2fs %s", label, time.perf_counter() - t, bits)


def system_info(tess=None) -> dict:
    from photo2pdf import __version__

    info = {
        "app_version": __version__,
        "frozen": bool(getattr(sys, "frozen", False)),
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "machine": platform.machine(),
        "cpu_count": os.cpu_count(),
        "time": datetime.now().isoformat(timespec="seconds"),
    }
    for mod in ("PIL", "cv2", "numpy", "pypdf", "img2pdf", "pillow_heif", "tkinterdnd2"):
        try:
            m = __import__(mod)
            info[f"lib_{mod}"] = getattr(m, "__version__", "present")
        except Exception as exc:  # noqa: BLE001
            info[f"lib_{mod}"] = f"missing ({type(exc).__name__})"
    if tess is not None:
        try:
            r = tess._run(["--version"], cwd=Path.cwd(), timeout=30)
            info["tesseract"] = ((r.stdout or r.stderr or "").splitlines() or ["?"])[0]
            info["tesseract_langs"] = sorted(tess.languages())
        except Exception as exc:  # noqa: BLE001
            info["tesseract"] = f"error: {exc}"
    else:
        info["tesseract"] = "not found"
    return info


def log_session_header(tess=None, opts=None) -> None:
    logging.info("=" * 60)
    logging.info("session start %s", json.dumps(system_info(tess)))
    if opts is not None:
        from dataclasses import asdict

        logging.info("settings %s", json.dumps(asdict(opts)))


def build_report(message: str, include_fields: bool = True, tess=None,
                 out_dir: Optional[Path] = None) -> Path:
    """Zip logs + system info + her message. Returns the zip path (Desktop, else Documents)."""
    for h in logging.getLogger().handlers:
        try:
            h.flush()
        except Exception:  # noqa: BLE001
            pass
    if out_dir is None:
        out_dir = Path.home() / "Desktop"
        if not out_dir.is_dir():
            out_dir = Path.home() / "Documents"
        if not out_dir.is_dir():
            out_dir = Path.home()
    from photo2pdf.core import unique_path

    out = unique_path(out_dir / f"Photos to PDF report {datetime.now():%Y-%m-%d %H%M}.zip")
    red = Redactor()
    ld = log_dir()
    logs = sorted(ld.glob(LOG_NAME + "*"), key=lambda p: p.name, reverse=True) + \
        [p for p in [ld / "crash.log"] if p.exists()]

    def clean(text: str) -> str:
        text = red.scrub(text)
        if not include_fields:
            text = "\n".join(
                (ln.split(f" {FIELDS_TAG} ", 1)[0] + f" {FIELDS_TAG} [left out]") if f" {FIELDS_TAG} " in ln
                else ln for ln in text.splitlines())
        return text

    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("message.txt", (message or "(no message)").strip() + "\n")
        z.writestr("system.json", json.dumps(system_info(tess), indent=2))
        z.writestr("report-info.txt",
                   f"created {datetime.now().isoformat(timespec='seconds')}\n"
                   f"recognised names included: {'yes' if include_fields else 'no'}\n")
        for p in logs:
            try:
                z.writestr(f"logs/{p.name}", clean(p.read_text("utf-8", errors="replace")))
            except OSError:
                continue
        sd = settings_dir()
        for name in ("settings.json", "naming.json") if include_fields else ("settings.json",):
            f = sd / name
            if f.exists():
                try:
                    z.writestr(name, red.scrub(f.read_text("utf-8", errors="replace")))
                except OSError:
                    pass
    logging.info("problem report written: %s (fields included: %s)", out.name, include_fields)
    return out
