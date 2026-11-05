"""Folder mode: find which photos are documents and group them into multi-page documents.

No machine-learning model is involved. Detection uses the bundled Tesseract:
  * The photo is shrunk to a 1600 px long edge and greyscaled, then run
    through Tesseract with `--psm 1` (page segmentation with orientation
    detection, so sideways pages still read).
  * Lighting is flattened first (see core.flatten_lighting); on a real
    unevenly lit page photo this raised the count from 8 to 36 words.
  * Words with confidence >= 60 that are at least 3 characters and mostly
    letters or digits are counted. A photo with at least `min_words` of them
    is a document. The thresholds are heuristics tuned on a small synthetic +
    sample set, not on a labelled corpus; the review screen exists because
    they will sometimes be wrong.

Grouping:
  * Each photo's capture time comes from EXIF DateTimeOriginal, then a
    timestamp in the filename (IMG_20241105_143210, PXL_..., "WhatsApp Image
    2024-11-05 at 14.32.10"), then the file's modified time.
  * Document photos sorted by time are cut into separate documents wherever
    the gap between neighbours exceeds `gap_seconds`, a non-document photo
    was taken in between, the folder changes, or a page shows a different
    sender or document date than the pages before it (a new letter).

Results are cached per (path, size, mtime) so re-scanning a folder is fast.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
import os
import re
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from photo2pdf.core import (
    IMAGE_EXTS, Options, Tesseract, _open_image, _resize_long, count_words, find_rotation,
    flatten_lighting, load_page, natural_key, rotate_cw,
)
from photo2pdf.naming import DocFields, extract, merge, sanitize, suggest_name, text_from_tsv

DETECT_LONG_EDGE = 1600
THUMB_SIZE = 160
CACHE_VERSION = 2


@dataclass
class PhotoInfo:
    path: Path
    taken: datetime
    words: int = 0
    is_document: bool = False
    rotation: int = 0                     # clockwise turn after straightening
    fields: Optional[DocFields] = None    # date / sender / type from OCR text
    saved_as: Optional[Path] = None       # PDF this photo already went into (registry)
    error: Optional[str] = None
    thumb: Optional[object] = field(default=None, repr=False, compare=False)  # PIL.Image


@dataclass
class ScanSettings:
    min_words: int = 12
    gap_seconds: int = 180
    recursive: bool = True   # disorganized archives are usually spread over subfolders
    workers: int = max(1, min(8, (os.cpu_count() or 2)))


# --------------------------------------------------------------------------
# Capture time
# --------------------------------------------------------------------------

_NAME_PATTERNS = [
    # IMG_20241105_143210, PXL_20241105_143210123, 20241105_143210, Screenshot_20241105-143210
    re.compile(r"(?<!\d)(20\d{2})(\d{2})(\d{2})[_\-T ]?(\d{2})(\d{2})(\d{2})"),
    # WhatsApp Image 2024-11-05 at 14.32.10 / 2024-11-05 14.32.10 / 2024-11-05_14-32-10
    re.compile(r"(20\d{2})-(\d{2})-(\d{2})(?: at |[ _T])(\d{2})[.\-:](\d{2})[.\-:](\d{2})"),
]


def time_from_name(name: str) -> Optional[datetime]:
    for pat in _NAME_PATTERNS:
        m = pat.search(name)
        if m:
            try:
                return datetime(*map(int, m.groups()))
            except ValueError:
                continue
    return None


def _exif_time(im) -> Optional[datetime]:
    try:
        exif = im.getexif()
        raw = exif.get_ifd(0x8769).get(0x9003) or exif.get(0x0132)  # DateTimeOriginal, DateTime
        if raw:
            return datetime.strptime(str(raw).strip()[:19], "%Y:%m:%d %H:%M:%S")
    except Exception:  # noqa: BLE001 - malformed EXIF is common
        pass
    return None


# --------------------------------------------------------------------------
# Detection
# --------------------------------------------------------------------------

def _analyse(path: Path, tess: Tesseract, settings: ScanSettings, opts: Options,
             cached: Optional[dict] = None) -> PhotoInfo:
    """Straighten, OCR, decide document-ness; for documents also orientation + name fields."""
    from photo2pdf import diag

    taken = None
    t0 = time.perf_counter()
    try:
        with _open_image(path) as raw:
            taken = _exif_time(raw)
        if taken is None:
            taken = _fallback_time(path)
        im = load_page(path, opts, max_edge=2000)
        if cached is not None:
            info = PhotoInfo(path=path, taken=taken, words=cached["words"],
                             rotation=cached.get("rotation", 0),
                             fields=DocFields.from_json(cached["fields"]) if cached.get("fields") else None)
        else:
            gray = flatten_lighting(_resize_long(im.convert("L"), DETECT_LONG_EDGE))
            with tempfile.TemporaryDirectory(prefix="photo2pdf-d-") as td:
                work = Path(td)
                img = work / "d.png"
                gray.save(img)
                r = tess._run([img.name, "stdout", "-l", opts.language, "--psm", "1", "tsv"],
                              cwd=work, timeout=180, threads=1)
                tsv = r.stdout or ""
                words = count_words(tsv)
                info = PhotoInfo(path=path, taken=taken, words=words)
                if words >= settings.min_words:
                    info.rotation = find_rotation(im, tess, opts.language, work, threads=1)
                    info.fields = extract(text_from_tsv(tsv))
        info.is_document = info.words >= settings.min_words
        logging.info("scan %s: words=%d document=%s rotation=%d cached=%s taken=%s (%.2fs)",
                     path.name, info.words, info.is_document, info.rotation, cached is not None,
                     info.taken.isoformat(timespec="minutes"), time.perf_counter() - t0)
        if info.fields:
            diag.fields("scan %s fields %s", path.name, info.fields.to_json())
        thumb = rotate_cw(im, info.rotation) if info.is_document else im
        thumb = thumb.copy()
        thumb.thumbnail((THUMB_SIZE, THUMB_SIZE))
        info.thumb = thumb
        return info
    except Exception as exc:  # noqa: BLE001
        logging.exception("scan failed for %s", path.name)
        return PhotoInfo(path=path, taken=taken or _fallback_time(path), error=str(exc))


def _fallback_time(path: Path) -> datetime:
    t = time_from_name(path.name)
    if t:
        return t
    try:
        return datetime.fromtimestamp(path.stat().st_mtime)
    except OSError:
        return datetime.fromtimestamp(0)


_SKIP_DIRS = {"appdata", "$recycle.bin", "system volume information", "node_modules"}


def _walk(folder: Path):
    """Files under folder, skipping hidden/system folders (".x", "$x", AppData, ...)."""
    try:
        entries = list(folder.iterdir())
    except OSError:
        return
    for p in entries:
        try:
            if p.is_dir():
                if p.name.startswith((".", "$")) or p.name.lower() in _SKIP_DIRS:
                    continue
                yield from _walk(p)
            elif p.is_file():
                yield p
        except OSError:
            continue


def list_folder(folder: Path, recursive: bool = False) -> list[Path]:
    it = _walk(folder) if recursive else (p for p in folder.iterdir() if p.is_file())
    return sorted((p for p in it if p.suffix.lower() in IMAGE_EXTS), key=natural_key)


def list_inputs(paths: list[Path], recursive: bool = False) -> list[Path]:
    """Photos from any mix of folders and files, de-duplicated."""
    seen: dict[Path, None] = {}
    for p in paths:
        p = Path(p)
        if p.is_dir():
            for f in list_folder(p, recursive):
                seen.setdefault(f.resolve(), None)
        elif p.is_file() and p.suffix.lower() in IMAGE_EXTS:
            seen.setdefault(p.resolve(), None)
    return list(seen)


# -- cache -----------------------------------------------------------------

def _cache_file() -> Path:
    base = os.environ.get("LOCALAPPDATA") if os.name == "nt" else None
    d = (Path(base) if base else Path.home() / ".cache") / "Photo2PDF"
    d.mkdir(parents=True, exist_ok=True)
    return d / "scan-cache.json"


def _cache_key(p: Path, opts: Options) -> str:
    st = p.stat()
    raw = f"{p.resolve()}|{st.st_size}|{st.st_mtime_ns}|{opts.language}|{opts.straighten}"
    return hashlib.sha1(raw.encode()).hexdigest()


def _load_cache() -> dict:
    try:
        data = json.loads(_cache_file().read_text("utf-8"))
        return data.get("entries", {}) if data.get("v") == CACHE_VERSION else {}
    except (OSError, ValueError):
        return {}


def _save_cache(entries: dict) -> None:
    try:
        tmp = _cache_file().with_suffix(".tmp")
        tmp.write_text(json.dumps({"v": CACHE_VERSION, "entries": entries}), "utf-8")
        os.replace(tmp, _cache_file())
    except OSError:
        pass


def scan_folder(folder: Path, settings: "ScanSettings", tess: Tesseract, **kw) -> list[PhotoInfo]:
    return scan_paths([folder], settings, tess, **kw)


def scan_paths(
    inputs: list[Path],
    settings: ScanSettings,
    tess: Tesseract,
    progress: Optional[Callable[[int, int, str], None]] = None,
    cancel: Optional[threading.Event] = None,
    use_cache: bool = True,
    opts: Optional[Options] = None,
) -> list[PhotoInfo]:
    """Analyse every photo in `inputs` (folders and/or files). Returns PhotoInfo in time order.

    The cache keeps word counts, rotation and the extracted name fields
    (date / sender / type), never the full OCR text.
    """
    opts = opts or Options()
    progress = progress or (lambda d, t, m: None)
    paths = list_inputs(list(inputs), settings.recursive)
    total = len(paths)
    cache = _load_cache() if use_cache else {}
    results: list[PhotoInfo] = []
    lock = threading.Lock()
    done = 0

    def job(p: Path) -> Optional[PhotoInfo]:
        nonlocal done
        if cancel and cancel.is_set():
            return None
        try:
            key = _cache_key(p, opts)
        except OSError:
            key = None
        hit = cache.get(key) if key else None
        info = _analyse(p, tess, settings, opts, cached=hit)
        with lock:
            done += 1
            progress(done, total, f"Checked {done} of {total}: {p.name}")
            if use_cache and key and info.error is None:
                cache[key] = {"words": info.words, "rotation": info.rotation,
                              "fields": info.fields.to_json() if info.fields else None}
        return info

    progress(0, total, f"Found {total} photos")
    logging.info("scan_folder: %d photos, settings=%s, cache_entries=%d", total, settings, len(cache))
    t_scan = time.perf_counter()
    # Each Tesseract process is limited to 1 thread; parallelism comes from running several.
    with ThreadPoolExecutor(max_workers=settings.workers) as pool:
        for info in pool.map(job, paths):
            if info is not None:
                results.append(info)
    if use_cache:
        _save_cache(cache)
    # Re-apply threshold (cache stores raw word counts so changing min_words needs no rescan).
    for r in results:
        r.is_document = r.error is None and r.words >= settings.min_words
    results.sort(key=lambda r: (r.taken, natural_key(r.path)))
    from photo2pdf import registry

    saved = registry.lookup([r.path for r in results])
    for r in results:
        r.saved_as = saved.get(r.path)
    n_doc = sum(r.is_document for r in results)
    n_err = sum(r.error is not None for r in results)
    logging.info("scan_folder done: %d checked, %d documents, %d errors, cancelled=%s (%.1fs)",
                 len(results), n_doc, n_err, bool(cancel and cancel.is_set()),
                 time.perf_counter() - t_scan)
    return results


# --------------------------------------------------------------------------
# Grouping + editable plan
# --------------------------------------------------------------------------

def _new_letterhead(current: list[PhotoInfo], ph: PhotoInfo) -> bool:
    """True when `ph` shows a different sender or document date than the document so far.

    A follow-on page normally has no letterhead, or repeats the same one; a
    different sender or date means a new letter was photographed right after.
    """
    if not ph.fields or not current:
        return False
    so_far = merge([p.fields for p in current if p.fields])
    f = ph.fields
    return bool((f.sender and so_far.sender and f.sender != so_far.sender)
                or (f.date and so_far.date and f.date != so_far.date))


def group_documents(photos: list[PhotoInfo], gap_seconds: int) -> list[list[PhotoInfo]]:
    """Split time-ordered photos into documents; non-documents and folder changes act as separators."""
    groups: list[list[PhotoInfo]] = []
    current: list[PhotoInfo] = []
    prev: Optional[PhotoInfo] = None
    for ph in photos:
        if not ph.is_document:
            if current:
                groups.append(current)
                current = []
            prev = None
            continue
        if prev is not None and ((ph.taken - prev.taken).total_seconds() > gap_seconds
                                 or ph.path.parent != prev.path.parent
                                 or _new_letterhead(current, ph)):
            groups.append(current)
            current = []
        current.append(ph)
        prev = ph
    if current:
        groups.append(current)
    return groups


ORGANIZE_CHOICES = {
    "none": "All in one folder",
    "year": "One folder per year",
    "sender": "One folder per sender",
    "sender_year": "Per sender, then per year",
}


@dataclass
class Plan:
    """What will be saved. The review screen edits this; the tests exercise it directly."""

    groups: list[list[PhotoInfo]]
    others: list[PhotoInfo]  # not documents (or removed by the user)
    custom_names: dict = field(default_factory=dict)  # first page path -> name typed by the user
    excluded: set = field(default_factory=set)        # first page paths of documents not to save
    history: list = field(default_factory=list, repr=False)

    @classmethod
    def from_scan(cls, photos: list[PhotoInfo], gap_seconds: int) -> "Plan":
        groups = group_documents(photos, gap_seconds)
        others = [p for p in photos if not p.is_document]
        # Near-misses first so the user sees likely mistakes at the top.
        others.sort(key=lambda p: -p.words)
        plan = cls(groups, others)
        # Documents saved in an earlier session start unticked: no duplicates by default.
        plan.excluded = {g[0].path for g in groups if all(p.saved_as for p in g)}
        return plan

    # -- undo ---------------------------------------------------------------

    def checkpoint(self) -> None:
        """Remember the current state; call before every edit."""
        snap = ([[(p, p.rotation, p.thumb) for p in g] for g in self.groups],
                [(p, p.rotation, p.thumb) for p in self.others],
                dict(self.custom_names), set(self.excluded))
        self.history.append(snap)
        del self.history[:-50]

    def can_undo(self) -> bool:
        return bool(self.history)

    def undo(self) -> None:
        if not self.history:
            return
        groups, others, names, excluded = self.history.pop()

        def back(items):
            out = []
            for p, rot, thumb in items:
                p.rotation, p.thumb = rot, thumb
                out.append(p)
            return out

        self.groups = [back(g) for g in groups]
        self.others = back(others)
        self.custom_names, self.excluded = names, excluded

    # -- which documents get saved -------------------------------------------

    def is_saved(self, g: int) -> bool:
        return all(p.saved_as for p in self.groups[g])

    def is_included(self, g: int) -> bool:
        return self.groups[g][0].path not in self.excluded

    def set_included(self, g: int, include: bool) -> None:
        key = self.groups[g][0].path
        (self.excluded.discard if include else self.excluded.add)(key)

    def included_indices(self) -> list[int]:
        return [g for g in range(len(self.groups)) if self.is_included(g)]

    def mark_saved(self, g: int, pdf: Path) -> None:
        for p in self.groups[g]:
            p.saved_as = pdf
        self.excluded.add(self.groups[g][0].path)

    # -- edits ----------------------------------------------------------------

    def split(self, g: int, i: int) -> None:
        """Start a new document at page i of group g."""
        if 0 < i < len(self.groups[g]):
            grp = self.groups[g]
            self.groups[g:g + 1] = [grp[:i], grp[i:]]

    def remove_group(self, g: int) -> None:
        self.others[0:0] = self.groups.pop(g)

    def join_previous(self, g: int) -> None:
        if g > 0:
            self.excluded.discard(self.groups[g][0].path)
            self.groups[g - 1].extend(self.groups.pop(g))

    def remove(self, g: int, i: int) -> None:
        ph = self.groups[g].pop(i)
        if not self.groups[g]:
            self.groups.pop(g)
        self.others.insert(0, ph)

    def rotate(self, g: int, i: int, degrees_cw: int) -> None:
        ph = self.groups[g][i]
        ph.rotation = (ph.rotation + degrees_cw) % 360
        if ph.thumb is not None:
            ph.thumb = rotate_cw(ph.thumb, degrees_cw)

    def set_name(self, g: int, name: str) -> None:
        name = name.strip()
        key = self.groups[g][0].path
        if name:
            self.custom_names[key] = sanitize(name[:-4] if name.lower().endswith(".pdf") else name) + ".pdf"
        else:
            self.custom_names.pop(key, None)

    def move(self, g: int, i: int, delta: int) -> None:
        grp = self.groups[g]
        j = i + delta
        if 0 <= j < len(grp):
            grp[i], grp[j] = grp[j], grp[i]

    def add(self, other_index: int) -> None:
        """Turn a non-document photo into its own document, placed in time order."""
        ph = self.others.pop(other_index)
        idx = 0
        while idx < len(self.groups) and self.groups[idx][0].taken <= ph.taken:
            idx += 1
        self.groups.insert(idx, [ph])

    # -- names and folders ----------------------------------------------------

    def fields(self, g: int) -> DocFields:
        return merge([p.fields for p in self.groups[g] if p.fields])

    def suggested_name(self, g: int) -> str:
        grp = self.groups[g]
        width = max(2, len(str(len(self.groups))))
        t = grp[0].taken
        pages = f"{len(grp)} page" + ("s" if len(grp) != 1 else "")
        fallback = f"Document {g + 1:0{width}d} - {t:%Y-%m-%d %H.%M} ({pages}).pdf"
        return suggest_name(self.fields(g), fallback)

    def folder_for(self, g: int, organize: str) -> str:
        """Subfolder (possibly empty) a document goes into for the chosen organize mode."""
        f = self.fields(g)
        year = str(f.date.year) if f.date else (str(f.year) if f.year else "Undated")
        sender = sanitize(f.sender) if f.sender else "Other"
        return {"year": year, "sender": sender, "sender_year": f"{sender}/{year}"}.get(organize, "")

    def output_names(self) -> list[str]:
        """Final names: user's edit, else content-based suggestion, else dated fallback. Unique."""
        names, seen = [], {}
        for g, grp in enumerate(self.groups):
            name = self.custom_names.get(grp[0].path) or self.suggested_name(g)
            stem, n = name[:-4], seen.get(name.lower(), 0)
            seen[name.lower()] = n + 1
            names.append(name if n == 0 else f"{stem} ({n + 1}).pdf")
        return names

    def output_paths(self, organize: str) -> list[str]:
        """Relative paths ("2025/name.pdf") for every document."""
        return [f"{folder}/{name}" if folder else name
                for folder, name in ((self.folder_for(g, organize), n)
                                     for g, n in enumerate(self.output_names()))]
