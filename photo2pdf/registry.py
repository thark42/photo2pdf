"""Remembers which photos have already been saved into which PDF.

Stops duplicates when the same folder is worked through over several
sessions, when Save is pressed twice, or after a stopped save. Keyed per
photo by (resolved path, size, mtime), like the scan cache, so an edited
or replaced photo counts as new. A record is only trusted while the PDF it
points at still exists: delete a PDF and its photos become "not saved"
again.

File: %LOCALAPPDATA%\\Photo2PDF\\saved.json (no document text, only paths).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional

_lock = threading.Lock()
VERSION = 1


def _file() -> Path:
    base = os.environ.get("LOCALAPPDATA") if os.name == "nt" else None
    d = (Path(base) if base else Path.home() / ".cache") / "Photo2PDF"
    d.mkdir(parents=True, exist_ok=True)
    return d / "saved.json"


def photo_key(p: Path) -> Optional[str]:
    try:
        st = p.stat()
    except OSError:
        return None
    raw = f"{p.resolve()}|{st.st_size}|{st.st_mtime_ns}"
    return hashlib.sha1(raw.encode()).hexdigest()


def _load() -> dict:
    try:
        data = json.loads(_file().read_text("utf-8"))
        return data.get("entries", {}) if data.get("v") == VERSION else {}
    except (OSError, ValueError):
        return {}


def _store(entries: dict) -> None:
    f = _file()
    tmp = f.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps({"v": VERSION, "entries": entries}), "utf-8")
        os.replace(tmp, f)
    except OSError:
        logging.exception("could not write saved-documents record")


def lookup(photos: Iterable[Path]) -> dict[Path, Path]:
    """photo -> PDF it was saved into, for photos whose PDF still exists."""
    with _lock:
        entries = _load()
    out: dict[Path, Path] = {}
    for p in photos:
        k = photo_key(p)
        rec = entries.get(k) if k else None
        if rec:
            pdf = Path(rec["pdf"])
            if pdf.exists():
                out[p] = pdf
    return out


def record(photos: Iterable[Path], pdf: Path) -> None:
    now = datetime.now().isoformat(timespec="seconds")
    with _lock:
        entries = _load()
        for p in photos:
            k = photo_key(p)
            if k:
                entries[k] = {"pdf": str(pdf), "when": now}
        _store(entries)
