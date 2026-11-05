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
    the gap between neighbours exceeds `gap_seconds`, or a non-document photo
    was taken in between.

Results are cached per (path, size, mtime) so re-scanning a folder is fast.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from photo2pdf.core import IMAGE_EXTS, Tesseract, _open_image, flatten_lighting, natural_key

DETECT_LONG_EDGE = 1600
THUMB_SIZE = 160
CACHE_VERSION = 1


@dataclass
class PhotoInfo:
    path: Path
    taken: datetime
    words: int = 0
    is_document: bool = False
    error: Optional[str] = None
    thumb: Optional[object] = field(default=None, repr=False, compare=False)  # PIL.Image


@dataclass
class ScanSettings:
    min_words: int = 12
    gap_seconds: int = 180
    recursive: bool = False
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

_WORD_OK = re.compile(r"^[^\W_]{3,}$|^[^\W_][\w'’.,:/\-]{2,}$", re.UNICODE)


def count_words(tsv: str, min_conf: float = 60.0) -> int:
    n = 0
    for line in tsv.splitlines()[1:]:
        cols = line.split("\t")
        if len(cols) < 12:
            continue
        try:
            conf = float(cols[10])
        except ValueError:
            continue
        text = cols[11].strip()
        if conf < min_conf or len(text) < 3:
            continue
        alnum = sum(ch.isalnum() for ch in text)
        if alnum / len(text) >= 0.7 and _WORD_OK.match(text):
            n += 1
    return n


def _analyse(path: Path, tess: Tesseract, settings: ScanSettings,
             cached_words: Optional[int] = None) -> PhotoInfo:
    from PIL import Image, ImageOps

    taken = None
    try:
        with _open_image(path) as im:
            im.load()
            taken = _exif_time(im)
            im = ImageOps.exif_transpose(im)
            thumb = im.convert("RGB")
            thumb.thumbnail((THUMB_SIZE, THUMB_SIZE))
            im = im.convert("L")
            if cached_words is not None:
                return PhotoInfo(path=path, taken=taken or _fallback_time(path), words=cached_words,
                                 is_document=cached_words >= settings.min_words, thumb=thumb)
            scale = DETECT_LONG_EDGE / max(im.size)
            im = im.resize((max(1, round(im.width * scale)), max(1, round(im.height * scale))),
                           Image.LANCZOS)
            im = flatten_lighting(im)
            with tempfile.TemporaryDirectory(prefix="photo2pdf-d-") as td:
                img = Path(td) / "d.png"
                im.save(img)
                r = tess._run([img.name, "stdout", "--psm", "1", "tsv"], cwd=img.parent, timeout=120,
                              threads=1)
            words = count_words(r.stdout or "")
    except Exception as exc:  # noqa: BLE001
        return PhotoInfo(path=path, taken=_fallback_time(path), error=str(exc))
    if taken is None:
        taken = _fallback_time(path)
    return PhotoInfo(path=path, taken=taken, words=words,
                     is_document=words >= settings.min_words, thumb=thumb)


def _fallback_time(path: Path) -> datetime:
    t = time_from_name(path.name)
    if t:
        return t
    try:
        return datetime.fromtimestamp(path.stat().st_mtime)
    except OSError:
        return datetime.fromtimestamp(0)


def list_folder(folder: Path, recursive: bool = False) -> list[Path]:
    it = folder.rglob("*") if recursive else folder.iterdir()
    return sorted((p for p in it if p.is_file() and p.suffix.lower() in IMAGE_EXTS), key=natural_key)


# -- cache -----------------------------------------------------------------

def _cache_file() -> Path:
    base = os.environ.get("LOCALAPPDATA") if os.name == "nt" else None
    d = (Path(base) if base else Path.home() / ".cache") / "Photo2PDF"
    d.mkdir(parents=True, exist_ok=True)
    return d / "scan-cache.json"


def _cache_key(p: Path) -> str:
    st = p.stat()
    return hashlib.sha1(f"{p.resolve()}|{st.st_size}|{st.st_mtime_ns}".encode()).hexdigest()


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


def scan_folder(
    folder: Path,
    settings: ScanSettings,
    tess: Tesseract,
    progress: Optional[Callable[[int, int, str], None]] = None,
    cancel: Optional[threading.Event] = None,
    use_cache: bool = True,
) -> list[PhotoInfo]:
    """Analyse every photo in `folder`. Returns PhotoInfo in time order."""
    progress = progress or (lambda d, t, m: None)
    paths = list_folder(folder, settings.recursive)
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
            key = _cache_key(p)
        except OSError:
            key = None
        hit = cache.get(key) if key else None
        info = _analyse(p, tess, settings, cached_words=hit["words"] if hit else None)
        with lock:
            done += 1
            progress(done, total, f"Checked {done} of {total}: {p.name}")
            if use_cache and key and info.error is None:
                cache[key] = {"words": info.words}
        return info

    progress(0, total, f"Found {total} photos")
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
    return results


# --------------------------------------------------------------------------
# Grouping + editable plan
# --------------------------------------------------------------------------

def group_documents(photos: list[PhotoInfo], gap_seconds: int) -> list[list[PhotoInfo]]:
    """Split time-ordered photos into documents; non-documents act as separators."""
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
        if prev is not None and (ph.taken - prev.taken).total_seconds() > gap_seconds:
            groups.append(current)
            current = []
        current.append(ph)
        prev = ph
    if current:
        groups.append(current)
    return groups


@dataclass
class Plan:
    """What will be saved. The review screen edits this; the tests exercise it directly."""

    groups: list[list[PhotoInfo]]
    others: list[PhotoInfo]  # not documents (or removed by the user)

    @classmethod
    def from_scan(cls, photos: list[PhotoInfo], gap_seconds: int) -> "Plan":
        groups = group_documents(photos, gap_seconds)
        others = [p for p in photos if not p.is_document]
        # Near-misses first so the user sees likely mistakes at the top.
        others.sort(key=lambda p: -p.words)
        return cls(groups, others)

    def split(self, g: int, i: int) -> None:
        """Start a new document at page i of group g."""
        if 0 < i < len(self.groups[g]):
            grp = self.groups[g]
            self.groups[g:g + 1] = [grp[:i], grp[i:]]

    def remove_group(self, g: int) -> None:
        self.others[0:0] = self.groups.pop(g)

    def join_previous(self, g: int) -> None:
        if g > 0:
            self.groups[g - 1].extend(self.groups.pop(g))

    def remove(self, g: int, i: int) -> None:
        ph = self.groups[g].pop(i)
        if not self.groups[g]:
            self.groups.pop(g)
        self.others.insert(0, ph)

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

    def output_names(self) -> list[str]:
        names = []
        width = max(2, len(str(len(self.groups))))
        for n, grp in enumerate(self.groups, 1):
            t = grp[0].taken
            pages = f"{len(grp)} page" + ("s" if len(grp) != 1 else "")
            names.append(f"Document {n:0{width}d} - {t:%Y-%m-%d %H.%M} ({pages}).pdf")
        return names
