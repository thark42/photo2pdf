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
from photo2pdf.naming import DocFields, merge, sanitize, suggest_name, text_from_tsv

DETECT_LONG_EDGE = 1600
THUMB_SIZE = 520        # documents: big enough for the "Large" thumbnail view
THUMB_SIZE_OTHER = 320  # non-documents (can be thousands of holiday photos)
CACHE_VERSION = 5
CARD_BACK_SECONDS = 120   # a low-text card photo this soon after a card is taken as its back


@dataclass
class PhotoInfo:
    path: Path
    taken: datetime
    words: int = 0
    is_document: bool = False
    rotation: int = 0                     # clockwise turn after straightening
    fields: Optional[DocFields] = None    # date / sender / type from OCR text
    saved_as: Optional[Path] = None       # PDF this photo already went into (registry)
    card_shape: bool = False              # straightened outline has ID-1 card proportions
    kind: str = "page"                    # see classify.py: page, card, receipt, confirmation, ticket, note, label
    reason: Optional[str] = None          # why it is not a document (envelope, screenshot, duplicate, ...)
    hard_to_read: bool = False            # blurred or low OCR confidence: suggest a retake
    screenshot: bool = False
    sharpness: float = 0.0
    dhash: Optional[int] = None           # image fingerprint (upright page)
    minhash: list = field(default_factory=list)  # text fingerprint (not reversible to the text)
    page_no: Optional[int] = None         # printed page number ("Pagina 2 van 3" -> 2)
    page_total: Optional[int] = None      # printed page count (-> 3), if given
    duplicate_of: Optional[Path] = None   # set aside as a second copy of this photo
    error: Optional[str] = None
    thumb: Optional[object] = field(default=None, repr=False, compare=False)  # PIL.Image
    thumb_jpeg: Optional[bytes] = field(default=None, repr=False, compare=False)
    thumb_rotation: int = 0   # rotation already applied to the stored thumbnail

    def get_thumb(self):
        """The thumbnail as it was made (turned by `thumb_rotation`), decoded from the compact
        JPEG copy on first use."""
        if self.thumb is None and self.thumb_jpeg:
            import io

            from PIL import Image

            self.thumb = Image.open(io.BytesIO(self.thumb_jpeg))
            self.thumb.load()
        return self.thumb

    def display_thumb(self):
        """The thumbnail turned to the page's current rotation (edits never touch the stored copy)."""
        t = self.get_thumb()
        if t is None:
            return None
        from photo2pdf.core import rotate_cw

        return rotate_cw(t, (self.rotation - self.thumb_rotation) % 360)

    def drop_decoded_thumb(self) -> None:
        """Free the decoded thumbnail; the JPEG copy stays."""
        if self.thumb_jpeg:
            self.thumb = None


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
    """Measure, OCR and classify one photo (see classify.py); documents also get orientation,
    name fields and fingerprints for duplicate detection."""
    from dataclasses import replace

    from photo2pdf import classify as cls
    from photo2pdf import diag
    from photo2pdf import features as ft

    taken = None
    t0 = time.perf_counter()
    try:
        with _open_image(path) as raw:
            taken = _exif_time(raw)
            screenshot = ft.is_screenshot(path, raw, raw.size)
        if taken is None:
            taken = _fallback_time(path)
        # Screenshots are already flat and upright: never warp or turn them.
        im = load_page(path, replace(opts, straighten=opts.straighten and not screenshot), max_edge=2000)
        if cached is not None:
            info = PhotoInfo(path=path, taken=taken, words=cached["words"],
                             rotation=cached.get("rotation", 0),
                             fields=DocFields.from_json(cached["fields"]) if cached.get("fields") else None,
                             card_shape=cached.get("card_shape", False), kind=cached.get("kind", "page"),
                             is_document=cached.get("is_document", False), reason=cached.get("reason"),
                             hard_to_read=cached.get("hard", False), dhash=cached.get("dhash"),
                             minhash=cached.get("minhash") or [], sharpness=cached.get("sharpness", 0.0),
                             screenshot=cached.get("screenshot", False),
                             page_no=(cached.get("page") or [None, None])[0],
                             page_total=(cached.get("page") or [None, None])[1])
        else:
            info = _measure(path, im, taken, tess, settings, opts, screenshot, ft, cls)
        logging.info("scan %s: words=%d document=%s kind=%s reason=%s card_shape=%s hard=%s rotation=%d "
                     "page=%s/%s cached=%s taken=%s (%.2fs)", path.name, info.words, info.is_document, info.kind,
                     info.reason, info.card_shape, info.hard_to_read, info.rotation, info.page_no,
                     info.page_total, cached is not None,
                     info.taken.isoformat(timespec="minutes"), time.perf_counter() - t0)
        if info.fields:
            diag.fields("scan %s fields %s", path.name, info.fields.to_json())
        # Kept as JPEG (tens of KB, not ~0.8 MB of pixels) and decoded only while on screen.
        import io

        info.thumb_rotation = info.rotation if info.is_document else 0
        thumb = rotate_cw(im, info.thumb_rotation).copy()
        size = THUMB_SIZE if info.is_document else THUMB_SIZE_OTHER
        thumb.thumbnail((size, size))
        buf = io.BytesIO()
        thumb.convert("RGB").save(buf, "JPEG", quality=82 if info.is_document else 70)
        info.thumb_jpeg = buf.getvalue()
        return info
    except Exception as exc:  # noqa: BLE001
        logging.exception("scan failed for %s", path.name)
        return PhotoInfo(path=path, taken=taken or _fallback_time(path), error=str(exc))


def _measure(path, im, taken, tess, settings, opts, screenshot, ft, cls) -> PhotoInfo:
    gray = flatten_lighting(_resize_long(im.convert("L"), DETECT_LONG_EDGE))
    card_shape = bool(im.info.get("photo2pdf_card"))
    quad = bool(im.info.get("photo2pdf_quad"))
    with tempfile.TemporaryDirectory(prefix="photo2pdf-d-") as td:
        work = Path(td)
        img = work / "d.png"

        def ocr(g):
            g.save(img)
            r = tess._run([img.name, "stdout", "-l", opts.language, "--psm", "1", "tsv"],
                          cwd=work, timeout=180, threads=1)
            return r.stdout or ""

        tsv = ocr(gray)
        words, text = count_words(tsv), text_from_tsv(tsv)
        pre_rotation = None
        if card_shape and words < settings.min_words:
            # Cards are small and often photographed upside down or sideways; page
            # segmentation then reads almost nothing. Turn it upright and read again.
            pre_rotation = find_rotation(im, tess, opts.language, work, threads=1, prefer_landscape=True)
            if pre_rotation:
                tsv = ocr(rotate_cw(gray, pre_rotation))
                words, text = count_words(tsv), text_from_tsv(tsv)
        feat = {"words": words, "quad": quad, "card_shape": card_shape, "screenshot": screenshot,
                "aspect": ft.page_aspect(im), "ink": ft.ink_density(gray), "sharpness": ft.sharpness(im)}
        feat.update(ft.ocr_stats(tsv))
        # Codes only change the decision together with ticket words, so only look for them then.
        from photo2pdf.naming import is_ticket_text

        feat.update(ft.find_codes(im) if is_ticket_text(text) else {"qr": False, "barcode": False})
        decision = cls.classify(feat, text, settings.min_words)
        if decision.kind == "receipt" and decision.fields is not None and "total" not in decision.fields.extra:
            from photo2pdf.naming import receipt_total_by_row

            total = receipt_total_by_row(tsv)
            if total:
                decision.fields.extra["total"] = total
        info = PhotoInfo(path=path, taken=taken, words=words, card_shape=card_shape,
                         kind=decision.kind or "page", is_document=decision.is_document,
                         reason=decision.reason, fields=decision.fields, screenshot=screenshot,
                         sharpness=feat["sharpness"])
        from photo2pdf.naming import page_number

        pn = None if screenshot else page_number(text)
        if pn:
            info.page_no, info.page_total = pn
            if not info.is_document and pn[1] and words >= 3:
                # "Pagina 3 van 3" with little else (a signature page, a short last page):
                # a printed page count is good evidence of a document page.
                logging.info("promoted %s to document: page %d of %d", path.name, pn[0], pn[1])
                info.is_document, info.kind, info.reason = True, "page", None
        if decision.is_document:
            if pre_rotation is not None:
                info.rotation = pre_rotation
            elif not screenshot:
                info.rotation = find_rotation(im, tess, opts.language, work, threads=1,
                                              prefer_landscape=decision.kind == "card")
            info.hard_to_read = cls.hard_to_read(feat)
        elif pre_rotation is not None:
            info.rotation = pre_rotation   # kept in case it becomes a card's back
        upright = rotate_cw(gray, info.rotation)
        info.dhash = ft.dhash(upright)
        # Text fingerprint only from a real reading; garbled low-confidence tokens from a blur
        # would make a blurred copy look like a different document.
        info.minhash = ft.minhash(text) if words >= 4 else []
        logging.info("features %s: %s", path.name,
                     {k: (round(v, 3) if isinstance(v, float) else v) for k, v in feat.items()})
    return info


def _fallback_time(path: Path) -> datetime:
    t = time_from_name(path.name)
    if t:
        return t
    try:
        return datetime.fromtimestamp(path.stat().st_mtime)
    except OSError:
        return datetime.fromtimestamp(0)


_SKIP_DIRS = {"appdata", "$recycle.bin", "system volume information", "node_modules",
              "windows", "program files", "program files (x86)", "programdata"}


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
                              "fields": info.fields.to_json() if info.fields else None,
                              "card_shape": info.card_shape, "kind": info.kind,
                              "is_document": info.is_document, "reason": info.reason,
                              "hard": info.hard_to_read, "dhash": info.dhash, "minhash": info.minhash,
                              "sharpness": info.sharpness, "screenshot": info.screenshot,
                              "page": [info.page_no, info.page_total]}
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
    # A photo that could not be read is never a document.
    for r in results:
        if r.error is not None:
            r.is_document = False
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
    if not current:
        return False
    if ph.kind != current[-1].kind:
        return True                      # a card next to a letter is never the same document
    if ph.kind in ("confirmation", "ticket", "label"):
        return True                      # one screenshot / ticket / type plate per document
    if ph.kind == "card" and len(current) >= 2:
        return True                      # a card has a front and a back, nothing more
    if not ph.fields:
        return False
    so_far = merge([p.fields for p in current if p.fields])
    f = ph.fields
    if ph.kind == "card" and f.doc_type and f.doc_type != so_far.doc_type:
        return True   # a recognised card type that differs from (or wasn't on) the card so far
    return bool((f.sender and so_far.sender and f.sender != so_far.sender)
                or (f.date and so_far.date and f.date != so_far.date))


PAGE_SEQUENCE_MAX_GAP = 2 * 3600   # seconds; consecutive page numbers join across longer pauses


def _continues(prev: PhotoInfo, ph: PhotoInfo) -> bool:
    """`ph` carries the page number right after `prev`'s, with no conflicting page count."""
    return (prev.page_no is not None and ph.page_no == prev.page_no + 1
            and (prev.page_total is None or ph.page_total is None or prev.page_total == ph.page_total))


def _page_break(prev: PhotoInfo, ph: PhotoInfo) -> bool:
    """Printed page numbers say `ph` cannot be the next page of `prev`'s document."""
    if prev.page_no is not None and prev.page_total and prev.page_no >= prev.page_total:
        return True                                   # the previous document was complete
    if ph.page_no is None:
        return False
    if ph.page_no == 1:
        return True
    if prev.page_no is None:
        return False
    if prev.page_total and ph.page_total and prev.page_total != ph.page_total:
        return True
    return ph.page_no <= prev.page_no


def group_documents(photos: list[PhotoInfo], gap_seconds: int) -> list[list[PhotoInfo]]:
    """Split time-ordered photos into documents.

    A photo set aside as a duplicate is skipped, not treated as a separator: a retaken page
    must not cut its letter in two. Printed page numbers decide first: page n+1 after page n
    joins (even after a long pause or with a different date in the text), and "page 1" or a
    restarted count begins a new document. Without page numbers: time gaps, other
    non-document photos, folder changes and a new letterhead separate documents.
    """
    groups: list[list[PhotoInfo]] = []
    current: list[PhotoInfo] = []
    prev: Optional[PhotoInfo] = None
    for ph in photos:
        if ph.duplicate_of is not None:
            continue
        if not ph.is_document:
            if current:
                groups.append(current)
                current = []
            prev = None
            continue
        if prev is not None:
            gap = (ph.taken - prev.taken).total_seconds()
            if ph.path.parent != prev.path.parent:
                split = True
            elif _continues(prev, ph) and ph.kind == prev.kind and gap <= PAGE_SEQUENCE_MAX_GAP:
                split = False
            elif _page_break(prev, ph):
                split = True
            else:
                split = gap > gap_seconds or _new_letterhead(current, ph)
            if split:
                groups.append(current)
                current = []
        current.append(ph)
        prev = ph
    if current:
        groups.append(current)
    return _join_page_runs(groups)


def _join_page_runs(groups: list[list[PhotoInfo]]) -> list[list[PhotoInfo]]:
    """Put back together a numbered document that something else cut in two, e.g. a photo
    of something else taken between page 4 and page 5."""
    out: list[list[PhotoInfo]] = []
    for g in groups:
        if out:
            a = out[-1]
            last = next((p for p in reversed(a) if p.page_no is not None), None)
            first = g[0]
            if (last is not None and _continues(last, first) and first.kind == last.kind
                    and first.path.parent == last.path.parent
                    and (first.taken - a[-1].taken).total_seconds() <= PAGE_SEQUENCE_MAX_GAP):
                logging.info("joined %s to the document before it: page %s follows page %s",
                             first.path.name, first.page_no, last.page_no)
                a.extend(g)
                continue
        out.append(g)
    return out


DUP_IMAGE_BITS = 40     # of 256 dHash bits; straightened re-shots of one page differ by far less
DUP_TEXT_SIMILARITY = 0.6
RETAKE_SECONDS = 15 * 60      # a retake is normally taken within minutes of the first shot
RETAKE_TEXT_SIMILARITY = 0.7  # same words, whatever the angle: a retake even if the picture differs


def _same_page(k: PhotoInfo, p: PhotoInfo) -> Optional[str]:
    """Why `p` is a second photo of the page `k` shows, or None."""
    from photo2pdf.features import hamming, minhash_similarity

    if k.kind != p.kind or k.path.parent != p.path.parent:
        return None
    if k.page_no is not None and p.page_no is not None and (
            k.page_no != p.page_no or (k.page_total and p.page_total and k.page_total != p.page_total)):
        return None                                  # different printed page numbers
    sim = minhash_similarity(k.minhash, p.minhash) if (k.minhash and p.minhash) else None
    close = abs((k.taken - p.taken).total_seconds()) <= RETAKE_SECONDS
    # 1. Near-identical picture, and the words (where both have them) agree.
    if hamming(k.dhash, p.dhash) <= DUP_IMAGE_BITS and (sim is None or sim >= DUP_TEXT_SIMILARITY):
        if sim is not None or not ((k.words >= 4 or p.words >= 4) and not (k.hard_to_read or p.hard_to_read)):
            return "same picture"
    if not close:
        return None
    # 2. Retaken from another angle or after a blurred first try: the picture differs, the text doesn't.
    if sim is not None and sim >= RETAKE_TEXT_SIMILARITY:
        return "same text"
    # 3. Same printed page of the same letter, minutes apart, one of them barely readable.
    if (k.page_no is not None and p.page_no == k.page_no and k.page_total and p.page_total == k.page_total
            and (k.hard_to_read or p.hard_to_read or sim is None or sim >= 0.3)):
        fk, fp = k.fields, p.fields
        if not (fk and fp and fk.sender and fp.sender and fk.sender != fp.sender):
            return f"same page number {p.page_no} of {p.page_total}"
    return None


def mark_duplicates(photos: list[PhotoInfo]) -> None:
    """The same page or card photographed more than once: keep the clearest, set the rest aside.

    A second photo counts as a copy when (1) its upright picture is near-identical and the
    words, where both have them, agree; or, when taken within 15 minutes of the other,
    (2) the words agree closely although the picture differs (another angle), or (3) it
    shows the same printed page number of the same letter. Different printed page numbers
    are never copies. The copy with more readable words, then the sharper one, is kept.
    Nothing is deleted: the others appear under "Not documents" as "Duplicate of ..."
    and can be added back.
    """
    docs = [p for p in photos if p.is_document and p.dhash is not None]
    docs.sort(key=lambda p: (-p.words, -p.sharpness))      # best copies first
    kept: list[PhotoInfo] = []
    for p in docs:
        dup_of, why = None, None
        for k in kept:
            why = _same_page(k, p)
            if why:
                dup_of = k
                break
        if dup_of is None:
            kept.append(p)
        else:
            p.is_document = False
            p.duplicate_of = dup_of.path
            p.reason = f"Duplicate of {dup_of.path.name} (the clearer copy is kept)"
            logging.info("duplicate %s of %s (%s)", p.path.name, dup_of.path.name, why)


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
        # The back of a card often has almost no text (stripe, barcode, signature panel).
        # A card-shaped photo taken right after a card front counts as that card's back.
        # Only a front that is a card on its own evidence can take a back, and only the
        # photo taken directly after it, so backs never chain onto other backs.
        prev, prev_is_front = None, False
        for ph in sorted(photos, key=lambda p: p.taken):
            is_front = ph.kind == "card" and ph.is_document
            if (not ph.is_document and ph.card_shape and prev is not None and prev_is_front
                    and (ph.taken - prev.taken).total_seconds() <= CARD_BACK_SECONDS
                    and ph.path.parent == prev.path.parent):
                ph.is_document, ph.kind = True, "card"
                is_front = False
            prev, prev_is_front = ph, is_front
        mark_duplicates(photos)
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
        snap = ([[(p, p.rotation) for p in g] for g in self.groups],
                [(p, p.rotation) for p in self.others],
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
            for p, rot in items:
                p.rotation = rot
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
        ph.reason, ph.duplicate_of = None, None
        if ph.kind not in ("page", "card", "receipt", "confirmation", "ticket", "note", "label"):
            ph.kind = "page"
        idx = 0
        while idx < len(self.groups) and self.groups[idx][0].taken <= ph.taken:
            idx += 1
        self.groups.insert(idx, [ph])

    # -- names and folders ----------------------------------------------------

    def fields(self, g: int) -> DocFields:
        return merge([p.fields for p in self.groups[g] if p.fields])

    def is_card(self, g: int) -> bool:
        return any(p.kind == "card" for p in self.groups[g])

    def kind(self, g: int) -> str:
        return self.groups[g][0].kind

    def hard_to_read(self, g: int) -> bool:
        return any(p.hard_to_read for p in self.groups[g])

    def suggested_name(self, g: int) -> str:
        grp = self.groups[g]
        width = max(2, len(str(len(self.groups))))
        t = grp[0].taken
        f = self.fields(g)
        kind = self.kind(g)
        if self.is_card(g):
            f.kind = "card"
            sides = "front and back" if len(grp) == 2 else f"{len(grp)} side" + ("s" if len(grp) != 1 else "")
            fallback = f"Card {g + 1:0{width}d} - {t:%Y-%m-%d %H.%M} ({sides}).pdf"
        elif kind in ("receipt", "confirmation", "ticket", "note", "label"):
            f.kind = kind
            word = {"receipt": "Receipt", "confirmation": "Screenshot", "ticket": "Ticket",
                    "note": "Note", "label": "Product label"}[kind]
            fallback = f"{word} - {t:%Y-%m-%d %H.%M}.pdf"
            if kind == "note":
                return fallback
        else:
            pages = f"{len(grp)} page" + ("s" if len(grp) != 1 else "")
            fallback = f"Document {g + 1:0{width}d} - {t:%Y-%m-%d %H.%M} ({pages}).pdf"
        return suggest_name(f, fallback)

    def folder_for(self, g: int, organize: str) -> str:
        """Subfolder (possibly empty) a document goes into for the chosen organize mode."""
        from photo2pdf.classify import FOLDERS, YEARLESS

        kind = "card" if self.is_card(g) else self.kind(g)
        if kind in FOLDERS:
            if organize == "none":
                return ""
            if kind in YEARLESS:
                return FOLDERS[kind]
            f = self.fields(g)
            year = f.date.year if f.date else self.groups[g][0].taken.year
            return f"{FOLDERS[kind]}/{year}"
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
